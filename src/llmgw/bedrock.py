"""Amazon Bedrock 어댑터.

Bedrock 호출은 이 모듈에만 존재한다. 상위 계층은 도메인 예외와 값 객체만
본다.

채팅은 Converse API 를 쓴다. 모델별 요청/응답 스키마 차이를 AWS 쪽에서
흡수해 주기 때문이다. `InvokeModel` 을 쓰면 Anthropic·Nova·Llama 마다 다른
본문을 게이트웨이가 직접 만들어야 하고, 새 모델이 나올 때마다 코드를
고쳐야 한다.

**임베딩만 `InvokeModel` 을 쓴다.** Converse 가 임베딩 모델을 지원하지 않아
선택의 여지가 없다. 그래서 위에서 피하려 한 계열별 본문 분기가 임베딩에는
존재하고, 분기를 최소로 유지하기 위해 **입력 토큰 수를 실제로 보고하는 계열만**
지원한다. 토큰 수를 모르면 비용이 0 으로 기록되어 월 예산이 조용히 무효가
된다.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
import typing

import boto3
import botocore.exceptions

from llmgw import cache
from llmgw import domain
from llmgw import errors
from llmgw import observability
from llmgw import pricing
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

# 지원하는 임베딩 모델 계열 → 모델 ID 접두어.
#
# 좁게 유지한다. `InvokeModel` 은 계열마다 요청·응답 본문이 다르고, 입력
# 토큰 수를 돌려주지 않는 모델도 있다. 토큰 수를 모르면 비용이 0 으로
# 집계되어 월 예산이 조용히 무효가 되므로, 실제 응답 형태를 확인한 계열만
# 넣는다. Cohere 계열은 토큰 수 보고 여부를 확인하지 못해 제외했다.
_TITAN_EMBED = "titan-embed"
_EMBEDDING_FAMILIES: dict[str, str] = {
    _TITAN_EMBED: "amazon.titan-embed-text",
}

# 임베딩 배치 하나에 허용하는 총 시간. Titan 은 호출당 텍스트 하나만 받아
# 배치가 그대로 순차 호출 수가 된다. 상한이 없으면 한 요청이 워커 스레드를
# 오래 점유해 다른 요청의 지연으로 번지고, 그 사이에도 Bedrock 은 계속
# 청구된다. ALB 유휴 타임아웃보다 작게 잡아 클라이언트가 오류를 받게 한다.
_EMBED_BATCH_DEADLINE_SECONDS = 45.0


def supports_converse(model_id: str) -> bool:
    """모델 ID 가 Converse 로 호출 가능한 계열인지 판정한다.

    Args:
        model_id: 기반 모델 ID 또는 추론 프로파일 ID.

    Returns:
        Converse 로 호출할 수 있다고 판단되면 `True`.
    """
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CONVERSE_MARKERS)


def embedding_family(model_id: str) -> str | None:
    """임베딩 모델 ID 의 계열을 판정한다.

    지원 계열을 좁게 유지하는 이유는 `InvokeModel` 응답에서 **입력 토큰 수를
    실제로 돌려주는** 모델만 비용을 정확히 귀속할 수 있기 때문이다. 토큰 수를
    추정해 넣으면 예산이 걸린 주체의 통제가 조용히 부정확해진다.

    Args:
        model_id: 모델 ID.

    Returns:
        계열 이름. 지원하지 않으면 `None`.
    """
    normalized = pricing.normalize_model_id(model_id).lower()
    for family, prefix in _EMBEDDING_FAMILIES.items():
        if normalized.startswith(prefix):
            return family
    return None


@dataclasses.dataclass(frozen=True)
class EmbedResult:
    """임베딩 호출 결과.

    Attributes:
        vectors: 입력 순서와 같은 순서의 임베딩 벡터 목록.
        input_tokens: 모델이 보고한 입력 토큰 수 합계.
    """

    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int


@dataclasses.dataclass(frozen=True)
class ConverseResult:
    """비스트리밍 Converse 호출 결과.

    Attributes:
        text: 어시스턴트 응답 텍스트.
        stop_reason: Bedrock 이 반환한 정지 이유.
        input_tokens: 입력 토큰 수.
        output_tokens: 출력 토큰 수.
        tool_calls: 모델이 요청한 도구 호출 목록.
    """

    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    tool_calls: tuple[translate.ToolUse, ...] = ()


@dataclasses.dataclass(frozen=True)
class StreamDelta:
    """스트리밍 중 발생한 이벤트 하나.

    Attributes:
        text: 이번 이벤트의 텍스트 증분. 메타데이터 이벤트면 빈 문자열.
        stop_reason: 정지 이유. `messageStop` 이벤트에서만 채워진다.
        input_tokens: 입력 토큰 수. `metadata` 이벤트에서만 채워진다.
        output_tokens: 출력 토큰 수. `metadata` 이벤트에서만 채워진다.
        is_final: 사용량 메타데이터를 담은 마지막 이벤트인지 여부.
        tool_index: 도구 호출 순번. **Bedrock 의 contentBlockIndex 가 아니다.**
            OpenAI 는 `tool_calls[].index` 를 도구 호출만 세는 번호로 쓰는데,
            Converse 의 블록 인덱스는 텍스트 블록까지 함께 센다. 그대로 쓰면
            텍스트가 앞에 있을 때 번호가 밀린다.
        tool_use_id: 도구 호출 ID. 도구 블록 시작 이벤트에서만 채워진다.
        tool_name: 도구 이름. 도구 블록 시작 이벤트에서만 채워진다.
        tool_arguments_delta: 인자 JSON 문자열의 증분.
    """

    text: str = ""
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    is_final: bool = False
    tool_index: int | None = None
    tool_use_id: str = ""
    tool_name: str = ""
    tool_arguments_delta: str = ""


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

    def embed(
        self,
        *,
        model_id: str,
        texts: typing.Sequence[str],
        dimensions: int | None = None,
    ) -> EmbedResult:
        """임베딩을 생성한다.

        Converse 는 임베딩 모델을 지원하지 않아 `InvokeModel` 을 쓴다. 즉
        모델 계열마다 본문 형태가 달라 게이트웨이가 직접 만들어야 한다.
        Converse 를 고른 이유가 바로 그 분기를 피하는 것이었으므로, 여기서는
        **토큰 수를 실제로 돌려주는 계열만** 지원한다.

        토큰 수를 모르는 모델을 추정값으로 기록하면 비용이 틀리고, 월 예산이
        걸린 주체에게는 통제가 조용히 무효가 된다. 그래서 지원 목록에 없는
        모델은 호출 전에 거부한다.

        Args:
            model_id: 임베딩 모델 ID.
            texts: 임베딩할 텍스트 목록.
            dimensions: 출력 차원 수. 모델이 지원할 때만 전달된다.

        Returns:
            벡터 목록과 입력 토큰 수.

        Raises:
            InvalidRequestError: 지원하지 않는 모델 계열인 경우.
            GatewayError: Bedrock 호출이 실패한 경우.
        """
        family = embedding_family(model_id)
        if family is None:
            raise errors.InvalidRequestError(
                f"이 게이트웨이가 지원하지 않는 임베딩 모델이다: {model_id}."
                " 임베딩은 Converse 가 아니라 InvokeModel 을 쓰기 때문에 모델"
                " 계열마다 본문 형태가 다르고, 토큰 수를 보고하지 않는 모델은"
                " 비용을 정확히 귀속할 수 없어 지원하지 않는다. 지원 계열: "
                + ", ".join(sorted(_EMBEDDING_FAMILIES))
            )

        vectors: list[tuple[float, ...]] = []
        total_tokens = 0
        # Titan 은 호출당 텍스트 하나만 받는다. 순서를 보장해야 하므로
        # 순차 호출한다. 배치 상한이 있어 호출 수는 제한된다.
        #
        # 실패해도 **이미 성공한 호출의 토큰은 잃지 않는다.** 그 호출은
        # 실제로 청구됐다. 0 으로 기록하면 비용이 집계에서 사라지고, 마지막
        # 입력만 실패하게 만들면 월 예산을 무한히 우회할 수 있다.
        deadline = time.monotonic() + _EMBED_BATCH_DEADLINE_SECONDS
        try:
            for text in texts:
                if time.monotonic() > deadline:
                    raise errors.UpstreamError(
                        "임베딩 배치가 제한 시간"
                        f"({_EMBED_BATCH_DEADLINE_SECONDS}초)을 넘겼다."
                        " 입력 수를 줄여 다시 보낸다."
                    )
                body = self._embedding_body(family, text, dimensions)
                try:
                    response = self._runtime.invoke_model(
                        modelId=model_id,
                        body=json.dumps(body),
                        contentType="application/json",
                        accept="application/json",
                    )
                    # 응답 본문을 다 읽기 전까지는 호출이 끝난 것이 아니다.
                    # 스트리밍 바디를 읽다가 나는 BotoCoreError(예:
                    # ReadTimeoutError)도 여기서 잡아야, 바깥의
                    # consumed_input_tokens 기록이 이 호출들의 토큰을
                    # 놓치지 않는다.
                    payload = self._read_embedding_body(response)
                except botocore.exceptions.ClientError as exc:
                    raise self._translate_error(exc, model_id) from exc
                except botocore.exceptions.BotoCoreError as exc:
                    raise errors.UpstreamError(
                        f"Bedrock 임베딩 호출에 실패했다: {type(exc).__name__}"
                    ) from exc
                vector, tokens = self._parse_embedding(
                    family, payload, model_id
                )
                vectors.append(vector)
                total_tokens += tokens
        except errors.GatewayError as exc:
            exc.consumed_input_tokens = total_tokens
            raise

        return EmbedResult(vectors=tuple(vectors), input_tokens=total_tokens)

    @staticmethod
    def _embedding_body(
        family: str, text: str, dimensions: int | None
    ) -> _JsonDict:
        """모델 계열에 맞는 InvokeModel 본문을 만든다."""
        if family == _TITAN_EMBED:
            body: _JsonDict = {"inputText": text}
            if dimensions is not None:
                body["dimensions"] = dimensions
            return body
        # 지원 계열은 embedding_family 가 이미 걸렀다.
        raise errors.InvalidRequestError(
            f"임베딩 본문을 만들 수 없는 계열이다: {family}"
        )

    @staticmethod
    def _read_embedding_body(response: _JsonDict) -> _JsonDict:
        """InvokeModel 응답 본문을 JSON 으로 읽는다.

        Raises:
            UpstreamError: 본문이 JSON 이 아닌 경우.
        """
        raw = response.get("body")
        if raw is None:
            raise errors.UpstreamError("Bedrock 임베딩 응답에 본문이 없다.")
        data = raw.read() if hasattr(raw, "read") else raw
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise errors.UpstreamError(
                "Bedrock 임베딩 응답을 해석할 수 없다."
            ) from exc
        if not isinstance(parsed, dict):
            raise errors.UpstreamError("Bedrock 임베딩 응답이 객체가 아니다.")
        return parsed

    @staticmethod
    def _parse_embedding(
        family: str, payload: _JsonDict, model_id: str
    ) -> tuple[tuple[float, ...], int]:
        """모델 계열에 맞게 벡터와 토큰 수를 뽑는다.

        Raises:
            UpstreamError: 벡터나 토큰 수가 없는 경우. 토큰 수를 0 으로
                기록하면 비용이 0 이 되어 예산이 조용히 무효가 된다.
        """
        if family != _TITAN_EMBED:
            raise errors.UpstreamError(
                f"임베딩 응답을 해석할 수 없는 계열이다: {family}"
            )
        raw_vector = payload.get("embedding")
        if not isinstance(raw_vector, list) or not raw_vector:
            raise errors.UpstreamError(f"임베딩 응답에 벡터가 없다: {model_id}")
        token_count = payload.get("inputTextTokenCount")
        # bool 은 int 의 하위 타입이라 isinstance 만으로는 걸러지지 않는다.
        # 음수도 거부한다. 음수 토큰 수는 음수 비용을 만들어 집계를 되돌린다.
        if (
            not isinstance(token_count, int)
            or isinstance(token_count, bool)
            or token_count < 0
        ):
            raise errors.UpstreamError(
                f"임베딩 응답의 입력 토큰 수가 올바르지 않다: {model_id}."
                " 토큰 수를 모르면 비용을 0 으로 기록하게 되고, 예산이 걸린"
                " 주체에게는 통제가 조용히 무효가 되므로 오류로 만든다."
            )
        # float() 의 ValueError/TypeError 는 GatewayError 가 아니라서 라우터가
        # 잡지 못한다. 그러면 이미 소비된 토큰을 기록하지 못한 채 500 이 나간다.
        try:
            vector = tuple(float(value) for value in raw_vector)
        except (TypeError, ValueError) as exc:
            raise errors.UpstreamError(
                f"임베딩 벡터를 숫자로 해석할 수 없다: {model_id}"
            ) from exc
        if any(not math.isfinite(value) for value in vector):
            # NaN/Inf 는 JSON 직렬화에서 깨지거나 조용히 통과한다.
            raise errors.UpstreamError(
                f"임베딩 벡터에 유한하지 않은 값이 있다: {model_id}"
            )
        return (vector, token_count)

    def converse(
        self,
        *,
        model_id: str,
        messages: list[_JsonDict],
        system: list[_JsonDict],
        inference_config: _JsonDict,
        guardrail: domain.GuardrailDecision | None = None,
        tool_config: _JsonDict | None = None,
    ) -> ConverseResult:
        """비스트리밍 Converse 를 호출한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.
            messages: Converse `messages`.
            system: Converse `system`. 비어 있으면 전달하지 않는다.
            inference_config: Converse `inferenceConfig`.
            guardrail: 가드레일 판정.
            tool_config: Converse `toolConfig`. 없으면 전달하지 않는다.

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
            tool_config=tool_config,
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
        output = response.get("output") or {}
        return ConverseResult(
            text=translate.extract_text(output),
            stop_reason=str(response.get("stopReason") or ""),
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
            tool_calls=translate.extract_tool_uses(output),
        )

    def converse_stream(
        self,
        *,
        model_id: str,
        messages: list[_JsonDict],
        system: list[_JsonDict],
        inference_config: _JsonDict,
        guardrail: domain.GuardrailDecision | None = None,
        tool_config: _JsonDict | None = None,
    ) -> typing.Iterator[StreamDelta]:
        """스트리밍 Converse 를 호출한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.
            messages: Converse `messages`.
            system: Converse `system`.
            inference_config: Converse `inferenceConfig`.
            guardrail: 가드레일 판정.
            tool_config: Converse `toolConfig`. 없으면 전달하지 않는다.

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
            tool_config=tool_config,
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
        """이벤트 스트림을 `StreamDelta` 로 변환한다.

        도구 호출은 세 단계로 온다. `contentBlockStart` 가 ID 와 이름을 주고,
        `contentBlockDelta` 의 `toolUse.input` 이 인자 JSON 을 조각내어 보내며,
        `contentBlockStop` 이 끝을 알린다.

        도구 순번은 **여기서 따로 센다.** Converse 의 `contentBlockIndex` 는
        텍스트 블록까지 포함해 번호를 매기므로, 텍스트가 앞에 오면 OpenAI 의
        `tool_calls[].index` 와 어긋난다.
        """
        # Bedrock 블록 인덱스 → OpenAI 도구 순번.
        tool_indexes: dict[int, int] = {}
        next_tool_index = 0
        try:
            for event in event_stream:
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    tool_use = (start.get("start") or {}).get("toolUse")
                    if not isinstance(tool_use, dict):
                        continue
                    block_index = int(start.get("contentBlockIndex") or 0)
                    tool_indexes[block_index] = next_tool_index
                    yield StreamDelta(
                        tool_index=next_tool_index,
                        tool_use_id=str(tool_use.get("toolUseId") or ""),
                        tool_name=str(tool_use.get("name") or ""),
                    )
                    next_tool_index += 1
                elif "contentBlockDelta" in event:
                    block = event["contentBlockDelta"]
                    delta = block.get("delta") or {}
                    tool_delta = delta.get("toolUse")
                    if isinstance(tool_delta, dict):
                        block_index = int(block.get("contentBlockIndex") or 0)
                        tool_index = tool_indexes.get(block_index)
                        if tool_index is None:
                            # 시작 이벤트를 못 본 도구 블록이다. 조용히 버리면
                            # 인자가 잘린 JSON 이 클라이언트로 간다.
                            raise errors.UpstreamError(
                                "도구 호출 스트림에 시작 이벤트가 없다."
                                f" (블록 {block_index})"
                            )
                        yield StreamDelta(
                            tool_index=tool_index,
                            tool_arguments_delta=str(
                                tool_delta.get("input") or ""
                            ),
                        )
                        continue
                    text = str(delta.get("text") or "")
                    if text:
                        yield StreamDelta(text=text)
                elif "contentBlockStop" in event:
                    # 블록 경계는 OpenAI 청크에 대응이 없다. 도구 인자는
                    # 누적 문자열이라 종료를 따로 알릴 필요가 없다.
                    continue
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
                    self._logger.debug(
                        "알 수 없는 Converse 스트림 이벤트를 건너뛴다",
                        extra={"event_keys": sorted(event)},
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
        tool_config: _JsonDict | None = None,
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
        if tool_config:
            params["toolConfig"] = tool_config
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
        # AWS 원본 메시지를 로그에 남긴다. 버리면 AccessDenied 가 모델 접근
        # 문제인지 가드레일 권한 문제인지 구분할 수 없다. 실제로 그것 때문에
        # 가드레일 도입 검증에서 원인을 찾지 못했다.
        #
        # 클라이언트 응답에는 넣지 않는다. AWS 메시지에 계정 ID 나 ARN 이
        # 들어올 수 있다.
        self._logger.warning(
            "Bedrock 호출이 실패했다",
            extra={
                "bedrock_error_code": code,
                "bedrock_error_message": message,
                "model_id": model_id,
            },
        )
        error_class = _ERROR_MAP.get(code, errors.UpstreamError)
        if error_class is errors.PermissionDeniedError:
            # 가드레일 관련 거부는 모델 접근 문제로 안내하면 안 된다. 운영자가
            # 콘솔에서 모델 액세스만 확인하다 원인을 놓친다.
            if "guardrail" in message.lower():
                return errors.PermissionDeniedError(
                    "가드레일에 접근할 수 없다. 태스크 역할 권한과 가드레일"
                    " 소유 계정·리전을 확인한다."
                )
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
