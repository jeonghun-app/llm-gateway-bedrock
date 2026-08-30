"""OpenAI 스펙과 Bedrock Converse 사이의 변환.

모두 순수 함수다. AWS 호출이나 시간·난수 의존이 없어 단위 테스트가 쉽다.

Bedrock Converse 는 OpenAI 와 세 가지 규칙이 다르다.

1. 시스템 프롬프트는 `messages` 가 아니라 별도 `system` 파라미터로 넣는다.
2. `messages` 는 user 로 시작해 user/assistant 가 번갈아 나와야 한다.
3. 각 메시지 본문은 `[{"text": ...}]` 형태의 콘텐츠 블록 배열이다.

OpenAI 클라이언트는 같은 역할 메시지를 연달아 보내는 경우가 흔하다.
그대로 넘기면 Bedrock 이 ValidationException 을 던지므로 여기서 병합한다.
"""

from __future__ import annotations

import typing

from llmgw import errors
from llmgw import pricing
from llmgw import schemas

# OpenAI 의 developer 역할은 system 과 같은 의미로 도입됐다.
_SYSTEM_ROLES = frozenset({"system", "developer"})
_USER_ROLE = "user"
_ASSISTANT_ROLE = "assistant"

# Bedrock stopReason → OpenAI finish_reason 매핑.
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
    # 컨텍스트 창을 넘겨 잘린 응답이다. OpenAI 의미로는 length 다. 기본값인
    # stop 으로 두면 클라이언트가 정상 완료로 읽어 잘린 답을 그대로 쓴다.
    "model_context_window_exceeded": "length",
    # 모델이 형식을 어긴 출력을 냈다. OpenAI 스펙에 대응하는 값이 없다.
    # stop 은 "정상적으로 끝났다" 는 뜻이라 정확하지 않지만, length 나
    # content_filter 는 더 틀리다. 명시적으로 적어 두는 이유는 이 값이
    # 기본값으로 흘러들어간 것이 아니라 검토한 결과임을 남기기 위해서다.
    # 클라이언트가 구분할 수 없으므로 운영자는 로그와 사용량 레코드의
    # stop_reason 으로 봐야 한다.
    "malformed_model_output": "stop",
    "malformed_tool_use": "stop",
}

_DEFAULT_FINISH_REASON = "stop"

_JsonDict = dict[str, typing.Any]


class BedrockRequest(typing.NamedTuple):
    """Bedrock Converse 호출 인자 묶음.

    Attributes:
        messages: Converse `messages` 파라미터.
        system: Converse `system` 파라미터. 비어 있으면 전달하지 않는다.
        inference_config: Converse `inferenceConfig` 파라미터.
    """

    messages: list[_JsonDict]
    system: list[_JsonDict]
    inference_config: _JsonDict


def to_bedrock_request(
    request: schemas.ChatCompletionRequest,
) -> BedrockRequest:
    """OpenAI 요청을 Bedrock Converse 인자로 변환한다.

    Args:
        request: 검증된 OpenAI 형식 요청.

    Returns:
        Converse 호출에 바로 넘길 수 있는 인자 묶음.

    Raises:
        InvalidRequestError: 시스템 메시지를 제외한 대화가 비어 있거나,
            대화가 assistant 로 시작하는 경우.
    """
    system_texts: list[str] = []
    conversation: list[tuple[str, str]] = []

    for message in request.messages:
        role = message.role.strip().lower()
        text = message.text()
        if role in _SYSTEM_ROLES:
            if text:
                system_texts.append(text)
            continue
        if role not in (_USER_ROLE, _ASSISTANT_ROLE):
            raise errors.InvalidRequestError(
                f"지원하지 않는 메시지 역할이다: {message.role}"
            )
        # 빈 콘텐츠 블록은 Bedrock 이 거부하므로 공백만 있는 메시지는
        # 대화에서 제외한다.
        if not text.strip():
            continue
        conversation.append((role, text))

    if not conversation:
        raise errors.InvalidRequestError(
            "user 또는 assistant 메시지가 최소 한 건 필요하다."
        )
    if conversation[0][0] != _USER_ROLE:
        raise errors.InvalidRequestError("대화는 user 메시지로 시작해야 한다.")

    merged = _merge_adjacent_roles(conversation)
    messages = [
        {"role": role, "content": [{"text": text}]} for role, text in merged
    ]

    inference_config: _JsonDict = {}
    if request.effective_max_tokens is not None:
        inference_config["maxTokens"] = request.effective_max_tokens
    if request.temperature is not None:
        inference_config["temperature"] = request.temperature
    if request.top_p is not None:
        inference_config["topP"] = request.top_p
    stop_sequences = request.stop_sequences
    if stop_sequences:
        inference_config["stopSequences"] = stop_sequences

    system = [{"text": text} for text in system_texts]
    return BedrockRequest(
        messages=messages,
        system=system,
        inference_config=inference_config,
    )


def _merge_adjacent_roles(
    conversation: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """같은 역할이 연속된 메시지를 하나로 합친다.

    Args:
        conversation: (역할, 텍스트) 순서 목록.

    Returns:
        역할이 번갈아 나오도록 병합된 목록.
    """
    merged: list[tuple[str, str]] = []
    for role, text in conversation:
        if merged and merged[-1][0] == role:
            previous_role, previous_text = merged[-1]
            merged[-1] = (previous_role, f"{previous_text}\n{text}")
        else:
            merged.append((role, text))
    return merged


def map_finish_reason(stop_reason: str | None) -> str:
    """Bedrock `stopReason` 을 OpenAI `finish_reason` 으로 바꾼다.

    Args:
        stop_reason: Bedrock 이 반환한 정지 이유.

    Returns:
        OpenAI 규약의 finish_reason. 모르는 값은 `stop` 으로 둔다.
    """
    if not stop_reason:
        return _DEFAULT_FINISH_REASON
    return _FINISH_REASONS.get(stop_reason, _DEFAULT_FINISH_REASON)


def extract_text(bedrock_output: _JsonDict) -> str:
    """Converse 응답에서 어시스턴트 텍스트를 추출한다.

    Args:
        bedrock_output: Converse 응답의 `output` 값.

    Returns:
        텍스트 블록을 이어붙인 문자열. 텍스트 블록이 없으면 빈 문자열.
    """
    message = bedrock_output.get("message") or {}
    blocks = message.get("content") or []
    return "".join(
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and "text" in block
    )


def build_completion_response(
    *,
    completion_id: str,
    created_unix: int,
    model_id: str,
    content: str,
    finish_reason: str,
    input_tokens: int,
    output_tokens: int,
) -> _JsonDict:
    """OpenAI 형식의 비스트리밍 응답 본문을 만든다.

    Args:
        completion_id: `chatcmpl-` 로 시작하는 응답 ID.
        created_unix: 생성 시각(유닉스 초).
        model_id: 요청 모델 ID.
        content: 어시스턴트 응답 텍스트.
        finish_reason: OpenAI finish_reason.
        input_tokens: 입력 토큰 수.
        output_tokens: 출력 토큰 수.

    Returns:
        직렬화 가능한 응답 딕셔너리.
    """
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_unix,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": _ASSISTANT_ROLE, "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def build_chunk(
    *,
    completion_id: str,
    created_unix: int,
    model_id: str,
    delta: _JsonDict,
    finish_reason: str | None = None,
    usage: _JsonDict | None = None,
) -> _JsonDict:
    """OpenAI 형식의 스트리밍 청크를 만든다.

    Args:
        completion_id: 응답 ID. 한 스트림 안에서 동일해야 한다.
        created_unix: 생성 시각(유닉스 초).
        model_id: 요청 모델 ID.
        delta: 이번 청크의 증분. 첫 청크는 `{"role": "assistant"}`.
        finish_reason: 마지막 청크에만 채운다.
        usage: 토큰 사용량. `stream_options.include_usage` 대응으로 마지막
            청크에만 채운다.

    Returns:
        직렬화 가능한 청크 딕셔너리.
    """
    chunk: _JsonDict = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_unix,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def build_model_list(model_ids: typing.Sequence[str]) -> _JsonDict:
    """`GET /v1/models` 응답을 만든다.

    Args:
        model_ids: 노출할 모델 ID 목록.

    Returns:
        OpenAI 형식의 모델 목록 응답.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                # OpenAI 스펙의 필수 필드다. Bedrock 은 모델 생성 시각을
                # 제공하지 않아 0으로 채운다.
                "created": 0,
                "owned_by": _model_owner(model_id),
            }
            for model_id in model_ids
        ],
    }


def _model_owner(model_id: str) -> str:
    """모델 ID 에서 공급자를 뽑는다.

    추론 프로파일 ID 는 `us.amazon.nova-lite-v1:0` 처럼 리전 접두어가 앞에
    붙는다. 첫 조각을 그대로 쓰면 공급자가 `us`/`global` 로 잡혀, 공급자별
    그룹화가 Amazon·Anthropic 모델을 리전 이름으로 분류한다. 접두어를 먼저
    제거한 뒤 공급자를 계산한다.

    Args:
        model_id: 모델 ID 또는 추론 프로파일 ID.

    Returns:
        공급자 이름. 판별할 수 없으면 빈 문자열.
    """
    normalized = pricing.normalize_model_id(model_id)
    if not normalized:
        return ""
    return normalized.split(".", 1)[0]
