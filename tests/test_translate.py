"""OpenAI ↔ Bedrock 변환 테스트."""

from __future__ import annotations

import pytest

from llmgw import errors
from llmgw import schemas
from llmgw import translate


def _request(**overrides: object) -> schemas.ChatCompletionRequest:
    """기본값이 채워진 요청 객체를 만든다."""
    payload: dict[str, object] = {
        "model": "amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "안녕"}],
    }
    payload.update(overrides)
    return schemas.ChatCompletionRequest.model_validate(payload)


def test_to_bedrock_request_기본대화를변환한다() -> None:
    # Arrange
    request = _request()

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.messages == [{"role": "user", "content": [{"text": "안녕"}]}]
    assert actual.system == []
    assert actual.inference_config == {}


def test_to_bedrock_request_시스템메시지는system으로분리된다() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "system", "content": "너는 번역가다"},
            {"role": "user", "content": "hello"},
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.system == [{"text": "너는 번역가다"}]
    assert len(actual.messages) == 1
    assert actual.messages[0]["role"] == "user"


def test_to_bedrock_request_developer역할도system으로처리한다() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "developer", "content": "규칙"},
            {"role": "user", "content": "hi"},
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.system == [{"text": "규칙"}]


def test_to_bedrock_request_연속된같은역할을병합한다() -> None:
    """Bedrock 은 user/assistant 교대를 요구한다."""
    # Arrange
    request = _request(
        messages=[
            {"role": "user", "content": "첫째"},
            {"role": "user", "content": "둘째"},
            {"role": "assistant", "content": "답1"},
            {"role": "assistant", "content": "답2"},
            {"role": "user", "content": "셋째"},
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    roles = [message["role"] for message in actual.messages]
    assert roles == ["user", "assistant", "user"], f"실제 {roles}"
    assert actual.messages[0]["content"] == [{"text": "첫째\n둘째"}]
    assert actual.messages[1]["content"] == [{"text": "답1\n답2"}]


def test_to_bedrock_request_멀티모달텍스트조각을이어붙인다() -> None:
    # Arrange
    request = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "첫 줄"},
                    {"type": "text", "text": "둘째 줄"},
                ],
            }
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.messages[0]["content"] == [{"text": "첫 줄\n둘째 줄"}]


def test_to_bedrock_request_추론설정을매핑한다() -> None:
    # Arrange
    request = _request(max_tokens=256, temperature=0.5, top_p=0.9, stop=["END"])

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.inference_config == {
        "maxTokens": 256,
        "temperature": 0.5,
        "topP": 0.9,
        "stopSequences": ["END"],
    }


def test_to_bedrock_request_max_completion_tokens가우선한다() -> None:
    # Arrange
    request = _request(max_tokens=100, max_completion_tokens=512)

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.inference_config["maxTokens"] == 512


def test_to_bedrock_request_stop문자열단일값도목록으로변환한다() -> None:
    # Arrange
    request = _request(stop="STOP")

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert actual.inference_config["stopSequences"] == ["STOP"]


def test_to_bedrock_request_빈stop은설정에넣지않는다() -> None:
    # Arrange
    request = _request(stop=[])

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert "stopSequences" not in actual.inference_config


def test_to_bedrock_request_공백만있는메시지는제외한다() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": "다시 질문"},
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    # 빈 assistant 가 빠지면서 user 두 건이 병합된다.
    assert len(actual.messages) == 1
    assert actual.messages[0]["content"] == [{"text": "질문\n다시 질문"}]


def test_to_bedrock_request_시스템메시지만있으면오류() -> None:
    # Arrange
    request = _request(messages=[{"role": "system", "content": "규칙"}])

    # Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="최소 한 건"):
        translate.to_bedrock_request(request)


def test_to_bedrock_request_assistant로시작하면오류() -> None:
    # Arrange
    request = _request(messages=[{"role": "assistant", "content": "먼저 말함"}])

    # Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="user 메시지로"):
        translate.to_bedrock_request(request)


def test_to_bedrock_request_지원하지않는역할은오류() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "function", "content": "x"},
        ]
    )

    # Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="역할"):
        translate.to_bedrock_request(request)


def test_to_bedrock_request_내용이None인메시지는제외된다() -> None:
    # Arrange
    request = _request(
        messages=[
            {"role": "user", "content": "질문"},
            {"role": "assistant", "content": None},
        ]
    )

    # Act
    actual = translate.to_bedrock_request(request)

    # Assert
    assert len(actual.messages) == 1


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("content_filtered", "content_filter"),
        ("guardrail_intervened", "content_filter"),
        ("완전히모르는값", "stop"),
        ("", "stop"),
        (None, "stop"),
    ],
)
def test_map_finish_reason(stop_reason: str | None, expected: str) -> None:
    # Arrange / Act
    actual = translate.map_finish_reason(stop_reason)

    # Assert
    assert actual == expected


def test_extract_text_여러텍스트블록을이어붙인다() -> None:
    # Arrange
    output = {
        "message": {
            "content": [
                {"text": "가"},
                {"text": "나"},
                {"toolUse": {"name": "x"}},
            ]
        }
    }

    # Act
    actual = translate.extract_text(output)

    # Assert
    assert actual == "가나"


def test_extract_text_빈응답은빈문자열() -> None:
    # Arrange / Act / Assert
    assert translate.extract_text({}) == ""
    assert translate.extract_text({"message": {}}) == ""


def test_build_completion_response_OpenAI형식을만든다() -> None:
    # Arrange / Act
    actual = translate.build_completion_response(
        completion_id="chatcmpl-1",
        created_unix=1787486400,
        model_id="amazon.nova-lite-v1:0",
        content="응답",
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
    )

    # Assert
    assert actual["object"] == "chat.completion"
    assert actual["choices"][0]["message"] == {
        "role": "assistant",
        "content": "응답",
    }
    assert actual["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


def test_build_chunk_finish_reason과usage는선택적이다() -> None:
    # Arrange / Act
    without = translate.build_chunk(
        completion_id="chatcmpl-1",
        created_unix=0,
        model_id="m",
        delta={"content": "가"},
    )
    with_usage = translate.build_chunk(
        completion_id="chatcmpl-1",
        created_unix=0,
        model_id="m",
        delta={},
        finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )

    # Assert
    assert without["object"] == "chat.completion.chunk"
    assert without["choices"][0]["finish_reason"] is None
    assert "usage" not in without
    assert with_usage["choices"][0]["finish_reason"] == "stop"
    assert with_usage["usage"]["total_tokens"] == 3


def test_build_model_list_owned_by는리전접두어를제거한공급자() -> None:
    # Arrange / Act
    actual = translate.build_model_list(
        ["amazon.nova-lite-v1:0", "us.anthropic.claude-3-haiku"]
    )

    # Assert
    assert actual["object"] == "list"
    # 추론 프로파일의 `us.` 접두어를 공급자로 잡으면 Anthropic 모델이
    # `us` 공급자로 분류된다.
    assert [entry["owned_by"] for entry in actual["data"]] == [
        "amazon",
        "anthropic",
    ]


def test_build_model_list_빈목록도유효한응답() -> None:
    # Arrange / Act
    actual = translate.build_model_list([])

    # Assert
    assert actual == {"object": "list", "data": []}


def test_chat_completion_request_n이2이상이면검증실패() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):  # noqa: PT011
        _request(n=2)


def test_chat_completion_request_messages가비면검증실패() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):  # noqa: PT011
        _request(messages=[])
