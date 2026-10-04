"""도구 사용·비전·구조화 출력 변환 테스트.

Converse 요청 형태의 정확성은 `botocore.stub.Stubber` 가 실제 서비스 모델과
대조해 검증한다. AWS 계정 없이도 잘못된 파라미터 이름이나 구조를
`ParamValidationError` 로 잡을 수 있다.

거부 동작을 함께 검증하는 이유는 이 게이트웨이가 **표현할 수 없는 요청을
조용히 무시하지 않는다**는 원칙을 지키기 때문이다. `tool_choice="none"` 처럼
Converse 에 대응이 없는 값을 받아들이면 지켜지지 않는 약속이 된다.
"""

from __future__ import annotations

import base64
import json
import typing

import boto3
import botocore.stub
import pytest

from llmgw import bedrock
from llmgw import cache
from llmgw import errors
from llmgw import observability
from llmgw import schemas
from llmgw import translate

# 1x1 PNG.
_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
_PNG_DATA_URL = f"data:image/png;base64,{_PNG_BASE64}"

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "도시의 현재 날씨를 조회한다",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _request(**overrides: typing.Any) -> schemas.ChatCompletionRequest:
    """테스트용 요청을 만든다."""
    payload: dict[str, typing.Any] = {
        "model": "amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "서울 날씨 알려줘"}],
    }
    payload.update(overrides)
    return schemas.ChatCompletionRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# 도구 정의 변환
# ---------------------------------------------------------------------------


def test_tools를toolConfig로변환한다() -> None:
    # Arrange
    request = _request(tools=[_WEATHER_TOOL])

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.tool_config is not None
    spec = actual.tool_config["tools"][0]["toolSpec"]
    assert spec["name"] == "get_weather"
    assert spec["inputSchema"]["json"]["required"] == ["city"]
    # 지정이 없으면 toolChoice 를 붙이지 않는다. 붙이면 모델 기본 동작을
    # 임의로 바꾸는 것이다.
    assert "toolChoice" not in actual.tool_config


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("auto", {"auto": {}}),
        ("required", {"any": {}}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"tool": {"name": "get_weather"}},
        ),
    ],
)
def test_tool_choice를Converse형식으로변환한다(
    given: typing.Any, expected: dict[str, typing.Any]
) -> None:
    # Act
    actual = translate.to_bedrock_request(
        _request(tools=[_WEATHER_TOOL], tool_choice=given)
    )

    # Assert
    assert actual.tool_config is not None
    assert actual.tool_config["toolChoice"] == expected


def test_tool_choice_none은거부한다() -> None:
    # Converse 에 대응 값이 없다. 받아들이면 "정의는 보여주되 호출 금지" 라는
    # 요청을 지키지 못한 채 응답하게 된다.
    with pytest.raises(errors.InvalidRequestError, match="tool_choice=none"):
        translate.to_bedrock_request(
            _request(tools=[_WEATHER_TOOL], tool_choice="none")
        )


def test_parallel_tool_calls_false는거부한다() -> None:
    with pytest.raises(errors.InvalidRequestError, match="parallel_tool_calls"):
        translate.to_bedrock_request(
            _request(tools=[_WEATHER_TOOL], parallel_tool_calls=False)
        )


def test_tools없이tool_choice만보내면거부한다() -> None:
    with pytest.raises(errors.InvalidRequestError, match="tools 가 없다"):
        translate.to_bedrock_request(_request(tool_choice="auto"))


# ---------------------------------------------------------------------------
# 도구 왕복
# ---------------------------------------------------------------------------


def test_어시스턴트도구호출을toolUse블록으로변환한다() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "user", "content": "서울 날씨"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "서울"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "맑음"},
        ],
        tools=[_WEATHER_TOOL],
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.messages[1]["role"] == "assistant"
    tool_use = actual.messages[1]["content"][0]["toolUse"]
    assert tool_use["toolUseId"] == "call_1"
    assert tool_use["input"] == {"city": "서울"}
    # 도구 결과는 user 턴의 toolResult 블록이다.
    assert actual.messages[2]["role"] == "user"
    tool_result = actual.messages[2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "call_1"


def test_도구결과가연속되면한user턴으로합쳐진다() -> None:
    # Converse 는 한 턴의 도구 결과를 하나의 user 메시지에 담아야 한다.
    request = _request(
        messages=[
            {"role": "user", "content": "날씨 두 곳"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "맑음"},
            {"role": "tool", "tool_call_id": "b", "content": "비"},
        ],
        tools=[_WEATHER_TOOL],
    )

    actual = translate.to_bedrock_request(request)

    assert len(actual.messages) == 3
    assert len(actual.messages[2]["content"]) == 2


def test_도구호출인자가JSON이아니면거부한다() -> None:
    request = _request(
        messages=[
            {"role": "user", "content": "안녕"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "get_weather",
                            "arguments": "not-json",
                        },
                    }
                ],
            },
        ],
    )
    with pytest.raises(errors.InvalidRequestError, match="JSON 이 아니다"):
        translate.to_bedrock_request(request)


def test_tool메시지에tool_call_id가없으면거부한다() -> None:
    request = _request(
        messages=[
            {"role": "user", "content": "안녕"},
            {"role": "tool", "content": "결과"},
        ],
    )
    with pytest.raises(errors.InvalidRequestError, match="tool_call_id"):
        translate.to_bedrock_request(request)


def test_응답의toolUse를추출한다() -> None:
    output = {
        "message": {
            "role": "assistant",
            "content": [
                {"text": "조회하겠습니다"},
                {
                    "toolUse": {
                        "toolUseId": "tu_1",
                        "name": "get_weather",
                        "input": {"city": "서울"},
                    }
                },
            ],
        }
    }

    actual = translate.extract_tool_uses(output)

    assert len(actual) == 1
    assert actual[0].tool_use_id == "tu_1"
    assert json.loads(actual[0].arguments) == {"city": "서울"}


def test_도구호출이있으면응답에tool_calls를넣는다() -> None:
    # Act
    actual = translate.build_completion_response(
        completion_id="chatcmpl-1",
        created_unix=0,
        model_id="amazon.nova-lite-v1:0",
        content="",
        finish_reason="tool_calls",
        input_tokens=1,
        output_tokens=1,
        tool_calls=[
            translate.ToolUse("tu_1", "get_weather", '{"city": "서울"}')
        ],
    )

    # Assert
    message = actual["choices"][0]["message"]
    # OpenAI 는 도구 호출이 있으면 content 를 null 로 둔다.
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "get_weather"
    assert actual["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# 비전
# ---------------------------------------------------------------------------


def test_base64이미지를Converse이미지블록으로변환한다() -> None:
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "설명해줘"},
                    {"type": "image_url", "image_url": {"url": _PNG_DATA_URL}},
                ],
            }
        ]
    )

    actual = translate.to_bedrock_request(request)

    blocks = actual.messages[0]["content"]
    assert blocks[0] == {"text": "설명해줘"}
    image = blocks[1]["image"]
    assert image["format"] == "png"
    # Converse 는 base64 문자열이 아니라 원시 바이트를 받는다.
    assert image["source"]["bytes"] == base64.b64decode(_PNG_BASE64)


def test_이미지만있는메시지는버려지지않는다() -> None:
    # 블록 기반 변환 이전에는 텍스트가 비면 메시지를 통째로 버렸다.
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _PNG_DATA_URL}}
                ],
            }
        ]
    )

    actual = translate.to_bedrock_request(request)

    assert len(actual.messages) == 1
    assert "image" in actual.messages[0]["content"][0]


def test_원격이미지URL은거부한다() -> None:
    # 게이트웨이가 클라이언트가 준 URL 을 가져오면 SSRF 통로가 된다.
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.png"},
                    }
                ],
            }
        ]
    )
    with pytest.raises(errors.InvalidRequestError, match="base64"):
        translate.to_bedrock_request(request)


def test_지원하지않는이미지형식은거부한다() -> None:
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/bmp;base64,QQ=="},
                    }
                ],
            }
        ]
    )
    with pytest.raises(errors.InvalidRequestError, match="이미지 형식"):
        translate.to_bedrock_request(request)


def test_깨진base64이미지는거부한다() -> None:
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,!!!!"},
                    }
                ],
            }
        ]
    )
    with pytest.raises(errors.InvalidRequestError, match="디코딩"):
        translate.to_bedrock_request(request)


def test_상한을넘는이미지는거부한다() -> None:
    oversized = base64.b64encode(
        b"\x00" * (translate.MAX_IMAGE_BYTES + 1)
    ).decode()
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{oversized}"
                        },
                    }
                ],
            }
        ]
    )
    with pytest.raises(errors.InvalidRequestError, match="너무 크다"):
        translate.to_bedrock_request(request)


# ---------------------------------------------------------------------------
# 구조화 출력
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


def test_json_schema를강제도구호출로변환한다() -> None:
    request = _request(
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": _SCHEMA},
        }
    )

    actual = translate.to_bedrock_request(request)

    assert actual.structured_tool_name == "answer"
    assert actual.tool_config is not None
    assert actual.tool_config["toolChoice"] == {"tool": {"name": "answer"}}
    spec = actual.tool_config["tools"][0]["toolSpec"]
    assert spec["inputSchema"]["json"] == _SCHEMA


def test_json_object는거부한다() -> None:
    # 스키마가 없으면 강제할 수단이 없다. 프롬프트로 부탁하고 지켜졌는지
    # 모르는 채 응답하면 요청과 다른 동작이다.
    with pytest.raises(errors.InvalidRequestError, match="json_object"):
        translate.to_bedrock_request(
            _request(response_format={"type": "json_object"})
        )


def test_json_schema와tools를함께쓰면거부한다() -> None:
    with pytest.raises(errors.InvalidRequestError, match="함께 쓸 수 없다"):
        translate.to_bedrock_request(
            _request(
                tools=[_WEATHER_TOOL],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "a", "schema": _SCHEMA},
                },
            )
        )


def test_json_schema와스트리밍을함께쓰면거부한다() -> None:
    with pytest.raises(errors.InvalidRequestError, match="스트리밍"):
        translate.to_bedrock_request(
            _request(
                stream=True,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "a", "schema": _SCHEMA},
                },
            )
        )


def test_구조화출력을본문으로되돌린다() -> None:
    calls = [translate.ToolUse("tu_1", "answer", '{"city": "서울"}')]

    actual = translate.unwrap_structured_output(calls, "answer")

    assert json.loads(actual) == {"city": "서울"}


def test_모델이스키마도구를호출하지않으면오류로만든다() -> None:
    # 빈 본문을 돌려주면 클라이언트가 스키마를 지킨 결과로 오해한다.
    with pytest.raises(errors.UpstreamError, match="구조화 출력"):
        translate.unwrap_structured_output((), "answer")


# ---------------------------------------------------------------------------
# Converse 파라미터 검증 (Stubber 가 실제 서비스 모델과 대조한다)
# ---------------------------------------------------------------------------


def _gateway(runtime: typing.Any) -> bedrock.BedrockGateway:
    """대역 런타임으로 어댑터를 만든다."""
    return bedrock.BedrockGateway(
        control_client=object(),
        runtime_client=runtime,
        logger=observability.create_logger(
            service_name="llmgw-test", level="CRITICAL"
        ),
        model_cache=cache.TtlCache(ttl_seconds=1.0),
    )


def test_toolConfig가Converse스펙검증을통과한다() -> None:
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    request = translate.to_bedrock_request(
        _request(tools=[_WEATHER_TOOL], tool_choice="auto")
    )
    expected_params = {
        "modelId": "amazon.nova-lite-v1:0",
        "messages": request.messages,
        "toolConfig": request.tool_config,
    }
    stubber.add_response(
        "converse",
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tu_1",
                                "name": "get_weather",
                                "input": {"city": "서울"},
                            }
                        }
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {
                "inputTokens": 20,
                "outputTokens": 8,
                "totalTokens": 28,
            },
            "metrics": {"latencyMs": 100},
        },
        expected_params,
    )

    # Act
    with stubber:
        result = _gateway(client).converse(
            model_id="amazon.nova-lite-v1:0",
            messages=request.messages,
            system=request.system,
            inference_config=request.inference_config,
            tool_config=request.tool_config,
        )

    # Assert
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].name == "get_weather"
    stubber.assert_no_pending_responses()


def test_이미지블록이Converse스펙검증을통과한다() -> None:
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    request = translate.to_bedrock_request(
        _request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": _PNG_DATA_URL},
                        }
                    ],
                }
            ]
        )
    )
    stubber.add_response(
        "converse",
        {
            "output": {
                "message": {"role": "assistant", "content": [{"text": "그림"}]}
            },
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 30,
                "outputTokens": 3,
                "totalTokens": 33,
            },
            "metrics": {"latencyMs": 100},
        },
        {
            "modelId": "amazon.nova-lite-v1:0",
            "messages": request.messages,
        },
    )

    with stubber:
        result = _gateway(client).converse(
            model_id="amazon.nova-lite-v1:0",
            messages=request.messages,
            system=request.system,
            inference_config=request.inference_config,
        )

    assert result.text == "그림"
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# 도구 스트리밍
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """converse_stream 이벤트를 그대로 돌려주는 대역."""

    def __init__(self, events: list[dict[str, typing.Any]]) -> None:
        self._events = events
        self.captured_params: dict[str, typing.Any] = {}

    def converse_stream(self, **params: typing.Any) -> dict[str, typing.Any]:
        """이벤트 목록을 스트림처럼 반환한다."""
        self.captured_params = params
        return {"stream": iter(self._events)}


def test_도구스트림의순번은도구만센다() -> None:
    """OpenAI 의 tool_calls[].index 는 도구 호출만 세는 번호다.

    Converse 의 `contentBlockIndex` 는 텍스트 블록까지 포함해 번호를 매긴다.
    그대로 쓰면 텍스트가 앞에 올 때 번호가 밀려, 클라이언트가 병렬 도구
    호출의 인자를 엉뚱한 호출에 붙인다.
    """
    # Arrange: 블록 0 은 텍스트, 블록 1·2 가 도구 호출이다.
    events: list[dict[str, typing.Any]] = [
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "네"},
            }
        },
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "a", "name": "f1"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"x"'}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 2,
                "start": {"toolUse": {"toolUseId": "b", "name": "f2"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": ":1}"}},
            }
        },
        {"messageStop": {"stopReason": "tool_use"}},
        {
            "metadata": {
                "usage": {"inputTokens": 5, "outputTokens": 2},
            }
        },
    ]

    # Act
    deltas = list(
        _gateway(_FakeRuntime(events)).converse_stream(
            model_id="m", messages=[], system=[], inference_config={}
        )
    )

    # Assert
    tool_deltas = [d for d in deltas if d.tool_index is not None]
    starts = [d for d in tool_deltas if d.tool_use_id]
    # 블록 인덱스는 1·2 이지만 도구 순번은 0·1 이어야 한다.
    assert [d.tool_index for d in starts] == [0, 1]
    assert [d.tool_use_id for d in starts] == ["a", "b"]
    arguments = "".join(d.tool_arguments_delta for d in tool_deltas)
    assert arguments == '{"x":1}'


def test_시작이벤트없는도구증분은오류로만든다() -> None:
    # 조용히 버리면 잘린 JSON 이 클라이언트로 간다.
    events: list[dict[str, typing.Any]] = [
        {
            "contentBlockDelta": {
                "contentBlockIndex": 7,
                "delta": {"toolUse": {"input": "{}"}},
            }
        }
    ]
    with pytest.raises(errors.UpstreamError, match="시작 이벤트"):
        list(
            _gateway(_FakeRuntime(events)).converse_stream(
                model_id="m", messages=[], system=[], inference_config={}
            )
        )
