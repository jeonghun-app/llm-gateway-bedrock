"""Amazon Bedrock 어댑터.

Bedrock 호출은 이 모듈에만 존재한다. 상위 계층은 도메인 예외와 값 객체만
본다.

Converse API 를 쓰는 이유는 모델별 요청/응답 스키마 차이를 AWS 쪽에서
흡수해 주기 때문이다. `InvokeModel` 을 쓰면 Anthropic·Nova·Llama 마다 다른
본문을 게이트웨이가 직접 만들어야 하고, 새 모델이 나올 때마다 코드를
고쳐야 한다.
"""

from __future__ import annotations

import dataclasses
import typing

import boto3
import botocore.exceptions

from llmgw import cache
from llmgw import domain
from llmgw import errors
from llmgw import observability
from llmgw import repository
from llmgw import translate

_JsonDict = dict[str, typing.Any]

# 모델 목록은 자주 바뀌지 않는다. 컨트롤 플레인 API 는 스로틀링 한도가
# 낮으므로 캐시해서 호출 수를 줄인다.
_MODEL_LIST_TTL_SECONDS = 300.0
_MODEL_CACHE_KEY = "bedrock:models"

# Bedrock 에러 코드 → 게이트웨이 예외 매핑.
_ERROR_MAP: dict[str, type[errors.GatewayError]] = {
    "ValidationException": errors.InvalidRequestError,
    "ResourceNotFoundException": errors.ModelNotFoundError,
    "AccessDeniedException": errors.PermissionDeniedError,
    "ThrottlingException": errors.UpstreamRateLimitError,
    "TooManyRequestsException": errors.UpstreamRateLimitError,
    "ServiceQuotaExceededException": errors.UpstreamRateLimitError,
    "ModelTimeoutException": errors.UpstreamError,
    "ModelNotReadyException": errors.UpstreamRateLimitError,
    "ModelErrorException": errors.UpstreamError,
    "ModelStreamErrorException": errors.UpstreamError,
    "InternalServerException": errors.UpstreamError,
    "ServiceUnavailableException": errors.UpstreamError,
}

# Converse API 로 호출할 수 없는 모델 계열. `list_foundation_models` 를
# `byOutputModality=TEXT` 로 걸러도 재순위(rerank)·임베딩 모델이 함께
# 나오고, 추론 프로파일 목록에는 이미지 생성 모델까지 섞인다. 이것들을
# `/v1/models` 로 노출하면 클라이언트가 골라 쓴 뒤 Bedrock 이
# "This action doesn't support the model" 로 400 을 낸다. Bedrock 이
# Converse 지원 여부를 알려주는 필드를 제공하지 않아 계열로 판별한다.
_NON_CONVERSE_MARKERS: tuple[str, ...] = (
    "embed",
    "rerank",
    "stability.",
    "stable-",
    "-image",
    "image-",
    "canvas",
    "reel",
    "sonic",
)


def supports_converse(model_id: str) -> bool:
    """모델 ID 가 Converse 로 호출 가능한 계열인지 판정한다.

    Args:
        model_id: 기반 모델 ID 또는 추론 프로파일 ID.

    Returns:
        Converse 로 호출할 수 있다고 판단되면 `True`.
    """
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CONVERSE_MARKERS)


@dataclasses.dataclass(frozen=True)
class ConverseResult:
    """비스트리밍 Converse 호출 결과.

    Attributes:
        text: 어시스턴트 응답 텍스트.
        stop_reason: Bedrock 이 반환한 정지 이유.
        input_tokens: 입력 토큰 수.
        output_tokens: 출력 토큰 수.
    """

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int


@dataclasses.dataclass(frozen=True)
class StreamDelta:
    """스트리밍 중 발생한 이벤트 하나.

    Attributes:
        text: 이번 이벤트의 텍스트 증분. 메타데이터 이벤트면 빈 문자열.
        stop_reason: 정지 이유. `messageStop` 이벤트에서만 채워진다.
        input_tokens: 입력 토큰 수. `metadata` 이벤트에서만 채워진다.
        output_tokens: 출력 토큰 수. `metadata` 이벤트에서만 채워진다.
        is_final: 사용량 메타데이터를 담은 마지막 이벤트인지 여부.
    """

    text: str = ""
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    is_final: bool = False


def create_clients(
    *, region: str, timeout_seconds: int
) -> tuple[typing.Any, typing.Any]:
    """Bedrock 컨트롤 플레인과 런타임 클라이언트를 만든다.

    두 클라이언트를 모듈 밖에서 한 번만 만들어 재사용한다. boto3 클라이언트
    생성은 자격증명 해석과 모델 로딩을 수반해 비싸다.

    Args:
        region: Bedrock 리전.
        timeout_seconds: 런타임 호출 읽기 타임아웃(초). 스트리밍 응답이
            길어질 수 있어 넉넉히 준다.

    Returns:
        (`bedrock` 클라이언트, `bedrock-runtime` 클라이언트) 튜플.
    """
    control_config = repository.boto_config(30)
    runtime_config = repository.boto_config(timeout_seconds)
    return (
        boto3.client("bedrock", region_name=region, config=control_config),
        boto3.client(
            "bedrock-runtime", region_name=region, config=runtime_config
        ),
    )


class BedrockGateway:
    """Bedrock Converse 호출과 모델 목록 조회를 담당한다."""

    def __init__(
        self,
        *,
        control_client: typing.Any,
        runtime_client: typing.Any,
        logger: observability.Logger,
        model_cache: cache.TtlCache[tuple[str, ...]] | None = None,
    ) -> None:
        """어댑터를 만든다.

        Args:
            control_client: `bedrock` 클라이언트.
            runtime_client: `bedrock-runtime` 클라이언트.
            logger: 구조화 로거.
            model_cache: 모델 목록 캐시. 생략하면 기본 TTL 캐시를 만든다.
        """
        self._control = control_client
        self._runtime = runtime_client
        self._logger = logger
        self._model_cache: cache.TtlCache[tuple[str, ...]] = (
            model_cache or cache.TtlCache(ttl_seconds=_MODEL_LIST_TTL_SECONDS)
        )

    def list_model_ids(self) -> tuple[str, ...]:
        """호출 가능한 모델 ID 목록을 반환한다.

        온디맨드 텍스트 모델과 활성 상태의 크로스리전 추론 프로파일을
        합쳐서 반환한다. 추론 프로파일을 포함하는 이유는 최신 Anthropic
        모델이 기반 모델 ID 로는 온디맨드 호출을 받지 않고 프로파일 ID 로만
        받는 경우가 있기 때문이다.

        Returns:
            정렬된 모델 ID 튜플. 조회에 실패하면 빈 튜플.
        """
        return self._model_cache.get_or_load(
            _MODEL_CACHE_KEY, self._load_model_ids
        )

    def converse(
        self,
        *,
        model_id: str,
        messages: list[_JsonDict],
        system: list[_JsonDict],
        inference_config: _JsonDict,
        guardrail: domain.GuardrailDecision | None = None,
    ) -> ConverseResult:
        """비스트리밍 Converse 를 호출한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.
            messages: Converse `messages`.
            system: Converse `system`. 비어 있으면 전달하지 않는다.
            inference_config: Converse `inferenceConfig`.

        Returns:
            응답 텍스트와 토큰 사용량.

        Raises:
            GatewayError: Bedrock 호출이 실패한 경우. 원인에 따라
                `InvalidRequestError`, `ModelNotFoundError`,
                `UpstreamRateLimitError`, `UpstreamError` 등으로 변환된다.
        """
        params = self._build_params(
            model_id=model_id,
            messages=messages,
            system=system,
            inference_config=inference_config,
            guardrail=guardrail,
            streaming=False,
        )
        try:
            response = self._runtime.converse(**params)
        except botocore.exceptions.ClientError as exc:
            raise self._translate_error(exc, model_id) from exc
        except botocore.exceptions.BotoCoreError as exc:
            # 커넥션 타임아웃, DNS 실패 등 SDK 레벨 오류.
            raise errors.UpstreamError(
                f"Bedrock 호출에 실패했다: {type(exc).__name__}"
            ) from exc

        usage = response.get("usage") or {}
        return ConverseResult(
            text=translate.extract_text(response.get("output") or {}),
            stop_reason=str(response.get("stopReason") or ""),
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
        )

    def converse_stream(
        self,
        *,
        model_id: str,
        messages: list[_JsonDict],
        system: list[_JsonDict],
        inference_config: _JsonDict,
        guardrail: domain.GuardrailDecision | None = None,
    ) -> typing.Iterator[StreamDelta]:
        """스트리밍 Converse 를 호출한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.
            messages: Converse `messages`.
            system: Converse `system`.
            inference_config: Converse `inferenceConfig`.

        Yields:
            텍스트 증분과 종료 메타데이터를 담은 `StreamDelta`.

        Raises:
            GatewayError: 호출이 실패하거나 스트림 중간에 오류가 난 경우.
        """
        params = self._build_params(
            model_id=model_id,
            messages=messages,
            system=system,
            inference_config=inference_config,
            guardrail=guardrail,
            streaming=True,
        )
        try:
            response = self._runtime.converse_stream(**params)
            event_stream = response["stream"]
        except botocore.exceptions.ClientError as exc:
            raise self._translate_error(exc, model_id) from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise errors.UpstreamError(
                f"Bedrock 스트리밍 호출에 실패했다: {type(exc).__name__}"
            ) from exc

        yield from self._iterate_stream(event_stream, model_id)

    # -- 내부 ---------------------------------------------------------------

    def _iterate_stream(
        self, event_stream: typing.Iterable[_JsonDict], model_id: str
    ) -> typing.Iterator[StreamDelta]:
        """이벤트 스트림을 `StreamDelta` 로 변환한다."""
        try:
            for event in event_stream:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta") or {}
                    text = str(delta.get("text") or "")
                    if text:
                        yield StreamDelta(text=text)
                elif "messageStop" in event:
                    yield StreamDelta(
                        stop_reason=str(
                            event["messageStop"].get("stopReason") or ""
                        )
                    )
                elif "metadata" in event:
                    usage = event["metadata"].get("usage") or {}
                    yield StreamDelta(
                        input_tokens=int(usage.get("inputTokens") or 0),
                        output_tokens=int(usage.get("outputTokens") or 0),
                        is_final=True,
                    )
                else:
                    # internalServerException 등 오류 이벤트가 스트림 안에
                    # 섞여 오는 경우가 있다. 조용히 끊지 않고 변환한다.
                    error_event = self._find_error_event(event)
                    if error_event is not None:
                        raise errors.UpstreamError(
                            f"Bedrock 스트림 오류: {error_event}"
                        )
        except botocore.exceptions.ClientError as exc:
            raise self._translate_error(exc, model_id) from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise errors.UpstreamError(
                f"Bedrock 스트림이 중단됐다: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _find_error_event(event: _JsonDict) -> str | None:
        """이벤트가 오류 이벤트인지 판별한다.

        Args:
            event: 스트림 이벤트.

        Returns:
            오류 이벤트 키 이름. 오류가 아니면 `None`.
        """
        for key in event:
            if key.endswith("Exception") or key.endswith("Error"):
                return key
        return None

    def verify_guardrail(self, guardrail_id: str, version: str) -> None:
        """가드레일이 존재하고 사용 가능한지 확인한다.

        설정을 저장하기 전에 부른다. 없는 가드레일을 저장하면 이후 모든 요청이
        `ValidationException` 으로 실패하는데, 그것을 배포 후에 알게 되는 것보다
        여기서 막는 편이 낫다.

        **런타임 fail-closed 를 대체하지는 않는다.** 저장 시점에 유효했던
        가드레일이 나중에 삭제되거나 권한이 바뀔 수 있다. 그 경우 AWS 가
        Converse 를 `ValidationException` 으로 거부하므로 조용히 통과하지는
        않는다.

        Args:
            guardrail_id: 가드레일 식별자 또는 ARN.
            version: 가드레일 버전.

        Raises:
            ResourceNotFoundError: 가드레일이 없거나 접근할 수 없는 경우.
            UpstreamError: 그 밖의 AWS 오류.
        """
        try:
            response = self._control.get_guardrail(
                guardrailIdentifier=guardrail_id, guardrailVersion=version
            )
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ResourceNotFoundException", "ValidationException"):
                raise errors.ResourceNotFoundError(
                    f"가드레일을 찾을 수 없다: {guardrail_id} 버전 {version}."
                    " 식별자와 버전, 그리고 이 계정·리전에 있는지 확인한다."
                ) from exc
            if code == "AccessDeniedException":
                raise errors.ResourceNotFoundError(
                    f"가드레일에 접근할 수 없다: {guardrail_id}."
                    " 태스크 역할에 bedrock:GetGuardrail 권한이 있는지"
                    " 확인한다."
                ) from exc
            raise errors.UpstreamError(
                f"가드레일 조회에 실패했다: {code}"
            ) from exc

        status = str(response.get("status", ""))
        if status != "READY":
            # 생성·수정 중인 가드레일을 기준선으로 삼으면 요청이 실패한다.
            raise errors.ResourceNotFoundError(
                f"가드레일이 사용 가능한 상태가 아니다: {status}."
                " READY 가 될 때까지 기다린 뒤 다시 설정한다."
            )

    @staticmethod
    def _build_params(
        *,
        model_id: str,
        messages: list[_JsonDict],
        system: list[_JsonDict],
        inference_config: _JsonDict,
        guardrail: domain.GuardrailDecision | None = None,
        streaming: bool = False,
    ) -> _JsonDict:
        """Converse 호출 파라미터를 만든다.

        빈 `system` 이나 빈 `inferenceConfig` 를 넘기면 일부 모델이
        ValidationException 을 던지므로 값이 있을 때만 포함한다.

        가드레일은 판정이 적용을 지시할 때만 붙인다. 두 가지를 고정한다.

        - `trace` 는 `disabled` 다. 켜면 응답에 `modelOutput`(차단하려던 원문)
          이 들어온다. 그것이 로그로 새면 막으려던 내용이 로그에 남는다.
        - 스트리밍은 `streamProcessingMode` 를 `sync` 로 강제한다. `async` 는
          차단 대상 텍스트를 클라이언트에 먼저 보내고 나중에 개입을 알린다.
          실측에서 차단어가 그대로 전달되는 것을 확인했다. 클라이언트가 이
          값을 고를 수 없어야 한다.
        """
        params: _JsonDict = {"modelId": model_id, "messages": messages}
        if system:
            params["system"] = system
        if inference_config:
            params["inferenceConfig"] = inference_config
        if guardrail is not None and guardrail.applied:
            config: _JsonDict = {
                "guardrailIdentifier": guardrail.guardrail_id,
                "guardrailVersion": guardrail.guardrail_version,
                "trace": "disabled",
            }
            if streaming:
                config["streamProcessingMode"] = "sync"
            params["guardrailConfig"] = config
        return params

    def _translate_error(
        self, exc: botocore.exceptions.ClientError, model_id: str
    ) -> errors.GatewayError:
        """botocore 오류를 게이트웨이 예외로 바꾼다.

        Args:
            exc: 발생한 ClientError.
            model_id: 호출 대상 모델 ID. 메시지에 포함해 추적을 돕는다.

        Returns:
            변환된 게이트웨이 예외.
        """
        error = exc.response.get("Error", {})
        code = str(error.get("Code") or "Unknown")
        message = str(error.get("Message") or "")
        self._logger.warning(
            "Bedrock 호출이 실패했다",
            extra={
                "bedrock_error_code": code,
                "model_id": model_id,
            },
        )
        error_class = _ERROR_MAP.get(code, errors.UpstreamError)
        if error_class is errors.PermissionDeniedError:
            return errors.PermissionDeniedError(
                f"모델에 접근할 수 없다. Bedrock 모델 액세스를 확인한다:"
                f" {model_id}"
            )
        if error_class is errors.ModelNotFoundError:
            return errors.ModelNotFoundError(
                f"모델을 찾을 수 없다: {model_id}. {message}"
            )
        return error_class(f"{code}: {message}")

    def _load_model_ids(self) -> tuple[str, ...]:
        """Bedrock 에서 Converse 로 호출 가능한 모델 목록을 조회한다.

        실패하면 빈 튜플을 반환한다.
        """
        model_ids: set[str] = set()
        try:
            response = self._control.list_foundation_models(
                byOutputModality="TEXT", byInferenceType="ON_DEMAND"
            )
            for summary in response.get("modelSummaries", []):
                model_id = summary.get("modelId")
                if model_id and supports_converse(str(model_id)):
                    model_ids.add(str(model_id))
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ):
            self._logger.exception("기반 모델 목록 조회에 실패했다")

        try:
            paginator = self._control.get_paginator("list_inference_profiles")
            for page in paginator.paginate():
                for profile in page.get("inferenceProfileSummaries", []):
                    if profile.get("status") != "ACTIVE":
                        continue
                    profile_id = profile.get("inferenceProfileId")
                    # 추론 프로파일도 걸러야 한다. 프로파일 목록에는 이미지
                    # 생성 모델처럼 Converse 로 호출할 수 없는 것이 섞여
                    # 들어온다.
                    if profile_id and supports_converse(str(profile_id)):
                        model_ids.add(str(profile_id))
        except (
            botocore.exceptions.ClientError,
            botocore.exceptions.BotoCoreError,
        ):
            # 추론 프로파일 API 가 없는 리전도 있다. 기반 모델만으로도
            # 동작해야 하므로 실패를 치명적으로 다루지 않는다.
            self._logger.warning("추론 프로파일 목록 조회를 건너뛴다")

        return tuple(sorted(model_ids))
