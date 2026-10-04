"""`POST /v1/embeddings` 테스트.

임베딩은 `Converse` 가 아니라 `InvokeModel` 을 쓰는 유일한 경로다. 즉 모델
계열마다 본문 형태가 달라 게이트웨이가 직접 만들어야 하고, 응답에서 입력 토큰
수를 못 받으면 비용이 0 으로 집계된다. **예산이 걸린 주체에게 비용 0 은 통제가
조용히 무효가 되는 것**이므로, 토큰 수를 확인한 계열만 지원하고 나머지는
거부하는지 검증한다.
"""

from __future__ import annotations

import base64
import decimal
import io
import json
import struct
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
from llmgw import translate

_TITAN = "amazon.titan-embed-text-v2:0"


def _embed(
    client: testclient.TestClient,
    api_key: str,
    **overrides: typing.Any,
) -> typing.Any:
    """임베딩 요청을 보낸다."""
    body: dict[str, typing.Any] = {"model": _TITAN, "input": "안녕하세요"}
    body.update(overrides)
    return client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )


# ---------------------------------------------------------------------------
# 기본 동작
# ---------------------------------------------------------------------------


def test_문자열입력을임베딩한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Act
    response = _embed(client, api_key)

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1
    assert body["data"][0]["object"] == "embedding"
    assert body["data"][0]["index"] == 0
    assert isinstance(body["data"][0]["embedding"], list)
    # 임베딩은 출력 토큰이 없다. OpenAI 도 이 엔드포인트에서
    # completion_tokens 를 주지 않는다.
    assert body["usage"] == {
        "prompt_tokens": conftest.FAKE_EMBED_TOKENS,
        "total_tokens": conftest.FAKE_EMBED_TOKENS,
    }
    assert "completion_tokens" not in body["usage"]


def test_배열입력은순서를보존한다(
    client: testclient.TestClient, api_key: str
) -> None:
    response = _embed(client, api_key, input=["첫째", "둘째", "셋째"])

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["index"] for item in data] == [0, 1, 2]
    # 대역은 순번+1 로 채운다. 순서가 섞이면 값이 어긋난다.
    assert data[0]["embedding"][0] == 1.0
    assert data[2]["embedding"][0] == 3.0


def test_base64형식으로인코딩한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # OpenAI 파이썬 클라이언트는 기본으로 base64 를 요청한다. 거부하면
    # 공식 SDK 경로가 동작하지 않는다.
    response = _embed(client, api_key, encoding_format="base64")

    assert response.status_code == 200
    encoded = response.json()["data"][0]["embedding"]
    assert isinstance(encoded, str)
    raw = base64.b64decode(encoded)
    decoded = struct.unpack(f"<{len(raw) // 4}f", raw)
    assert decoded == (1.0, 1.0, 1.0)


def test_dimensions를모델에전달한다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    response = _embed(client, api_key, dimensions=256)

    assert response.status_code == 200
    assert fake_bedrock.last_embed_call["dimensions"] == 256


# ---------------------------------------------------------------------------
# 거부
# ---------------------------------------------------------------------------


def test_지원하지않는임베딩모델은거부한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Cohere 는 토큰 수 보고 여부를 확인하지 못해 지원 목록에 없다.
    response = _embed(client, api_key, model="cohere.embed-v4:0")

    assert response.status_code == 400


def test_빈입력은거부한다(client: testclient.TestClient, api_key: str) -> None:
    response = _embed(client, api_key, input="")

    assert response.status_code == 400


def test_배열에빈문자열이섞이면호출전에거부한다(
    client: testclient.TestClient,
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    # 모든 항목이 아니라 하나라도 비면 거부해야 한다. 통과시키면 앞선
    # 입력은 이미 Bedrock 에 청구된 뒤에야 실패해 비용이 낭비된다.
    response = _embed(client, api_key, input=["안녕하세요", ""])

    assert response.status_code == 400
    assert fake_bedrock.last_embed_call is None


def test_토큰ID배열은거부한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Bedrock 은 텍스트만 받는다. 역토큰화를 추측하면 청구 대상 입력이
    # 달라진다.
    response = _embed(client, api_key, input=[[1, 2, 3]])

    assert response.status_code == 400


def test_배치상한을넘으면거부한다(
    client: testclient.TestClient, api_key: str
) -> None:
    response = _embed(client, api_key, input=["x"] * 200)

    assert response.status_code == 400


def test_인증없이는거부한다(client: testclient.TestClient) -> None:
    response = client.post(
        "/v1/embeddings", json={"model": _TITAN, "input": "안녕"}
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 채팅 경로와의 분리
# ---------------------------------------------------------------------------


def test_임베딩모델을채팅으로보내면안내와함께거부한다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    # Bedrock 이 내는 "This action doesn't support the model" 만으로는
    # 어디로 보내야 하는지 알 수 없다.
    fake_bedrock.last_call = None
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": _TITAN,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    assert response.status_code == 400
    assert "/v1/embeddings" in response.json()["error"]["message"]
    assert fake_bedrock.last_call is None


# ---------------------------------------------------------------------------
# 비용 정합성
# ---------------------------------------------------------------------------


def test_사용량이출력토큰0으로기록된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
) -> None:
    # Act
    response = _embed(client, api_key, input=["첫째", "둘째"])

    # Assert
    assert response.status_code == 200
    expected_tokens = conftest.FAKE_EMBED_TOKENS * 2
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].requests == 1
    assert totals["TOTAL"].input_tokens == expected_tokens
    # 임베딩은 출력 토큰이 없는 것이 정상이다.
    assert totals["TOTAL"].output_tokens == 0
    assert totals[f"MODEL#{_TITAN}"].requests == 1
    # 픽스처 단가: 입력 0.004 USD/1K, 출력 0. 14/1000*0.004 = 5.6e-5
    assert totals["TOTAL"].cost_usd == decimal.Decimal("0.0000560000")
    # 단가를 아는 모델이므로 미등록 카운터가 오르지 않아야 한다.
    assert totals["TOTAL"].unpriced_requests == 0


def test_예산초과키는임베딩도차단된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
) -> None:
    # 임베딩을 예외로 두면 예산을 우회하는 경로가 된다.
    key = conftest.seed_api_key(
        registry,
        key_id="key-budget",
        monthly_budget_usd=decimal.Decimal("0"),
    )

    response = _embed(client, key)

    assert response.status_code == 429


def test_허용목록에없는임베딩모델은거부된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
) -> None:
    key = conftest.seed_api_key(
        registry,
        key_id="key-restricted",
        allowed_models=("amazon.nova-lite-v1:0",),
    )

    response = _embed(client, key)

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 어댑터 (InvokeModel 본문·응답 해석)
# ---------------------------------------------------------------------------


class _FakeInvokeRuntime:
    """invoke_model 을 흉내내는 대역."""

    def __init__(self, payload: dict[str, typing.Any]) -> None:
        self._payload = payload
        self.captured: dict[str, typing.Any] = {}

    def invoke_model(self, **params: typing.Any) -> dict[str, typing.Any]:
        """요청을 기록하고 고정 응답을 반환한다."""
        self.captured = params
        return {"body": io.BytesIO(json.dumps(self._payload).encode())}


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


def test_titan본문과응답을해석한다() -> None:
    # Arrange
    runtime = _FakeInvokeRuntime(
        {"embedding": [0.1, 0.2], "inputTextTokenCount": 4}
    )

    # Act
    result = _gateway(runtime).embed(
        model_id=_TITAN, texts=["안녕"], dimensions=512
    )

    # Assert
    body = json.loads(runtime.captured["body"])
    assert body == {"inputText": "안녕", "dimensions": 512}
    assert runtime.captured["modelId"] == _TITAN
    assert result.input_tokens == 4
    assert result.vectors == ((0.1, 0.2),)


def test_토큰수가없으면오류로만든다() -> None:
    # 토큰 수를 0 으로 기록하면 비용이 0 이 되어 예산이 조용히 무효가 된다.
    runtime = _FakeInvokeRuntime({"embedding": [0.1]})

    with pytest.raises(errors.UpstreamError, match="입력 토큰 수"):
        _gateway(runtime).embed(model_id=_TITAN, texts=["안녕"])


def test_벡터가없으면오류로만든다() -> None:
    runtime = _FakeInvokeRuntime({"inputTextTokenCount": 4})

    with pytest.raises(errors.UpstreamError, match="벡터가 없다"):
        _gateway(runtime).embed(model_id=_TITAN, texts=["안녕"])


def test_지원하지않는계열은호출전에거부한다() -> None:
    runtime = _FakeInvokeRuntime({})

    with pytest.raises(errors.InvalidRequestError, match="지원하지 않는"):
        _gateway(runtime).embed(model_id="cohere.embed-v4:0", texts=["안녕"])
    # Bedrock 을 호출하지 않았어야 한다.
    assert runtime.captured == {}


def test_배치는입력순서대로벡터를모은다() -> None:
    runtime = _FakeInvokeRuntime({"embedding": [1.0], "inputTextTokenCount": 2})

    result = _gateway(runtime).embed(model_id=_TITAN, texts=["a", "b", "c"])

    assert len(result.vectors) == 3
    # 토큰 수는 호출마다 합산된다.
    assert result.input_tokens == 6


# ---------------------------------------------------------------------------
# 응답 빌더
# ---------------------------------------------------------------------------


def test_float32범위를넘는벡터값은GatewayError로거부한다() -> None:
    # math.isfinite 는 유한한지만 보지, float32 범위(~3.4e38) 를 넘는지는
    # 보지 않는다. struct.pack 의 OverflowError 를 그대로 두면 500 이 나가며
    # 실패 레코드로 집계되지 않는다. 평범한 Exception 이 아니라 GatewayError
    # 로 바뀌어야 라우터의 실패 처리 경로가 잡을 수 있다.
    with pytest.raises(errors.GatewayError):
        translate.build_embedding_response(
            model_id=_TITAN,
            vectors=[[1e39]],
            input_tokens=3,
            base64_encoding=True,
        )


def test_임베딩응답에completion_tokens가없다() -> None:
    actual = translate.build_embedding_response(
        model_id=_TITAN, vectors=[[0.5]], input_tokens=3
    )

    assert actual["usage"] == {"prompt_tokens": 3, "total_tokens": 3}
    assert actual["model"] == _TITAN
