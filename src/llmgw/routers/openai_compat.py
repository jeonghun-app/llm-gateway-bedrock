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

from llmgw import bedrock as bedrock_module
from llmgw import domain
from llmgw import errors
from llmgw import pricing as pricing_module
from llmgw import schemas
from llmgw import services as services_module
from llmgw import translate
from llmgw.extensions import v1 as extensions_v1

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


def _reject_unsupported_content(
    payload: schemas.ChatCompletionRequest,
) -> None:
    """지원하지 않는 본문 조각이 있으면 거부한다.

    텍스트와 이미지(`image_url`)는 Converse 블록으로 변환한다. 오디오·파일
    같은 나머지 종류는 변환하지 않으므로 거부한다. 변환하지 않은 채
    통과시키면 `ChatMessage.text()` 가 텍스트만 이어붙이고 나머지를 버린다.
    보낸 클라이언트는 모델이 그것을 보고 답한 것으로 오해한 채 결과를 쓴다.
    조용히 버리는 것보다 거부하는 편이 정직하다.

    Bedrock 을 호출하기 전에 검사하므로 비용이 발생하지 않는다.

    Args:
        payload: 요청 본문.

    Raises:
        InvalidRequestError: 지원하지 않는 조각이 있는 경우.
    """
    unsupported: list[str] = []
    for message in payload.messages:
        for part_type in message.unsupported_part_types():
            if part_type not in unsupported:
                unsupported.append(part_type)
    if not unsupported:
        return
    raise errors.InvalidRequestError(
        "지원하지 않는 메시지 본문 종류다: "
        + ", ".join(sorted(unsupported))
        + ". 이 게이트웨이는 텍스트와 이미지만 Bedrock 으로 전달한다."
        " 변환하지 않는 조각을 조용히 버리면 모델이 그것을 보고 답한 것으로"
        " 오해하게 되므로 거부한다."
    )


def _reject_unfilterable_content(
    services: services_module.Services,
    payload: schemas.ChatCompletionRequest,
) -> None:
    """확장 필터가 볼 수 없는 요청을 거부한다.

    `extensions.v1` 의 요청 DTO 는 본문을 `str` 하나로 표현한다. 이미지나
    도구 호출·도구 결과가 섞인 요청은 그 형태로 표현할 수 없어, 필터에
    넘기면 **필터가 보지 못한 부분이 그대로 통과한다.**

    개인정보 마스킹 필터를 켠 운영자가 "필터가 돌았다" 는 보고를 받았는데
    실제로는 이미지 안의 주민번호를 아무도 검사하지 않은 상태가 되는 것이
    가장 나쁘다. 확장이 활성화된 동안에는 이런 요청을 거부한다.

    Args:
        services: 서비스 컨테이너.
        payload: 요청 본문.

    Raises:
        InvalidRequestError: 활성 확장이 있고 요청이 v1 계약으로 표현할 수
            없는 내용을 담은 경우.
    """
    if services.request_filters.is_empty:
        return

    reasons: list[str] = []
    if payload.tools:
        reasons.append("tools")
    if (
        payload.response_format is not None
        and payload.response_format.type == "json_schema"
    ):
        # 구조화 출력은 클라이언트가 준 JSON Schema 를 합성 도구로 만들어
        # Bedrock 에 보낸다. 스키마의 description 등은 모델이 읽는 프롬프트의
        # 일부인데, 확장은 messages 만 보므로 검사되지 않는다. 즉 필터를 켠
        # 상태에서 임의 텍스트를 모델에 밀어넣는 통로가 된다.
        reasons.append("response_format=json_schema")
    for message in payload.messages:
        if message.image_parts() and "이미지" not in reasons:
            reasons.append("이미지")
        if message.tool_calls and "tool_calls" not in reasons:
            reasons.append("tool_calls")
        if (
            message.role.strip().lower() == "tool"
            and "도구 결과" not in reasons
        ):
            reasons.append("도구 결과")
    if not reasons:
        return

    raise errors.InvalidRequestError(
        "요청 필터 확장이 활성화된 상태에서는 이 요청을 처리할 수 없다:"
        f" {', '.join(reasons)}."
        " 확장 v1 계약은 본문을 텍스트 하나로만 표현하므로 이 내용을 검사할"
        " 수 없다. 검사하지 못한 것을 통과시키면 필터를 켠 의미가 없어"
        " 거부한다. 확장을 끄거나 텍스트만 보낸다."
    )


def _enforce_pricing_policy(
    services: services_module.Services,
    model_id: str,
    principal: domain.Principal,
) -> None:
    """단가를 모르는 모델 요청을 정책에 따라 거부한다.

    단가가 없으면 비용이 0 으로 집계된다. 그 결과가 두 가지로 갈린다.

    - 예산을 쓰지 않는 주체: 보고 정확도가 떨어진다. 불편하지만 통제가
      깨지는 것은 아니다.
    - 예산을 쓰는 주체: **월 예산이 영원히 걸리지 않는다.** 설정한 상한이
      조용히 무효가 되므로 통제 우회다.

    그래서 두 경우를 다르게 다룬다. `reject` 정책은 모든 요청을 거부하고,
    기본값 `allow` 여도 **금액 예산이 걸린 주체에게는 거부한다.** 예산을
    설정한 운영자는 그것이 지켜진다고 믿을 자격이 있다.

    Args:
        services: 서비스 컨테이너.
        model_id: 요청한 모델 ID.
        principal: 인증된 요청 주체.

    Raises:
        InvalidRequestError: 단가가 없고, 정책이 `reject` 이거나 주체에게
            금액 예산이 설정된 경우.
    """
    if services.pricing.get(model_id) is not None:
        return

    if services.settings.unpriced_model_policy == "reject":
        raise errors.InvalidRequestError(
            f"이 모델의 단가가 등록되지 않아 요청을 거부한다: {model_id}."
            " 비용 귀속을 보장할 수 없기 때문이다. pricing.json 에 단가를"
            " 추가하거나 LLMGW_UNPRICED_MODEL_POLICY 를 조정한다."
        )

    if principal.has_monetary_budget:
        raise errors.InvalidRequestError(
            f"이 모델의 단가가 등록되지 않아 요청을 거부한다: {model_id}."
            " 이 키에는 월 예산이 걸려 있는데, 단가를 모르는 모델은 비용이"
            " 0 으로 집계되어 예산이 영원히 걸리지 않는다. pricing.json 에"
            " 단가를 추가하거나 예산을 해제한다."
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
        x_request_id: 클라이언트가 지정한 상관관계 ID. 응답 헤더와 모든
            로그, 사용량 레코드에 그대로 남는다. **멱등 키가 아니다.**
            Bedrock 호출은 한 번마다 실제 비용이 발생하므로 같은 값으로
            재시도하면 호출 횟수만큼 집계된다.

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
        _reject_unsupported_content(payload)
        _reject_non_chat_model(payload.model)
        _reject_unfilterable_content(services, payload)
        services.authenticator.enforce_rate_limit(principal, started_at)
        _enforce_pricing_policy(services, payload.model, principal)
        services.authenticator.enforce_model(principal, payload.model)
        services.authenticator.enforce_budget(principal, started_at)
        payload = _apply_request_filters(
            services=services,
            principal=principal,
            payload=payload,
            request_id=request_id,
            started_at=started_at,
        )
        bedrock_request = translate.to_bedrock_request(payload)
        # 가드레일은 확장이 요청을 변형한 뒤에 판정한다. 실제로 Bedrock 에
        # 보내는 내용이 검사 대상이어야 한다.
        guardrail = services.guardrails.resolve(principal)
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
                guardrail=guardrail,
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
        guardrail=guardrail,
        request_id=request_id,
        started_at=started_at,
    )


def _apply_request_filters(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    payload: schemas.ChatCompletionRequest,
    request_id: str,
    started_at: datetime.datetime,
) -> schemas.ChatCompletionRequest:
    """요청 필터 확장을 적용한 요청을 반환한다.

    확장에는 내부 스키마가 아니라 `extensions.v1` 의 불변 DTO 를 넘긴다.
    확장이 제자리에서 수정하지 못하게 하고, 내부 리팩터링이 확장을 깨뜨리지
    않게 하려는 것이다.

    확장은 모델과 스트리밍 여부를 바꿀 수 없다. 그 둘은 컨텍스트에만 있고
    반환 DTO 에는 없다. 모델을 바꿀 수 있으면 이미 통과한 모델 허용 목록과
    단가 정책 검사를 우회하게 된다.

    Args:
        services: 서비스 컨테이너.
        principal: 인증된 요청 주체.
        payload: 원본 요청.
        request_id: 상관관계 ID.
        started_at: 요청 수신 시각.

    Returns:
        확장이 반환한 값을 반영한 요청. 활성 확장이 없으면 받은 객체를
        그대로 반환한다.

    Raises:
        RequestRejectedError: 확장이 요청을 거부한 경우.
        ExtensionUnavailableError: 확장이 고장났거나 제한 시간을 넘긴 경우.
    """
    chain = services.request_filters
    if chain.is_empty:
        # 확장이 없으면 DTO 변환 비용도 들이지 않는다.
        return payload

    context = extensions_v1.RequestContext(
        principal=extensions_v1.ExtensionPrincipal(
            account_id=principal.account_id,
            team_id=principal.team_id,
            user_id=principal.user_id,
            key_id=principal.key_id,
        ),
        request_id=request_id,
        model_id=payload.model,
        started_at=started_at,
        streamed=payload.stream,
        deadline_at=started_at
        + datetime.timedelta(
            seconds=services.settings.extension_timeout_seconds
        ),
    )
    original = extensions_v1.RequestPayload(
        messages=tuple(
            extensions_v1.Message(role=message.role, content=message.text())
            for message in payload.messages
        ),
        max_tokens=payload.effective_max_tokens,
        temperature=payload.temperature,
        top_p=payload.top_p,
        stop_sequences=tuple(payload.stop_sequences),
    )
    filtered = chain.apply(original, context=context)
    if filtered == original:
        # 아무 확장도 바꾸지 않았다. 원본을 그대로 쓰면 `extra` 로 들어온
        # 필드가 보존된다.
        return payload

    # 변형된 값으로 새 요청을 만든다. model 과 stream 은 원본에서 가져온다.
    return payload.model_copy(
        update={
            "messages": [
                schemas.ChatMessage(role=item.role, content=item.content)
                for item in filtered.messages
            ],
            "max_tokens": filtered.max_tokens,
            "max_completion_tokens": None,
            "temperature": filtered.temperature,
            "top_p": filtered.top_p,
            "stop": list(filtered.stop_sequences) or None,
        }
    )


def _blocking_completion(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    payload: schemas.ChatCompletionRequest,
    bedrock_request: translate.BedrockRequest,
    guardrail: domain.GuardrailDecision,
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
            guardrail=guardrail,
            tool_config=bedrock_request.tool_config,
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

    content = result.text
    tool_calls = result.tool_calls
    finish_reason = translate.map_finish_reason(result.stop_reason)

    # 후처리를 성공 레코드보다 **먼저** 한다. 여기서 실패한 뒤에 200 레코드가
    # 남으면 대시보드는 성공으로 보이는데 클라이언트는 오류를 받는다. 에러율이
    # 과소 보고되고, 그것도 운영자가 가장 찾아야 하는 경우(모델이 강제 도구
    # 호출을 지원하지 않음)에 과소 보고된다.
    try:
        if bedrock_request.structured_tool_name is not None:
            # 구조화 출력은 강제 도구 호출로 구현했다. 합성 도구를 클라이언트에
            # 노출하지 않고, 그 입력을 본문 JSON 으로 되돌린다.
            content = translate.unwrap_structured_output(
                tool_calls, bedrock_request.structured_tool_name
            )
            tool_calls = ()
            # **도구 호출로 끝난 경우만** stop 으로 바꾼다. 무조건 덮으면
            # max_tokens 로 잘린 응답(length)이나 가드레일 개입
            # (content_filter)이 "정상 종료" 로 보고된다. 클라이언트는 잘린
            # JSON 을 완전한 결과로 읽는다.
            if finish_reason == "tool_calls":
                finish_reason = "stop"
    except errors.GatewayError as exc:
        _record_failure(
            services=services,
            principal=principal,
            request_id=request_id,
            started_at=started_at,
            model_id=payload.model,
            exc=exc,
            streamed=False,
            # 토큰은 실제로 소비됐다. 상태만 실패다.
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
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
        guardrail=guardrail,
        stop_reason=result.stop_reason,
    )
    services.recorder.persist(record, key_hash=principal.key_hash)

    return translate.build_completion_response(
        completion_id=f"chatcmpl-{request_id}",
        created_unix=int(started_at.timestamp()),
        model_id=payload.model,
        content=content,
        finish_reason=finish_reason,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        tool_calls=tool_calls,
    )


def _stream_completion(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    payload: schemas.ChatCompletionRequest,
    bedrock_request: translate.BedrockRequest,
    guardrail: domain.GuardrailDecision,
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
            guardrail=guardrail,
            tool_config=bedrock_request.tool_config,
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
            if delta.tool_index is not None:
                # 도구 블록 시작이면 id·name 을 실어 보내고, 이후 증분은
                # arguments 조각만 보낸다. OpenAI 클라이언트가 index 로
                # 병렬 도구 호출을 구분한다.
                tool_delta: dict[str, typing.Any] = {"index": delta.tool_index}
                if delta.tool_use_id:
                    tool_delta["id"] = delta.tool_use_id
                    tool_delta["type"] = "function"
                    tool_delta["function"] = {
                        "name": delta.tool_name,
                        "arguments": "",
                    }
                else:
                    tool_delta["function"] = {
                        "arguments": delta.tool_arguments_delta
                    }
                yield _sse(
                    translate.build_chunk(
                        completion_id=completion_id,
                        created_unix=created_unix,
                        model_id=payload.model,
                        delta={"tool_calls": [tool_delta]},
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
            guardrail=guardrail,
            stop_reason=stop_reason,
        )
        services.recorder.persist(record, key_hash=principal.key_hash)


def _reject_unfilterable_embedding(
    services: services_module.Services,
) -> None:
    """확장이 켜진 상태의 임베딩 요청을 거부한다.

    확장 v1 계약(`extensions.v1.RequestPayload`)은 **채팅 메시지만** 표현한다.
    임베딩 입력을 넘길 자리가 없어, 이 엔드포인트는 필터를 거치지 않고 곧장
    Bedrock 으로 간다.

    개인정보 마스킹 확장을 켠 운영자는 모든 요청이 검사된다고 믿는다. 검사되지
    않는 엔드포인트를 조용히 열어 두면, 없는 것보다 나쁘다 — 없으면 위험을
    알지만 있으면 안전하다고 오해한다. 그래서 거부한다.

    Args:
        services: 서비스 컨테이너.

    Raises:
        InvalidRequestError: 활성 확장이 있는 경우.
    """
    if services.request_filters.is_empty:
        return
    raise errors.InvalidRequestError(
        "요청 필터 확장이 활성화된 상태에서는 /v1/embeddings 를 처리할 수"
        " 없다. 확장 v1 계약은 채팅 메시지만 표현하므로 임베딩 입력을 검사할"
        " 수 없고, 검사하지 못한 것을 통과시키면 필터를 켠 의미가 없다."
        " 확장을 끄거나 임베딩을 쓰지 않는다."
    )


def _reject_non_chat_model(model_id: str) -> None:
    """채팅으로 호출할 수 없는 모델을 명확한 메시지로 거부한다.

    임베딩 모델을 `/v1/chat/completions` 로 보내면 Bedrock 이
    "This action doesn't support the model" 로 400 을 낸다. 그 메시지만으로는
    어디로 보내야 하는지 알 수 없다. 게이트웨이가 먼저 안내한다.

    Args:
        model_id: 요청 모델 ID.

    Raises:
        InvalidRequestError: Converse 로 호출할 수 없는 계열인 경우.
    """
    if bedrock_module.supports_converse(model_id):
        return
    if bedrock_module.embedding_family(model_id) is not None:
        raise errors.InvalidRequestError(
            f"이 모델은 임베딩 모델이다: {model_id}."
            " 채팅이 아니라 POST /v1/embeddings 로 호출한다."
        )
    raise errors.InvalidRequestError(
        f"이 모델은 채팅으로 호출할 수 없다: {model_id}."
        " 임베딩·재순위·이미지 생성 모델은 Converse 를 지원하지 않는다."
    )


def _record_failure(
    *,
    services: services_module.Services,
    principal: domain.Principal,
    request_id: str,
    started_at: datetime.datetime,
    model_id: str,
    exc: errors.GatewayError,
    streamed: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """실패한 요청을 사용량에 기록한다.

    Args:
        services: 서비스 컨테이너.
        principal: 인증된 요청 주체.
        request_id: 상관관계 ID.
        started_at: 요청 시작 시각.
        model_id: 요청 모델 ID.
        exc: 발생한 오류.
        streamed: 스트리밍 요청이었는지 여부.
        input_tokens: 실패 전에 이미 소비된 입력 토큰 수. 배치 요청이
            중간에 실패했거나, 업스트림 응답을 받은 뒤 후처리에서 실패한
            경우 **0 이 아니다.** 실제로 청구된 토큰을 0 으로 기록하면
            비용이 집계에서 사라져 예산이 조용히 무효가 된다.
        output_tokens: 같은 이유로 이미 소비된 출력 토큰 수.
    """
    record = services.recorder.build_record(
        principal=principal,
        request_id=request_id,
        started_at=started_at,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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


# ---------------------------------------------------------------------------
# 임베딩
# ---------------------------------------------------------------------------

# 한 요청에 담을 수 있는 텍스트 수 상한. Titan 은 호출당 텍스트 하나만 받아
# 배치가 그대로 호출 수가 된다. 상한이 없으면 한 요청이 태스크를 오래 점유해
# 다른 요청의 지연으로 번진다.
_MAX_EMBEDDING_BATCH = 96


@router.post("/embeddings")
def embeddings(
    payload: schemas.EmbeddingRequest,
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
    x_request_id: typing.Annotated[
        str | None, fastapi.Header(alias="X-Request-Id")
    ] = None,
) -> dict[str, typing.Any]:
    """텍스트 임베딩을 생성한다.

    채팅과 같은 검사를 모두 통과한다. 인증 → 레이트리밋 → 단가 정책 →
    모델 허용 목록 → 예산 순이다. 임베딩만 예외로 두면 예산을 우회하는
    경로가 된다.

    가드레일은 붙이지 않는다. 임베딩은 생성 응답이 없어 검사 대상이 없고,
    Converse 경로가 아니라 `guardrailConfig` 를 실을 자리도 없다.

    Args:
        payload: OpenAI 형식 요청 본문.
        services: 서비스 컨테이너.
        authorization: `Bearer <api-key>` 헤더.
        x_request_id: 클라이언트가 지정한 상관관계 ID.

    Returns:
        OpenAI 형식의 임베딩 응답.

    Raises:
        GatewayError: 인증·권한·예산·업스트림 오류가 발생한 경우.
    """
    started_at = services.clock.now()
    request_id = (x_request_id or "").strip() or services.id_factory.new_id()
    principal = services.authenticator.authenticate(authorization)

    try:
        texts = _embedding_texts(payload)
        _reject_unfilterable_embedding(services)
        services.authenticator.enforce_rate_limit(principal, started_at)
        _enforce_pricing_policy(services, payload.model, principal)
        services.authenticator.enforce_model(principal, payload.model)
        services.authenticator.enforce_budget(principal, started_at)
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

    try:
        result = services.bedrock.embed(
            model_id=payload.model,
            texts=texts,
            dimensions=payload.dimensions,
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
            # 배치가 중간에 실패했으면 앞선 호출은 이미 청구됐다.
            input_tokens=exc.consumed_input_tokens,
        )
        raise

    # 응답을 먼저 만든다. 직렬화가 실패한 뒤에 성공 레코드가 남으면
    # 대시보드는 200 으로 보이는데 클라이언트는 오류를 받는다.
    try:
        body = translate.build_embedding_response(
            model_id=payload.model,
            vectors=result.vectors,
            input_tokens=result.input_tokens,
            base64_encoding=payload.encoding_format == "base64",
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
            input_tokens=result.input_tokens,
        )
        raise

    # 출력 토큰이 없는 것이 정상이다. pricing.json 의 임베딩 모델은
    # output_per_1k_usd 가 0 이라 비용 계산이 그대로 정확하다.
    record = services.recorder.build_record(
        principal=principal,
        request_id=request_id,
        started_at=started_at,
        model_id=payload.model,
        input_tokens=result.input_tokens,
        output_tokens=0,
        latency_ms=_elapsed_ms(services, started_at),
        status_code=200,
        streamed=False,
    )
    services.recorder.persist(record, key_hash=principal.key_hash)

    return body


def _embedding_texts(payload: schemas.EmbeddingRequest) -> list[str]:
    """임베딩 입력을 검증해 텍스트 목록으로 만든다.

    Args:
        payload: 요청 본문.

    Returns:
        임베딩할 텍스트 목록.

    Raises:
        InvalidRequestError: 입력이 비었거나 배치 상한을 넘은 경우.
    """
    if isinstance(payload.input, list) and any(
        not isinstance(item, str) for item in payload.input
    ):
        # OpenAI 는 토큰 ID 배열도 허용하지만 Bedrock 은 텍스트만 받는다.
        # 역토큰화를 추측으로 하면 청구 대상 입력이 달라진다.
        raise errors.InvalidRequestError(
            "input 은 문자열 또는 문자열 배열이어야 한다. 토큰 ID 배열은"
            " Bedrock 이 받지 않아 지원하지 않는다."
        )
    try:
        texts = payload.texts()
    except ValueError as exc:
        raise errors.InvalidRequestError(str(exc)) from exc
    if len(texts) > _MAX_EMBEDDING_BATCH:
        raise errors.InvalidRequestError(
            f"한 요청의 입력 수 상한은 {_MAX_EMBEDDING_BATCH} 개다:"
            f" {len(texts)} 개를 보냈다."
        )
    return texts
