"""OpenAI 호환 엔드포인트 테스트.

`fastapi.testclient.TestClient` 로 실제 라우팅·의존성·예외 핸들러를 모두
거친다. Bedrock 만 대역으로 바꾼다.
"""

from __future__ import annotations

import decimal
import json
import typing

from fastapi import testclient
import pytest

import conftest
from llmgw import domain
from llmgw import repository


def test_healthz_인증없이200을반환한다(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_루트는대시보드로리다이렉트한다(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/", follow_redirects=False)

    # Assert
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui/"


def test_요청ID가응답헤더에실린다(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/healthz", headers={"X-Request-Id": "abc-123"})

    # Assert
    assert response.headers["X-Request-Id"] == "abc-123"


# -- /v1/models -------------------------------------------------------------


def test_models_허용목록이없으면전체를반환한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Arrange / Act
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {api_key}"}
    )

    # Assert
    assert response.status_code == 200
    ids = [entry["id"] for entry in response.json()["data"]]
    assert ids == [
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0",
    ]


def test_models_키의허용목록으로필터링한다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    restricted = conftest.seed_api_key(
        registry,
        key_id="key-restricted",
        allowed_models=("amazon.nova-lite-v1:0",),
    )

    # Act
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {restricted}"}
    )

    # Assert
    ids = [entry["id"] for entry in response.json()["data"]]
    assert ids == ["amazon.nova-lite-v1:0"]


def test_models_기반모델로등록하면추론프로파일도노출된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = conftest.seed_api_key(
        registry,
        key_id="key-profile",
        allowed_models=("anthropic.claude-3-haiku-20240307-v1:0",),
    )

    # Act
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )

    # Assert
    ids = [entry["id"] for entry in response.json()["data"]]
    assert ids == ["us.anthropic.claude-3-haiku-20240307-v1:0"]


def test_models_인증없으면401(client: testclient.TestClient) -> None:
    # Arrange / Act
    response = client.get("/v1/models")

    # Assert
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# -- /v1/chat/completions 정상 경로 -----------------------------------------


def test_chat_completions_정상응답을반환한다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "max_tokens": 64,
        },
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "안녕하세요"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }


def test_chat_completions_사용량이집계된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange / Act
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].requests == 1
    assert totals["TOTAL"].input_tokens == 12
    assert totals["TOTAL"].output_tokens == 5
    assert totals["TEAM#platform"].requests == 1
    assert totals["USER#alice"].requests == 1
    assert totals["MODEL#amazon.nova-lite-v1:0"].requests == 1
    # 픽스처 단가: 입력 0.001, 출력 0.002 USD/1K
    # 12/1000*0.001 + 5/1000*0.002 = 1.2e-5 + 1.0e-5 = 2.2e-5
    assert totals["TOTAL"].cost_usd == decimal.Decimal("0.0000220000")


def test_chat_completions_동일X_Request_Id를재사용해도호출마다집계된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
) -> None:
    """클라이언트가 지정한 요청 ID 로 집계를 건너뛸 수 없어야 한다.

    Bedrock 호출은 한 번마다 실제 비용이 발생한다. 같은 `X-Request-Id` 로
    기록을 건너뛰면 사용량이 집계에 반영되지 않아 월 예산 검사가 영원히
    통과하고 청구 배분에서도 빠진다. 헤더 하나로 과금과 예산을 우회할 수
    있으므로 호출 횟수만큼 집계해야 한다.
    """
    # Arrange
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Request-Id": "retry-me",
    }
    body = {
        "model": "amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "안녕"}],
    }

    # Act
    first = client.post("/v1/chat/completions", headers=headers, json=body)
    second = client.post("/v1/chat/completions", headers=headers, json=body)

    # Assert
    assert first.status_code == 200
    assert second.status_code == 200
    # 상관관계 ID 는 클라이언트가 보낸 값을 그대로 돌려준다.
    assert first.headers["X-Request-Id"] == "retry-me"
    assert second.headers["X-Request-Id"] == "retry-me"
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert (
        totals["TOTAL"].requests == 2
    ), f"기대 2, 실제 {totals['TOTAL'].requests}"
    # 예산 검사가 읽는 축도 함께 늘어야 한다.
    assert totals["USER#alice"].requests == 2
    assert totals["TOTAL"].cost_usd == decimal.Decimal("0.0000440000")


def test_chat_completions_요청ID가없으면매번새로집계된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "model": "amazon.nova-lite-v1:0",
        "messages": [{"role": "user", "content": "안녕"}],
    }

    # Act
    client.post("/v1/chat/completions", headers=headers, json=body)
    client.post("/v1/chat/completions", headers=headers, json=body)

    # Assert
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].requests == 2


def test_chat_completions_시스템메시지를Bedrock의system으로넘긴다(
    client: testclient.TestClient,
    api_key: str,
    fake_bedrock: conftest.FakeBedrock,
) -> None:
    # Arrange / Act
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [
                {"role": "system", "content": "너는 번역가다"},
                {"role": "user", "content": "hello"},
            ],
        },
    )

    # Assert
    assert fake_bedrock.last_call is not None
    assert fake_bedrock.last_call["system"] == [{"text": "너는 번역가다"}]


# -- /v1/chat/completions 실패 경로 -----------------------------------------


def test_chat_completions_인증없음_401이고사용량을남기지않는다(
    client: testclient.TestClient, usage_store: repository.UsageStore
) -> None:
    """주체를 알 수 없는 요청을 특정 계정에 청구할 수 없다."""
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 401
    assert usage_store.list_records("acme", "2026-08-23") == []


def test_chat_completions_허용되지않은모델_403이고실패로집계된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    restricted = conftest.seed_api_key(
        registry,
        key_id="key-restricted",
        allowed_models=("amazon.nova-lite-v1:0",),
    )

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {restricted}"},
        json={
            "model": "amazon.nova-pro-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].error_requests == 1
    assert totals["TOTAL"].cost_usd == decimal.Decimal("0")


def test_chat_completions_예산초과_429이고실패로집계된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    budgeted = conftest.seed_api_key(
        registry,
        key_id="key-budget",
        monthly_budget_usd=decimal.Decimal("0"),
    )

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {budgeted}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "budget_exceeded"
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["KEY#key-budget"].error_requests == 1


def test_chat_completions_assistant로시작하면400(
    client: testclient.TestClient, api_key: str
) -> None:
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "assistant", "content": "먼저 말함"}],
        },
    )

    # Assert
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_스키마검증실패_400이고OpenAI형식(
    client: testclient.TestClient, api_key: str
) -> None:
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "amazon.nova-lite-v1:0"},
    )

    # Assert
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "messages" in body["error"]["message"]


def test_chat_completions_Bedrock스로틀링_429로변환된다(
    client: testclient.TestClient,
    api_key: str,
    fake_bedrock: conftest.FakeBedrock,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    from llmgw import errors

    fake_bedrock.raise_on_converse = errors.UpstreamRateLimitError(
        "ThrottlingException: too many"
    )

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_throttled"
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].error_requests == 1


def test_chat_completions_비활성키_401(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    disabled = conftest.seed_api_key(
        registry,
        key_id="key-disabled",
        status=domain.EntityStatus.DISABLED,
    )

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {disabled}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 401


# -- 스트리밍 ---------------------------------------------------------------


def _parse_sse(text: str) -> list[typing.Any]:
    """SSE 본문에서 JSON 프레임만 뽑는다."""
    frames: list[typing.Any] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            frames.append("[DONE]")
            continue
        frames.append(json.loads(payload))
    return frames


def test_chat_completions_스트리밍_증분과종료프레임을보낸다(
    client: testclient.TestClient, api_key: str
) -> None:
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "stream": True,
        },
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(response.text)
    assert frames[-1] == "[DONE]"
    assert frames[0]["choices"][0]["delta"] == {
        "role": "assistant",
        "content": "",
    }
    contents = [
        frame["choices"][0]["delta"].get("content")
        for frame in frames[1:-2]
        if isinstance(frame, dict)
    ]
    assert "".join(part for part in contents if part) == "안녕하세요"
    final = frames[-2]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == 17


def test_chat_completions_스트리밍_사용량이집계된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange / Act
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "stream": True,
        },
    )

    # Assert
    records = usage_store.list_records("acme", "2026-08-23")
    assert len(records) == 1
    assert bool(records[0]["streamed"]) is True
    assert int(records[0]["input_tokens"]) == 12
    assert int(records[0]["output_tokens"]) == 5


def test_chat_completions_스트리밍중오류_본문에에러가실리고실패로집계된다(
    client: testclient.TestClient,
    api_key: str,
    fake_bedrock: conftest.FakeBedrock,
    usage_store: repository.UsageStore,
) -> None:
    """헤더를 이미 보낸 뒤라 상태 코드는 200 이지만 집계는 실패여야 한다."""
    # Arrange
    from llmgw import errors

    fake_bedrock.raise_on_stream = errors.UpstreamError("스트림 오류")

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "stream": True,
        },
    )

    # Assert
    assert response.status_code == 200
    frames = _parse_sse(response.text)
    error_frames = [
        frame
        for frame in frames
        if isinstance(frame, dict) and "error" in frame
    ]
    assert error_frames, f"에러 프레임이 없다: {frames}"
    assert error_frames[0]["error"]["code"] == "upstream_error"
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].error_requests == 1


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_단가없는모델_요청은성공하고비용0으로집계된다(
    client: testclient.TestClient,
    api_key: str,
    usage_store: repository.UsageStore,
    stream: bool,
) -> None:
    """새 모델 등장 시 게이트웨이가 요청을 거부하면 가용성이 떨어진다."""
    # Arrange / Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-pro-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "stream": stream,
        },
    )

    # Assert
    assert response.status_code == 200
    records = usage_store.list_records("acme", "2026-08-23")
    assert len(records) == 1
    assert bool(records[0]["pricing_known"]) is False
    assert decimal.Decimal(str(records[0]["cost_usd"])) == 0
