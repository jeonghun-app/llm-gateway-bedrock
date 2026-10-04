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

import base64
import binascii
import json
import re
import struct
import typing

from llmgw import errors
from llmgw import pricing
from llmgw import schemas

# OpenAI 의 developer 역할은 system 과 같은 의미로 도입됐다.
_SYSTEM_ROLES = frozenset({"system", "developer"})
_USER_ROLE = "user"
_ASSISTANT_ROLE = "assistant"
_TOOL_ROLE = "tool"

# Converse 이미지 블록이 받는 형식.
_SUPPORTED_IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "webp"})

# 게이트웨이가 표현할 수 있는 response_format.type 값.
_SUPPORTED_RESPONSE_FORMATS = frozenset({"text", "json_schema"})

# 이미지 하나의 디코딩 후 크기 상한. base64 는 원본보다 약 33% 크고,
# 태스크 메모리가 1 GiB 라 동시 요청이 큰 이미지를 버퍼링하면 OOM 이 난다.
MAX_IMAGE_BYTES = 4_500_000

# 요청 하나의 이미지 총량과 개수 상한. 개별 상한만으로는 4.4MB × 50장이
# 통과해 220MB 를 할당한다.
MAX_TOTAL_IMAGE_BYTES = 18_000_000
MAX_IMAGES_PER_REQUEST = 8

_DATA_URL_PATTERN = re.compile(
    r"^data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<payload>.*)$",
    re.DOTALL,
)

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
        tool_config: Converse `toolConfig` 파라미터. 없으면 `None`.
        structured_tool_name: `response_format` 을 강제 도구로 구현할 때 쓴
            합성 도구 이름. 응답에서 이 도구의 입력을 본문으로 되돌린다.
            구조화 출력 요청이 아니면 `None`.
    """

    messages: list[_JsonDict]
    system: list[_JsonDict]
    inference_config: _JsonDict
    tool_config: _JsonDict | None = None
    structured_tool_name: str | None = None


class ToolUse(typing.NamedTuple):
    """Converse 응답의 도구 호출 하나.

    Attributes:
        tool_use_id: Bedrock 이 부여한 도구 호출 ID.
        name: 호출할 도구 이름.
        arguments: 인자를 직렬화한 JSON 문자열.
    """

    tool_use_id: str
    name: str
    arguments: str


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
            대화가 assistant 로 시작하거나, 변환할 수 없는 필드가 있는 경우.
    """
    system_texts: list[str] = []
    conversation: list[tuple[str, list[_JsonDict]]] = []
    # 이미지 총량은 요청 단위로 센다. 개별 상한만으로는 막히지 않는다.
    image_budget = _ImageBudget()

    for message in request.messages:
        role = message.role.strip().lower()
        if role in _SYSTEM_ROLES:
            # Converse 의 system 파라미터는 텍스트 블록만 받는다. 이미지를
            # 조용히 버리면 보낸 사람은 모델이 그것을 보고 답한 것으로
            # 오해한다. 거부하는 편이 정직하다.
            if message.image_parts():
                raise errors.InvalidRequestError(
                    "system/developer 메시지에는 이미지를 넣을 수 없다."
                    " Converse 의 system 파라미터는 텍스트만 받는다."
                    " 이미지는 user 메시지로 보낸다."
                )
            text = message.text()
            if text:
                system_texts.append(text)
            continue
        if role == _TOOL_ROLE:
            # 도구 결과는 Converse 에서 user 턴의 toolResult 블록이다.
            conversation.append((_USER_ROLE, [_tool_result_block(message)]))
            continue
        if role not in (_USER_ROLE, _ASSISTANT_ROLE):
            raise errors.InvalidRequestError(
                f"지원하지 않는 메시지 역할이다: {message.role}"
            )
        blocks = _message_blocks(message, image_budget)
        # 빈 콘텐츠 블록은 Bedrock 이 거부하므로 내용이 없는 메시지는
        # 대화에서 제외한다.
        if not blocks:
            continue
        conversation.append((role, blocks))

    if not conversation:
        raise errors.InvalidRequestError(
            "user 또는 assistant 메시지가 최소 한 건 필요하다."
        )
    if conversation[0][0] != _USER_ROLE:
        raise errors.InvalidRequestError("대화는 user 메시지로 시작해야 한다.")

    merged = _merge_adjacent_roles(conversation)
    messages = [{"role": role, "content": blocks} for role, blocks in merged]

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

    tool_config, structured_tool_name = _build_tool_config(request)

    system = [{"text": text} for text in system_texts]
    return BedrockRequest(
        messages=messages,
        system=system,
        inference_config=inference_config,
        tool_config=tool_config,
        structured_tool_name=structured_tool_name,
    )


def _message_blocks(
    message: schemas.ChatMessage, budget: _ImageBudget
) -> list[_JsonDict]:
    """OpenAI 메시지 하나를 Converse 콘텐츠 블록 목록으로 만든다.

    Args:
        message: user 또는 assistant 메시지.

    Returns:
        Converse 콘텐츠 블록 목록. 내용이 없으면 빈 목록.

    Raises:
        InvalidRequestError: 이미지 데이터나 도구 호출 인자가 잘못된 경우.
    """
    blocks: list[_JsonDict] = []

    if isinstance(message.content, str):
        if message.content.strip():
            blocks.append({"text": message.content})
    elif isinstance(message.content, list):
        for part in message.content:
            if part.is_text():
                if not part.text.strip():
                    continue
                # 같은 메시지 안의 연속된 텍스트 조각은 하나로 이어붙인다.
                # OpenAI 클라이언트가 문단을 조각으로 쪼개 보내는 경우가
                # 흔하고, 블록을 그대로 늘리면 모델이 보는 프롬프트가
                # 달라진다.
                if blocks and _is_text_block(blocks[-1]):
                    blocks[-1] = {"text": f"{blocks[-1]['text']}\n{part.text}"}
                else:
                    blocks.append({"text": part.text})
            elif part.is_image():
                # 단축 평가로 넘기면 image_url 이 없는 조각이 조용히 사라진다.
                if part.image_url is None:
                    raise errors.InvalidRequestError(
                        "image_url 조각에 image_url 객체가 없다."
                    )
                blocks.append(_image_block(part.image_url.url, budget))

    for tool_call in message.tool_calls or []:
        blocks.append(_tool_use_block(tool_call))

    return blocks


def _image_block(url: str, budget: _ImageBudget) -> _JsonDict:
    """데이터 URL 을 Converse 이미지 블록으로 만든다.

    **원격 URL 은 거부한다.** 게이트웨이가 클라이언트가 준 URL 을 직접
    가져오면, Bedrock 호출 권한을 가진 태스크에서 임의 주소로 요청을 보내는
    통로가 된다(SSRF). VPC 내부 주소와 인스턴스 메타데이터가 사정권에 들어온다.
    지연과 이그레스도 요청 예산 안에서 통제되지 않는다.

    Args:
        url: `data:image/<형식>;base64,<페이로드>` 형태의 데이터 URL.

    Returns:
        Converse 이미지 콘텐츠 블록.

    Raises:
        InvalidRequestError: 원격 URL 이거나, 형식이 지원 목록에 없거나,
            base64 가 깨졌거나, 크기 상한을 넘은 경우.
    """
    if not url.startswith("data:"):
        raise errors.InvalidRequestError(
            "이미지는 base64 데이터 URL 로만 보낼 수 있다. 원격 URL 을"
            " 게이트웨이가 대신 가져오지 않는다. 게이트웨이가 임의 주소로"
            " 요청을 보내는 통로가 되면 내부망이 노출되기 때문이다."
            " data:image/png;base64,... 형태로 인코딩해서 보낸다."
        )

    match = _DATA_URL_PATTERN.match(url)
    if match is None:
        raise errors.InvalidRequestError(
            "이미지 데이터 URL 형식이 아니다. 지원하는 형식은 "
            + ", ".join(sorted(_SUPPORTED_IMAGE_FORMATS))
            + " 이고, data:image/<형식>;base64,<페이로드> 로 보낸다."
        )

    media_format = match.group("format").lower()
    if media_format == "jpg":
        media_format = "jpeg"
    if media_format not in _SUPPORTED_IMAGE_FORMATS:
        raise errors.InvalidRequestError(
            f"지원하지 않는 이미지 형식이다: {media_format}."
            " 지원하는 형식은 " + ", ".join(sorted(_SUPPORTED_IMAGE_FORMATS))
        )

    payload = match.group("payload")
    # **디코딩 전에** 인코딩 길이로 먼저 거른다. base64 는 원본의 약 4/3 이라
    # 상한을 넘는 것은 디코딩해 볼 필요가 없다. 나중에 검사하면 거부할
    # 페이로드를 메모리에 먼저 다 올린다.
    if len(payload) > (MAX_IMAGE_BYTES // 3 + 1) * 4 + 4:
        raise errors.InvalidRequestError(
            f"이미지가 너무 크다. 이미지 하나당 상한은 {MAX_IMAGE_BYTES}"
            " 바이트다."
        )

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise errors.InvalidRequestError(
            "이미지 base64 페이로드를 디코딩할 수 없다."
        ) from exc

    if not raw:
        raise errors.InvalidRequestError("이미지 페이로드가 비어 있다.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise errors.InvalidRequestError(
            f"이미지가 너무 크다: {len(raw)} 바이트."
            f" 이미지 하나당 상한은 {MAX_IMAGE_BYTES} 바이트다."
        )

    budget.consume(len(raw))
    return {"image": {"format": media_format, "source": {"bytes": raw}}}


class _ImageBudget:
    """한 요청이 쓸 수 있는 이미지 총량을 센다.

    이미지 하나당 상한만 두면 4.4MB 이미지 50장이 통과한다. 디코딩된
    바이트가 220MB 가 되고, 태스크 메모리가 1 GiB 라 동시 요청 몇 건으로
    프로세스가 죽는다. 요청 전체에 상한을 둔다.
    """

    def __init__(self) -> None:
        """예산을 만든다."""
        self._bytes = 0
        self._count = 0

    def consume(self, size: int) -> None:
        """이미지 하나를 예산에서 차감한다.

        Args:
            size: 디코딩된 바이트 수.

        Raises:
            InvalidRequestError: 개수나 총량 상한을 넘은 경우.
        """
        self._count += 1
        self._bytes += size
        if self._count > MAX_IMAGES_PER_REQUEST:
            raise errors.InvalidRequestError(
                f"한 요청의 이미지 수 상한은 {MAX_IMAGES_PER_REQUEST} 장이다."
            )
        if self._bytes > MAX_TOTAL_IMAGE_BYTES:
            raise errors.InvalidRequestError(
                "한 요청의 이미지 총량 상한은"
                f" {MAX_TOTAL_IMAGE_BYTES} 바이트다."
            )


def _tool_use_block(tool_call: schemas.ToolCall) -> _JsonDict:
    """OpenAI 도구 호출을 Converse `toolUse` 블록으로 만든다.

    Args:
        tool_call: 어시스턴트 메시지의 도구 호출.

    Returns:
        Converse `toolUse` 콘텐츠 블록.

    Raises:
        InvalidRequestError: `arguments` 가 JSON 객체가 아닌 경우.
    """
    raw = tool_call.function.arguments or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise errors.InvalidRequestError(
            f"도구 호출 인자가 JSON 이 아니다 (tool_call_id={tool_call.id})."
            " OpenAI 규약대로 arguments 는 JSON 문자열이어야 한다."
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.InvalidRequestError(
            f"도구 호출 인자는 JSON 객체여야 한다 (tool_call_id="
            f"{tool_call.id})."
        )
    return {
        "toolUse": {
            "toolUseId": tool_call.id,
            "name": tool_call.function.name,
            "input": parsed,
        }
    }


def _tool_result_block(message: schemas.ChatMessage) -> _JsonDict:
    """`role="tool"` 메시지를 Converse `toolResult` 블록으로 만든다.

    Args:
        message: 도구 결과 메시지.

    Returns:
        Converse `toolResult` 콘텐츠 블록.

    Raises:
        InvalidRequestError: `tool_call_id` 가 없는 경우.
    """
    tool_call_id = (message.tool_call_id or "").strip()
    if not tool_call_id:
        raise errors.InvalidRequestError(
            "role=tool 메시지에는 tool_call_id 가 필요하다. 어느 도구 호출에"
            " 대한 결과인지 알 수 없으면 Bedrock 이 대화를 거부한다."
        )
    # 도구 결과에 이미지를 담는 것은 이 게이트웨이가 변환하지 않는다.
    # 조용히 버리면 보낸 사람이 모델이 그것을 봤다고 오해한다.
    if message.image_parts():
        raise errors.InvalidRequestError(
            "role=tool 메시지에는 이미지를 넣을 수 없다. 이 게이트웨이는"
            " 도구 결과를 텍스트로만 전달한다."
        )
    return {
        "toolResult": {
            "toolUseId": tool_call_id,
            "content": [{"text": message.text()}],
            "status": "success",
        }
    }


def _build_tool_config(
    request: schemas.ChatCompletionRequest,
) -> tuple[_JsonDict | None, str | None]:
    """`tools`/`tool_choice`/`response_format` 을 `toolConfig` 로 만든다.

    구조화 출력(`response_format.type == "json_schema"`)은 Converse 에 대응
    필드가 없어 **강제 도구 호출**로 구현한다. 스키마를 입력으로 받는 도구
    하나를 합성해 그것만 호출하도록 강제하고, 응답에서 그 도구의 입력을 꺼내
    본문으로 되돌린다. 클라이언트가 보는 계약은 OpenAI 와 같다.

    Args:
        request: 요청 본문.

    Returns:
        (`toolConfig` 또는 `None`, 합성 도구 이름 또는 `None`).

    Raises:
        InvalidRequestError: Converse 로 표현할 수 없는 조합인 경우.
    """
    response_format = request.response_format
    wants_structured = (
        response_format is not None and response_format.type == "json_schema"
    )

    if response_format is not None:
        if response_format.type == "json_object":
            raise errors.InvalidRequestError(
                "스키마 없는 response_format.type=json_object 는 Converse 에서"
                " 강제할 수 없다. 프롬프트로 부탁하는 방식은 모델이 어겨도"
                " 게이트웨이가 알 수 없어 지원하지 않는다."
                " json_schema 를 쓴다."
            )
        if response_format.type not in _SUPPORTED_RESPONSE_FORMATS:
            raise errors.InvalidRequestError(
                f"지원하지 않는 response_format.type 이다:"
                f" {response_format.type}."
                " 지원하는 값은 "
                + ", ".join(sorted(_SUPPORTED_RESPONSE_FORMATS))
            )

    if wants_structured and request.tools:
        raise errors.InvalidRequestError(
            "response_format=json_schema 와 tools 를 함께 쓸 수 없다."
            " 구조화 출력을 강제 도구 호출로 구현하기 때문에 요청한 도구와"
            " 충돌한다. 둘 중 하나만 보낸다."
        )

    if wants_structured and request.tool_choice is not None:
        # 구조화 출력은 합성 도구를 강제 호출(toolChoice)해 구현한다.
        # tool_choice 를 함께 받아 조용히 무시하면, 예를 들어
        # tool_choice="none" 을 보낸 사람은 도구가 전혀 호출되지 않을
        # 것으로 기대하는데 실제로는 강제 호출된다. 다른 지원하지 않는
        # 조합처럼 조용히 다르게 동작시키지 않고 거부한다.
        raise errors.InvalidRequestError(
            "response_format=json_schema 와 tool_choice 를 함께 쓸 수 없다."
            " 구조화 출력은 합성 도구를 항상 강제 호출하므로 tool_choice 가"
            " 요청한 선택 전략과 충돌한다."
        )

    if wants_structured and request.stream:
        raise errors.InvalidRequestError(
            "response_format=json_schema 는 스트리밍과 함께 쓸 수 없다."
            " 구조화 출력을 강제 도구 호출로 구현하기 때문에 본문이 도구 인자로"
            " 오고, 증분 텍스트가 발생하지 않는다. 스트리밍을 끄고 요청한다."
        )

    if request.parallel_tool_calls is False:
        raise errors.InvalidRequestError(
            "parallel_tool_calls=false 는 지원하지 않는다. Converse 에 병렬"
            " 도구 호출을 끄는 스위치가 없어, 받아들이면 지켜지지 않는 약속이"
            " 된다. 도구를 하나만 노출하면 사실상 같은 효과를 얻는다."
        )

    if wants_structured:
        assert response_format is not None  # 위에서 확인했다.
        spec = response_format.json_schema or schemas.JsonSchemaSpec()
        if not spec.json_schema:
            raise errors.InvalidRequestError(
                "response_format=json_schema 인데 schema 가 비어 있다."
            )
        tool_name = _sanitize_tool_name(spec.name)
        return (
            {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool_name,
                            "description": (
                                "요청된 스키마에 맞는 결과를 반환한다."
                            ),
                            "inputSchema": {"json": spec.json_schema},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": tool_name}},
            },
            tool_name,
        )

    if not request.tools:
        if request.tool_choice is not None:
            raise errors.InvalidRequestError(
                "tool_choice 를 보냈지만 tools 가 없다."
            )
        return (None, None)

    tools = [
        {
            "toolSpec": {
                "name": tool.function.name,
                "description": tool.function.description or tool.function.name,
                "inputSchema": {
                    "json": tool.function.parameters
                    or {"type": "object", "properties": {}}
                },
            }
        }
        for tool in request.tools
    ]
    config: _JsonDict = {"tools": tools}
    choice = _map_tool_choice(request.tool_choice)
    if choice is not None:
        config["toolChoice"] = choice
    return (config, None)


def _map_tool_choice(
    tool_choice: str | dict[str, typing.Any] | None,
) -> _JsonDict | None:
    """OpenAI `tool_choice` 를 Converse `toolChoice` 로 바꾼다.

    `none` 은 거부한다. OpenAI 의 `none` 은 "도구 정의는 보여주되 호출은
    금지" 라는 뜻이다. Converse 에 대응 값이 없고, `toolConfig` 를 아예 빼면
    모델이 보는 프롬프트가 달라져 다른 답이 나온다. `auto` 로 보내고 결과의
    도구 호출을 버리는 방식은 토큰을 실제로 쓰고도 요청과 다른 동작을 한다.
    받아들이면 지켜지지 않는 약속이 되므로 거부한다.

    Args:
        tool_choice: OpenAI 형식 값.

    Returns:
        Converse `toolChoice`. 지정이 없으면 `None`.

    Raises:
        InvalidRequestError: 표현할 수 없는 값인 경우.
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        if normalized == "auto":
            return {"auto": {}}
        if normalized in ("required", "any"):
            return {"any": {}}
        if normalized == "none":
            raise errors.InvalidRequestError(
                "tool_choice=none 은 지원하지 않는다. Converse 에 대응 값이"
                " 없어 정확히 구현할 수 없다. 도구를 쓰지 않으려면 tools 를"
                " 보내지 않는다."
            )
        raise errors.InvalidRequestError(
            f"지원하지 않는 tool_choice 다: {tool_choice}"
        )

    function = tool_choice.get("function")
    name = ""
    if isinstance(function, dict):
        name = str(function.get("name") or "").strip()
    if not name:
        raise errors.InvalidRequestError(
            "tool_choice 객체에는 function.name 이 필요하다."
        )
    return {"tool": {"name": name}}


def _sanitize_tool_name(name: str) -> str:
    """도구 이름을 Bedrock 이 받는 문자만 남겨 정리한다.

    Args:
        name: 원본 이름.

    Returns:
        영숫자와 밑줄만 남긴 이름. 비면 기본값을 쓴다.
    """
    cleaned = "".join(
        char if (char.isascii() and char.isalnum()) or char == "_" else "_"
        for char in name
    ).strip("_")
    return cleaned[:64] or "structured_output"


def _merge_adjacent_roles(
    conversation: list[tuple[str, list[_JsonDict]]],
) -> list[tuple[str, list[_JsonDict]]]:
    """같은 역할이 연속된 메시지를 하나로 합친다.

    텍스트 블록이 맞닿는 경우에만 개행으로 이어 붙인다. 그렇지 않으면 블록
    목록을 그대로 잇는다. 이미지나 도구 블록을 문자열로 합칠 수는 없다.

    Args:
        conversation: (역할, 블록 목록) 순서 목록.

    Returns:
        역할이 번갈아 나오도록 병합된 목록.
    """
    merged: list[tuple[str, list[_JsonDict]]] = []
    for role, blocks in conversation:
        if merged and merged[-1][0] == role:
            previous = merged[-1][1]
            if (
                previous
                and blocks
                and _is_text_block(previous[-1])
                and _is_text_block(blocks[0])
            ):
                previous[-1] = {
                    "text": f"{previous[-1]['text']}\n{blocks[0]['text']}"
                }
                previous.extend(blocks[1:])
            else:
                previous.extend(blocks)
        else:
            merged.append((role, list(blocks)))
    return merged


def _is_text_block(block: _JsonDict) -> bool:
    """블록이 텍스트 블록이면 True."""
    return set(block) == {"text"}


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


def extract_tool_uses(bedrock_output: _JsonDict) -> tuple[ToolUse, ...]:
    """Converse 응답에서 도구 호출을 추출한다.

    Args:
        bedrock_output: Converse 응답의 `output` 값.

    Returns:
        도구 호출 튜플. 없으면 빈 튜플.
    """
    message = bedrock_output.get("message") or {}
    blocks = message.get("content") or []
    uses: list[ToolUse] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        tool_use = block.get("toolUse")
        if not isinstance(tool_use, dict):
            continue
        uses.append(
            ToolUse(
                tool_use_id=str(tool_use.get("toolUseId") or ""),
                name=str(tool_use.get("name") or ""),
                arguments=json.dumps(
                    tool_use.get("input") or {}, ensure_ascii=False
                ),
            )
        )
    return tuple(uses)


def unwrap_structured_output(
    tool_calls: typing.Sequence[ToolUse], tool_name: str
) -> str:
    """강제 도구 호출 결과를 구조화 출력 본문으로 되돌린다.

    `response_format=json_schema` 는 Converse 에 대응 필드가 없어 스키마를
    입력으로 받는 도구를 합성해 강제 호출한다. 클라이언트에게는 OpenAI 와
    같은 계약을 보여야 하므로, 합성 도구의 입력을 그대로 본문 JSON 으로
    되돌리고 도구 호출은 노출하지 않는다.

    Args:
        tool_calls: 응답에서 추출한 도구 호출 목록.
        tool_name: 합성한 도구 이름.

    Returns:
        스키마에 맞는 JSON 문자열.

    Raises:
        UpstreamError: 모델이 합성 도구를 호출하지 않은 경우. 조용히 빈
            본문을 돌려주면 클라이언트가 스키마를 지킨 결과로 오해한다.
    """
    for call in tool_calls:
        if call.name == tool_name:
            return call.arguments
    raise errors.UpstreamError(
        "모델이 구조화 출력 스키마를 채우지 않았다. 이 모델이 강제 도구"
        " 호출(toolChoice)을 지원하지 않을 수 있다."
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
    tool_calls: typing.Sequence[ToolUse] = (),
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
        tool_calls: 모델이 요청한 도구 호출 목록.

    Returns:
        직렬화 가능한 응답 딕셔너리.
    """
    message: _JsonDict = {"role": _ASSISTANT_ROLE}
    if tool_calls:
        # OpenAI 는 도구 호출이 있으면 content 를 null 로 둔다.
        message["content"] = content or None
        message["tool_calls"] = [
            {
                "id": call.tool_use_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
            for call in tool_calls
        ]
    else:
        message["content"] = content

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_unix,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
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


def build_embedding_response(
    *,
    model_id: str,
    vectors: typing.Sequence[typing.Sequence[float]],
    input_tokens: int,
    base64_encoding: bool = False,
) -> _JsonDict:
    """OpenAI 형식의 임베딩 응답을 만든다.

    `usage` 에 `completion_tokens` 를 넣지 않는다. 임베딩은 출력 토큰이 없고,
    OpenAI 도 이 엔드포인트에서는 `prompt_tokens` 와 `total_tokens` 만 준다.

    Args:
        model_id: 요청 모델 ID.
        vectors: 입력 순서와 같은 순서의 임베딩 벡터.
        input_tokens: 입력 토큰 수.
        base64_encoding: `True` 면 벡터를 리틀엔디언 float32 base64 로 만든다.
            OpenAI 파이썬 클라이언트가 기본으로 요청하는 형식이다.

    Returns:
        직렬화 가능한 응답 딕셔너리.
    """
    data: list[_JsonDict] = []
    for index, vector in enumerate(vectors):
        if base64_encoding:
            # `math.isfinite` 검사는 값이 유한한지만 본다. float32 범위
            # (~3.4e38)를 넘는 유한값은 통과한 뒤 여기서 OverflowError 가
            # 난다. Titan 은 정규화된 벡터를 주므로 현실에서는 생기지
            # 않지만, 조용히 500 으로 새면 이 레코드도 실패로 집계되지
            # 않는다.
            try:
                packed = struct.pack(f"<{len(vector)}f", *vector)
            except (struct.error, OverflowError) as exc:
                raise errors.UpstreamError(
                    "임베딩 벡터 값이 float32 범위를 넘어 직렬화할 수 없다."
                ) from exc
            embedding: typing.Any = base64.b64encode(packed).decode("ascii")
        else:
            embedding = list(vector)
        data.append(
            {"object": "embedding", "index": index, "embedding": embedding}
        )
    return {
        "object": "list",
        "data": data,
        "model": model_id,
        "usage": {
            "prompt_tokens": input_tokens,
            "total_tokens": input_tokens,
        },
    }


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
