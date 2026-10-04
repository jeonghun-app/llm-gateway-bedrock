"""협의체 코드 리뷰에서 나온 결함의 회귀 테스트.

각 테스트는 수정 전에 실패한다. 여기 모아 둔 이유는 이 결함들이 공통적으로
**조용히 잘못되는** 종류라서다. 예외가 나지 않고, 응답은 그럴듯하고, 기존
테스트는 통과한다. 그래서 명시적으로 고정한다.
"""

from __future__ import annotations

import io
import json
import typing

from fastapi import testclient
import pytest

import conftest
from llmgw import bedrock
from llmgw import cache
from llmgw import domain
from llmgw import errors
from llmgw import observability
from llmgw import repository
from llmgw import schemas
from llmgw import translate

_TITAN = "amazon.titan-embed-text-v2:0"
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
_PNG_URL = f"data:image/png;base64,{_PNG}"


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


# ---------------------------------------------------------------------------
# B1: 배치 중간 실패 시 이미 청구된 토큰을 잃지 않는다 (예산 우회)
# ---------------------------------------------------------------------------


class _FailAfterN:
    """N 번째 호출부터 실패하는 invoke_model 대역."""

    def __init__(self, fail_at: int, token_count: int = 5) -> None:
        self.fail_at = fail_at
        self.token_count = token_count
        self.calls = 0

    def invoke_model(self, **params: typing.Any) -> dict[str, typing.Any]:
        """지정한 순번부터 예외를 던진다."""
        del params
        self.calls += 1
        if self.calls >= self.fail_at:
            raise botocore_client_error()
        return {
            "body": io.BytesIO(
                json.dumps(
                    {
                        "embedding": [0.1, 0.2],
                        "inputTextTokenCount": self.token_count,
                    }
                ).encode()
            )
        }


def botocore_client_error() -> Exception:
    """Bedrock 스로틀링 오류를 만든다."""
    import botocore.exceptions

    return botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "InvokeModel",
    )


class _FakeBody:
    """지정한 순번부터 `.read()` 가 BotoCoreError 를 던지는 응답 본문 대역."""

    def __init__(self, raise_at_call: list[int], token_count: int) -> None:
        self._raise_at_call = raise_at_call
        self._token_count = token_count

    def read(self) -> bytes:
        """읽을 때 예외를 던지거나 정상 본문을 돌려준다."""
        if self._raise_at_call:
            import botocore.exceptions

            raise botocore.exceptions.ReadTimeoutError(
                endpoint_url="https://bedrock-runtime.example.com"
            )
        return json.dumps(
            {
                "embedding": [0.1, 0.2],
                "inputTextTokenCount": self._token_count,
            }
        ).encode()


class _FailReadAfterN:
    """N 번째 호출부터 응답 본문을 읽을 때 BotoCoreError 가 나는 대역.

    `invoke_model` 자체는 성공한다 — 네트워크 스트림을 다 받은 뒤 몸체를
    읽다가 타임아웃이 나는 상황(`ReadTimeoutError`)을 재현한다.
    """

    def __init__(self, fail_at: int, token_count: int = 5) -> None:
        self.fail_at = fail_at
        self.token_count = token_count
        self.calls = 0

    def invoke_model(self, **params: typing.Any) -> dict[str, typing.Any]:
        """항상 성공하지만 N 번째 응답의 본문은 읽을 때 예외가 난다."""
        del params
        self.calls += 1
        raise_at_call = [1] if self.calls >= self.fail_at else []
        return {"body": _FakeBody(raise_at_call, self.token_count)}


def test_응답본문을읽다가BotoCoreError가나도이미소비된토큰이보존된다() -> None:
    """`invoke_model` 호출 자체가 아니라 응답 본문을 읽다가 나는
    `BotoCoreError`(예: `ReadTimeoutError`)도 변환해야 한다.

    이 예외가 `GatewayError` 로 바뀌지 않으면 라우터의
    `except errors.GatewayError` 에 잡히지 않아 앞서 성공한 호출들의 토큰이
    사용량에 기록되지 못한 채 사라진다.
    """
    runtime = _FailReadAfterN(fail_at=3, token_count=5)

    with pytest.raises(errors.GatewayError) as caught:
        _gateway(runtime).embed(model_id=_TITAN, texts=["a", "b", "c", "d"])

    # 1·2번째는 성공했다 → 5 + 5 = 10
    assert caught.value.consumed_input_tokens == 10


def test_배치중간실패시이미소비된토큰이예외에실린다() -> None:
    """3번째에서 실패하면 앞선 2번의 토큰이 보존돼야 한다.

    0 으로 기록하면 이미 청구된 비용이 집계에서 사라진다. 마지막 입력만
    실패하게 만들면 월 예산을 무한히 우회할 수 있으므로 통제 우회다.
    """
    runtime = _FailAfterN(fail_at=3, token_count=5)

    with pytest.raises(errors.GatewayError) as caught:
        _gateway(runtime).embed(model_id=_TITAN, texts=["a", "b", "c", "d"])

    # 1·2번째는 성공했다 → 5 + 5 = 10
    assert caught.value.consumed_input_tokens == 10


def test_배치중간실패가사용량에기록된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
    fake_bedrock: typing.Any,
) -> None:
    # Arrange: 어댑터가 소비 토큰을 실은 예외를 던지는 상황을 재현한다.
    failure = errors.UpstreamRateLimitError("throttled")
    failure.consumed_input_tokens = 21
    fake_bedrock.raise_on_embed = failure

    # Act
    response = client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": _TITAN, "input": ["a", "b", "c"]},
    )

    # Assert
    assert response.status_code == failure.status_code
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    # 실패했지만 이미 청구된 토큰은 남아야 한다.
    assert totals["TOTAL"].input_tokens == 21
    assert totals["TOTAL"].cost_usd > 0


def test_벡터가숫자가아니면도메인예외로바꾼다() -> None:
    # float() 의 ValueError 는 GatewayError 가 아니라서 라우터가 잡지 못한다.
    # 그러면 이미 소비된 토큰을 기록하지 못한 채 500 이 나간다.
    runtime = _FakePayload({"embedding": ["x"], "inputTextTokenCount": 3})

    with pytest.raises(errors.UpstreamError, match="숫자로 해석"):
        _gateway(runtime).embed(model_id=_TITAN, texts=["a"])


class _FakePayload:
    """고정 페이로드를 돌려주는 invoke_model 대역."""

    def __init__(self, payload: dict[str, typing.Any]) -> None:
        self._payload = payload

    def invoke_model(self, **params: typing.Any) -> dict[str, typing.Any]:
        """고정 응답을 반환한다."""
        del params
        return {"body": io.BytesIO(json.dumps(self._payload).encode())}


@pytest.mark.parametrize("token_count", [True, -1, "5", None, 1.5], ids=str)
def test_토큰수가정상int가아니면거부한다(token_count: typing.Any) -> None:
    # bool 은 int 의 하위 타입이라 isinstance 만으로는 걸러지지 않는다.
    # 음수는 음수 비용을 만들어 집계를 되돌린다.
    runtime = _FakePayload(
        {"embedding": [0.1], "inputTextTokenCount": token_count}
    )

    with pytest.raises(errors.UpstreamError, match="입력 토큰 수"):
        _gateway(runtime).embed(model_id=_TITAN, texts=["a"])


def test_유한하지않은벡터값은거부한다() -> None:
    runtime = _FakePayload(
        {"embedding": [float("nan")], "inputTextTokenCount": 3}
    )

    with pytest.raises(errors.UpstreamError, match="유한하지 않은"):
        _gateway(runtime).embed(model_id=_TITAN, texts=["a"])


# ---------------------------------------------------------------------------
# B4: 구조화 출력의 finish_reason 과 사용량 레코드
# ---------------------------------------------------------------------------


def test_구조화출력에서잘린응답은length로보고한다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    """`max_tokens` 로 잘렸는데 stop 으로 보고하면 안 된다.

    클라이언트는 잘린 JSON 을 완전한 결과로 읽는다. translate 의 매핑 표가
    이미 length 로 정하고 있는데 호출부에서 덮어쓰던 문제다.
    """
    fake_bedrock.tool_calls = (
        translate.ToolUse("tu_1", "answer", '{"city": "서울"}'),
    )
    fake_bedrock.stop_reason = "max_tokens"

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": {"type": "object", "properties": {}},
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "length"


def test_구조화출력에서가드레일개입은content_filter로보고한다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    """가드레일이 막은 요청을 "모델이 toolChoice 를 지원하지 않는다" 는
    502 로 다루면 안 된다.

    가드레일이 개입하면 Converse 는 도구 호출 없이 차단 문구와
    `guardrail_intervened` 만 돌려준다. 이를 `unwrap_structured_output` 의
    "모델이 합성 도구를 호출하지 않았다" 경로로 처리하면 가드레일에 걸린
    요청마다 502 가 나가 OpenAI SDK 가 재시도하며 반복 청구되고, 대시보드에는
    가드레일 개입이 아니라 upstream 오류로 남는다.
    """
    fake_bedrock.tool_calls = ()
    fake_bedrock.stop_reason = "guardrail_intervened"

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": {"type": "object", "properties": {}},
                },
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "content_filter"
    assert (
        body["choices"][0]["message"]["content"] == conftest.FAKE_RESPONSE_TEXT
    )


def test_구조화출력실패는사용량에실패로기록된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
    fake_bedrock: typing.Any,
) -> None:
    # 모델이 합성 도구를 호출하지 않으면 502 가 나간다. 그런데 성공 레코드를
    # 먼저 쓰면 대시보드는 200 으로 보인다. 에러율이 과소 보고되고, 그것도
    # 운영자가 가장 찾아야 하는 경우에 과소 보고된다.
    fake_bedrock.tool_calls = ()
    fake_bedrock.stop_reason = "end_turn"

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": {"type": "object", "properties": {}},
                },
            },
        },
    )

    assert response.status_code >= 500
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    # 토큰은 실제로 소비됐으므로 기록돼야 하고, 성공으로 세어지면 안 된다.
    assert totals["TOTAL"].input_tokens == conftest.FAKE_INPUT_TOKENS
    assert totals["TOTAL"].error_requests == 1


# ---------------------------------------------------------------------------
# M5 / M6: 조용한 이미지 유실
# ---------------------------------------------------------------------------


def test_system메시지의이미지는거부한다() -> None:
    # Converse 의 system 파라미터는 텍스트만 받는다. 조용히 버리면 보낸 사람은
    # 모델이 이미지를 보고 답한 것으로 오해한다.
    request = schemas.ChatCompletionRequest.model_validate(
        {
            "model": "amazon.nova-lite-v1:0",
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "지침"},
                        {"type": "image_url", "image_url": {"url": _PNG_URL}},
                    ],
                },
                {"role": "user", "content": "안녕"},
            ],
        }
    )

    with pytest.raises(errors.InvalidRequestError, match="system/developer"):
        translate.to_bedrock_request(request)


def test_도구결과메시지의이미지는거부한다() -> None:
    request = schemas.ChatCompletionRequest.model_validate(
        {
            "model": "amazon.nova-lite-v1:0",
            "messages": [
                {"role": "user", "content": "안녕"},
                {
                    "role": "tool",
                    "tool_call_id": "t1",
                    "content": [
                        {"type": "text", "text": "결과"},
                        {"type": "image_url", "image_url": {"url": _PNG_URL}},
                    ],
                },
            ],
        }
    )

    with pytest.raises(errors.InvalidRequestError, match="role=tool"):
        translate.to_bedrock_request(request)


def test_image_url객체가없으면거부한다() -> None:
    # 단축 평가로 넘기면 조각이 조용히 사라진다.
    request = schemas.ChatCompletionRequest.model_validate(
        {
            "model": "amazon.nova-lite-v1:0",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url"}],
                }
            ],
        }
    )

    with pytest.raises(errors.InvalidRequestError, match="image_url 객체"):
        translate.to_bedrock_request(request)


# ---------------------------------------------------------------------------
# M11: 이미지 자원 상한
# ---------------------------------------------------------------------------


def _image_request(count: int, payload_bytes: int) -> typing.Any:
    """이미지 count 장을 담은 요청을 만든다."""
    import base64

    blob = base64.b64encode(b"\x00" * payload_bytes).decode()
    return schemas.ChatCompletionRequest.model_validate(
        {
            "model": "amazon.nova-lite-v1:0",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{blob}"
                            },
                        }
                        for _ in range(count)
                    ],
                }
            ],
        }
    )


def test_이미지개수상한을넘으면거부한다() -> None:
    request = _image_request(translate.MAX_IMAGES_PER_REQUEST + 1, 16)

    with pytest.raises(errors.InvalidRequestError, match="이미지 수 상한"):
        translate.to_bedrock_request(request)


def test_이미지총량상한을넘으면거부한다() -> None:
    # 개별 상한은 넘지 않지만 합계가 넘는 경우. 개별 검사만으로는 통과한다.
    per_image = translate.MAX_IMAGE_BYTES - 1
    count = translate.MAX_TOTAL_IMAGE_BYTES // per_image + 1
    assert count <= translate.MAX_IMAGES_PER_REQUEST, "테스트 전제 확인"
    request = _image_request(count, per_image)

    with pytest.raises(errors.InvalidRequestError, match="총량 상한"):
        translate.to_bedrock_request(request)


# ---------------------------------------------------------------------------
# SSE 도구 프레임 (협의체 리뷰: 이 코드에 테스트가 0건이었다)
# ---------------------------------------------------------------------------


def _sse_chunks(body: str) -> list[dict[str, typing.Any]]:
    """SSE 본문에서 JSON 청크만 뽑는다."""
    chunks: list[dict[str, typing.Any]] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            continue
        chunks.append(json.loads(payload))
    return chunks


def test_SSE도구프레임이OpenAI형식을지킨다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    """첫 프레임만 id/type/name 을 싣고, 이후는 arguments 조각만 싣는다.

    이 조립이 틀리면 예외가 나지 않는다. 클라이언트 SDK 가 인자를 이어붙일 때
    깨진 JSON 이 되어 그쪽에서 터진다. 그래서 와이어 형식을 직접 고정한다.
    """
    # Arrange: 텍스트 → 도구 두 개. 텍스트가 앞에 오는 순서가 중요하다.
    fake_bedrock.stream_tool_deltas = (
        bedrock.StreamDelta(text="네"),
        bedrock.StreamDelta(tool_index=0, tool_use_id="tu_a", tool_name="f1"),
        bedrock.StreamDelta(tool_index=0, tool_arguments_delta='{"x"'),
        bedrock.StreamDelta(tool_index=0, tool_arguments_delta=":1}"),
        bedrock.StreamDelta(tool_index=1, tool_use_id="tu_b", tool_name="f2"),
        bedrock.StreamDelta(tool_index=1, tool_arguments_delta="{}"),
    )
    fake_bedrock.stop_reason = "tool_use"

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "서울 날씨"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f1",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "stream": True,
        },
    )

    # Assert
    assert response.status_code == 200
    chunks = _sse_chunks(response.text)
    tool_frames = [
        chunk["choices"][0]["delta"]["tool_calls"][0]
        for chunk in chunks
        if chunk["choices"] and "tool_calls" in chunk["choices"][0]["delta"]
    ]

    starts = [frame for frame in tool_frames if "id" in frame]
    assert [frame["id"] for frame in starts] == ["tu_a", "tu_b"]
    # 도구 순번은 도구만 세는 번호다. 텍스트가 앞에 있어도 0 부터 시작한다.
    assert [frame["index"] for frame in starts] == [0, 1]
    for frame in starts:
        assert frame["type"] == "function"
        assert frame["function"]["arguments"] == ""

    # 이후 프레임은 arguments 만 싣는다. id/type/name 을 다시 보내면 SDK 가
    # 이름을 덧붙여 망친다.
    increments = [frame for frame in tool_frames if "id" not in frame]
    for frame in increments:
        assert set(frame) == {"index", "function"}
        assert set(frame["function"]) == {"arguments"}

    # 인자를 순번별로 이어붙이면 유효한 JSON 이 되어야 한다.
    joined: dict[int, str] = {}
    for frame in increments:
        joined.setdefault(frame["index"], "")
        joined[frame["index"]] += frame["function"]["arguments"]
    assert json.loads(joined[0]) == {"x": 1}
    assert json.loads(joined[1]) == {}

    # 종료 프레임의 finish_reason.
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_SSE텍스트와도구가섞여도순서가유지된다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    fake_bedrock.stream_tool_deltas = (
        bedrock.StreamDelta(text="먼저"),
        bedrock.StreamDelta(tool_index=0, tool_use_id="t", tool_name="f"),
        bedrock.StreamDelta(tool_index=0, tool_arguments_delta="{}"),
    )
    fake_bedrock.stop_reason = "tool_use"

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "stream": True,
        },
    )

    chunks = _sse_chunks(response.text)
    kinds = [
        (
            "text"
            if chunk["choices"] and chunk["choices"][0]["delta"].get("content")
            else (
                "tool"
                if chunk["choices"]
                and "tool_calls" in chunk["choices"][0]["delta"]
                else "other"
            )
        )
        for chunk in chunks
    ]
    # 텍스트가 도구보다 앞에 나와야 한다.
    assert kinds.index("text") < kinds.index("tool")
