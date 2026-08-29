"""OpenAI 호환 라우터.

핸들러를 `async def` 가 아니라 `def` 로 정의한 것은 의도적이다. boto3 는
동기 라이브러리라서 `async def` 안에서 호출하면 이벤트 루프를 막는다.
동기 핸들러는 Starlette 이 스레드풀에서 실행하므로 다른 요청이 함께
진행된다. 스트리밍 제너레이터도 같은 이유로 동기 제너레이터다.

사용량 기록 정책
----------------
인증 실패(401)는 호출 주체를 특정할 수 없어 사용량을 남기지 않는다.
인증 이후의 모든 실패(403/429/4xx/5xx)는 주체가 확정되어 있으므로 실패
요청으로 집계한다. 그래야 대시보드의 에러율이 실제 사용 경험을 반영한다.
"""

from __future__ import annotations

import datetime
import json
import typing

import fastapi
from fastapi import responses

from llmgw import domain
from llmgw import errors
from llmgw import pricing as pricing_module
from llmgw import schemas
from llmgw import services as services_module
from llmgw import translate

router = fastapi.APIRouter(prefix="/v1", tags=["openai-compat"])

_REQUEST_ID_HEADER = "X-Request-Id"
_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_DONE = "data: [DONE]\n\n"

# 스트리밍 응답에는 상태 코드를 나중에 바꿀 수 없다. 스트림 시작 후 발생한
# 오류는 이 코드로 사용량에 기록한다.
_STREAM_ERROR_STATUS = 500


def _sse(payload: dict[str, typing.Any]) -> str:
    """딕셔너리를 SSE 데이터 프레임으로 만든다.

    Args:
        payload: 직렬화할 페이로드.

    Returns:
        `data: {...}\\n\\n` 형태의 문자열.
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _visible_models(services: services_module.Services) -> tuple[str, ...]:
    """정책에 따라 노출할 모델 목록을 만든다.

    `hide` 정책이면 단가 표에 없는 모델을 목록에서 뺀다. 클라이언트가 그것을
    고르면 비용이 0으로 집계되어 청구 배분과 예산 검사가 어긋난다. 감추기만
    하고 명시적 호출은 막지 않는 이유는, 새 모델을 급히 써야 하는 상황을
    완전히 봉쇄하지 않기 위해서다. 봉쇄가 필요하면 `reject` 를 쓴다.

    Args:
        services: 서비스 컨테이너.

    Returns:
        노출할 모델 ID 목록.
    """
    available = services.bedrock.list_model_ids()
    if services.settings.unpriced_model_policy != "hide":
        return tuple(available)
    known = set(services.pricing.known_model_ids())
    return tuple(
        model_id
        for model_id in available
        if pricing_module.normalize_model_id(model_id) in known
    )


def _enforce_pricing_policy(
    services: services_module.Services, model_id: str
) -> None:
    """단가를 모르는 모델 요청을 정책에 따라 거부한다.

    `reject` 정책은 비용 귀속을 보장한다. 단가가 없으면 비용이 0으로
    집계되고, 그러면 월 예산이 영원히 걸리지 않으며 청구 배분에서도 빠진다.
    그 상태를 허용할 수 없는 조직을 위한 선택지다.

    Args:
        services: 서비스 컨테이너.
        model_id: 요청한 모델 ID.

    Raises:
        InvalidRequestError: `reject` 정책이고 단가가 없는 경우.
    """
    if services.settings.unpriced_model_policy != "reject":
        return
    if services.pricing.get(model_id) is not None:
        return
    raise errors.InvalidRequestError(
        f"이 모델의 단가가 등록되지 않아 요청을 거부한다: {model_id}."
        " 비용 귀속을 보장할 수 없기 때문이다. pricing.json 에 단가를"
        " 추가하거나 LLMGW_UNPRICED_MODEL_POLICY 를 조정한다."
    )


@router.get("/models")
def list_models(
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> dict[str, typing.Any]:
    """호출 가능한 모델 목록을 OpenAI 형식으로 반환한다.

    키에 허용 모델 목록이 설정돼 있으면 그 목록으로 제한한다. 클라이언트가
    쓸 수 없는 모델을 보여주면 선택 후 403 을 받게 되어 혼란스럽다.

    Args:
        services: 서비스 컨테이너.
        authorization: `Bearer <api-key>` 헤더.

    Returns:
        OpenAI 형식의 모델 목록.

    Raises:
        AuthenticationError: API 키가 유효하지 않은 경우.
    """
    principal = services.authenticator.authenticate(authorization)
    available = _visible_models(services)
    if not principal.allowed_models:
        return translate.build_model_list(available)

    permitted = {
        pricing_module.normalize_model_id(model_id)
        for model_id in principal.allowed_models
    }
    # Bedrock 이 실제로 노출하는 ID 를 그대로 돌려주되, 허용 목록에 없는
    # 것만 걸러낸다. 허용 목록에 있지만 리전에 없는 모델은 보여주지 않는다.
    filtered = [
        model_id
        for model_id in available
        if pricing_module.normalize_model_id(model_id) in permitted
    ]
    return translate.build_model_list(filtered)


@router.post("/chat/completions")
def chat_completions(
    payload: schemas.ChatCompletionRequest,
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
    x_request_id: typing.Annotated[
        str | None, fastapi.Header(alias=_REQUEST_ID_HEADER)
    ] = None,
) -> typing.Any:
    """채팅 완성을 수행한다.

    Args:
        payload: OpenAI 형식 요청 본문.
        services: 서비스 컨테이너.
        authorization: `Bearer <api-key>` 헤더.
        x_request_id: 클라이언트가 지정한 요청 ID. 재시도 시 같은 값을
            보내면 사용량이 중복 집계되지 않는다.

    Returns:
        비스트리밍이면 OpenAI 형식 응답 딕셔너리, 스트리밍이면
        `StreamingResponse`.

    Raises:
        GatewayError: 인증·권한·예산·업스트림 오류가 발생한 경우.
    """
    started_at = services.clock.now()
    request_id = (x_request_id or "").strip() or services.id_factory.new_id()

    # 401 은 주체를 알 수 없어 사용량을 남길 수 없다. 예외를 그대로 올린다.
    principal = services.authenticator.authenticate(authorization)

    try:
        services.authenticator.enforce_rate_limit(principal, started_at)
        _enforce_pricing_policy(services, payload.model)
        services.authenticator.enforce_model(principal, payload.model)
        services.authenticator.enforce_budget(principal, started_at)
        bedrock_request = translate.to_bedrock_request(payload)
    except errors.GatewayError as exc:
        _record_failure(
            services=services,
            principal=principal,
            request_id=request_id,
            started_at=started_at,
            model_id=payload.model,
            exc=exc,
            streamed=payload.stream,
        )
        raise

    if payload.stream:
        return responses.StreamingResponse(
            _stream_completion(
                services=services,
                principal=principal,
                payload=payload,
                bedrock_request=bedrock_request,
                request_id=request_id,
                started_at=started_at,
            ),
            media_type=_SSE_MEDIA_TYPE,
            headers={
                _REQUEST_ID_HEADER: request_id,
                # 프록시가 SSE 를 버퍼링하면 증분이 한꺼번에 도착한다.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return _blocking_completion(
        services=services,
        principal=principal,
        payload=payload,
        bedrock_request=bedrock_request,
        request_id=request_id,
        started_at=started_at,
    )


def _blocking_completion(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    payload: schemas.ChatCompletionRequest,
    bedrock_request: translate.BedrockRequest,
    request_id: str,
    started_at: datetime.datetime,
) -> dict[str, typing.Any]:
    """비스트리밍 요청을 처리하고 사용량을 기록한다."""
    try:
        result = services.bedrock.converse(
            model_id=payload.model,
            messages=bedrock_request.messages,
            system=bedrock_request.system,
            inference_config=bedrock_request.inference_config,
        )
    except errors.GatewayError as exc:
        _record_failure(
            services=services,
            principal=principal,
            request_id=request_id,
            started_at=started_at,
            model_id=payload.model,
            exc=exc,
            streamed=False,
        )
        raise

    record = services.recorder.build_record(
        principal=principal,
        request_id=request_id,
        started_at=started_at,
        model_id=payload.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=_elapsed_ms(services, started_at),
        status_code=200,
        streamed=False,
    )
    services.recorder.persist(record, key_hash=principal.key_hash)

    return translate.build_completion_response(
        completion_id=f"chatcmpl-{request_id}",
        created_unix=int(started_at.timestamp()),
        model_id=payload.model,
        content=result.text,
        finish_reason=translate.map_finish_reason(result.stop_reason),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _stream_completion(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    payload: schemas.ChatCompletionRequest,
    bedrock_request: translate.BedrockRequest,
    request_id: str,
    started_at: datetime.datetime,
) -> typing.Iterator[str]:
    """스트리밍 응답을 만들고, 스트림이 끝나면 사용량을 기록한다.

    사용량 기록을 `finally` 에 둔 이유는 클라이언트가 중간에 연결을 끊어도
    그때까지 발생한 토큰 비용을 집계에 남기기 위해서다.
    """
    completion_id = f"chatcmpl-{request_id}"
    created_unix = int(started_at.timestamp())
    input_tokens = 0
    output_tokens = 0
    stop_reason = ""
    status_code = 200
    error_code = ""

    try:
        yield _sse(
            translate.build_chunk(
                completion_id=completion_id,
                created_unix=created_unix,
                model_id=payload.model,
                delta={"role": "assistant", "content": ""},
            )
        )
        for delta in services.bedrock.converse_stream(
            model_id=payload.model,
            messages=bedrock_request.messages,
            system=bedrock_request.system,
            inference_config=bedrock_request.inference_config,
        ):
            if delta.text:
                yield _sse(
                    translate.build_chunk(
                        completion_id=completion_id,
                        created_unix=created_unix,
                        model_id=payload.model,
                        delta={"content": delta.text},
                    )
                )
            if delta.stop_reason:
                stop_reason = delta.stop_reason
            if delta.is_final:
                input_tokens = delta.input_tokens
                output_tokens = delta.output_tokens

        yield _sse(
            translate.build_chunk(
                completion_id=completion_id,
                created_unix=created_unix,
                model_id=payload.model,
                delta={},
                finish_reason=translate.map_finish_reason(stop_reason),
                usage={
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            )
        )
        yield _SSE_DONE
    except errors.GatewayError as exc:
        # 이미 200 헤더를 보냈으므로 상태 코드를 바꿀 수 없다. SSE 본문에
        # 오류를 실어 클라이언트가 인지하게 한다.
        status_code = exc.status_code
        error_code = exc.code
        services.logger.warning(
            "스트리밍 중 오류가 발생했다",
            extra={"request_id": request_id, "error_code": exc.code},
        )
        yield _sse(exc.to_payload())
        yield _SSE_DONE
    except Exception as exc:  # noqa: BLE001 - 스트림 유실을 막는 최후 방어
        status_code = _STREAM_ERROR_STATUS
        error_code = "internal_error"
        services.logger.exception(
            "스트리밍 중 예상하지 못한 오류가 발생했다",
            extra={"request_id": request_id},
        )
        yield _sse(errors.GatewayError(str(exc)).to_payload())
        yield _SSE_DONE
    finally:
        record = services.recorder.build_record(
            principal=principal,
            request_id=request_id,
            started_at=started_at,
            model_id=payload.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=_elapsed_ms(services, started_at),
            status_code=status_code,
            error_code=error_code,
            streamed=True,
        )
        services.recorder.persist(record, key_hash=principal.key_hash)


def _record_failure(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    request_id: str,
    started_at: datetime.datetime,
    model_id: str,
    exc: errors.GatewayError,
    streamed: bool,
) -> None:
    """실패한 요청을 사용량에 기록한다."""
    record = services.recorder.build_record(
        principal=principal,
        request_id=request_id,
        started_at=started_at,
        model_id=model_id,
        input_tokens=0,
        output_tokens=0,
        latency_ms=_elapsed_ms(services, started_at),
        status_code=exc.status_code,
        error_code=exc.code,
        streamed=streamed,
    )
    services.recorder.persist(record, key_hash=principal.key_hash)


def _elapsed_ms(
    services: services_module.Services, started_at: datetime.datetime
) -> int:
    """요청 시작부터 지금까지 경과한 밀리초를 계산한다."""
    delta = services.clock.now() - started_at
    return max(int(delta.total_seconds() * 1000), 0)
