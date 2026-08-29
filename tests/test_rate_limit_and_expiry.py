"""레이트 리밋 · 키 만료 · 단가 미등록 정책 테스트.

세 기능 모두 **기본값이 기존 동작과 같아야** 한다. 한도 미설정, 만료 미설정,
`allow` 정책이 기본이므로 이 기능들을 켜지 않은 배포는 동작이 바뀌지 않는다.

레이트 리밋에서 가장 중요한 검증은 **원자성**이다. 태스크가 여러 개인 환경에서
인메모리 카운터를 쓰면 한도가 태스크 수만큼 늘어난다. DynamoDB 조건부 업데이트가
실제로 한도를 지키는지 확인한다.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import typing

from fastapi import testclient
import pytest

from llmgw import apikey
from llmgw import app as app_module
from llmgw import clock
from llmgw import config
from llmgw import domain
from llmgw import repository
from llmgw import services as services_module

_MODEL = "amazon.nova-lite-v1:0"
_UNPRICED_MODEL = "anthropic.claude-opus-5"


def _chat(
    client: testclient.TestClient, key: str, model: str = _MODEL
) -> typing.Any:
    """채팅 완성을 호출한다."""
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )


# ---------------------------------------------------------------------------
# 레이트 리밋 저장소 (원자성)
# ---------------------------------------------------------------------------


def test_레이트리밋_한도까지허용하고그다음을거부한다(
    usage_store: repository.UsageStore,
) -> None:
    """조건부 업데이트가 한도를 정확히 지켜야 한다."""
    # Arrange
    now = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
    kwargs = {
        "account_id": "acme",
        "scope": "KEY#k1",
        "minute": "2026-08-29T12:00",
        "limit": 3,
        "now": now,
    }

    # Act
    results = [usage_store.try_consume_rate_limit(**kwargs) for _ in range(5)]

    # Assert
    assert results == [True, True, True, False, False], results


def test_레이트리밋_분이바뀌면카운터가새로시작한다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    now = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
    base = {"account_id": "acme", "scope": "KEY#k1", "limit": 1, "now": now}

    # Act
    first = usage_store.try_consume_rate_limit(
        **base, minute="2026-08-29T12:00"
    )
    same = usage_store.try_consume_rate_limit(**base, minute="2026-08-29T12:00")
    next_minute = usage_store.try_consume_rate_limit(
        **base, minute="2026-08-29T12:01"
    )

    # Assert
    assert first is True
    assert same is False
    assert next_minute is True


def test_레이트리밋_주체별로카운터가분리된다(
    usage_store: repository.UsageStore,
) -> None:
    """한 키가 한도를 채워도 다른 키는 영향받지 않아야 한다."""
    # Arrange
    now = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
    base = {
        "account_id": "acme",
        "minute": "2026-08-29T12:00",
        "limit": 1,
        "now": now,
    }

    # Act / Assert
    assert usage_store.try_consume_rate_limit(**base, scope="KEY#k1") is True
    assert usage_store.try_consume_rate_limit(**base, scope="KEY#k1") is False
    assert usage_store.try_consume_rate_limit(**base, scope="KEY#k2") is True
    assert (
        usage_store.try_consume_rate_limit(**base, scope="USER#alice") is True
    )


def test_레이트리밋_카운터는사용량조회에섞이지않는다(
    usage_store: repository.UsageStore,
) -> None:
    """카운터가 usage 테이블을 쓰지만 사용량 표에 나타나면 안 된다."""
    # Arrange
    now = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
    usage_store.try_consume_rate_limit(
        account_id="acme",
        scope="KEY#k1",
        minute="2026-08-29T12:00",
        limit=10,
        now=now,
    )

    # Act
    records = usage_store.list_records("acme", "2026-08-29")

    # Assert
    assert records == []


# ---------------------------------------------------------------------------
# 레이트 리밋 (요청 경로)
# ---------------------------------------------------------------------------


def test_한도가없으면저장소를건드리지않는다(
    authenticator: typing.Any,
) -> None:
    """한도 미설정이 기본값이라 요청 경로에 왕복이 늘면 안 된다."""

    # Arrange
    class _Boom:
        def try_consume_rate_limit(self, **_: object) -> bool:
            raise AssertionError("한도가 없으면 호출되면 안 된다")

    authenticator._usage_store = _Boom()
    principal = domain.Principal(
        account_id="acme", user_id="alice", key_id="k", key_hash="h"
    )

    # Act / Assert: 예외가 나지 않아야 한다.
    authenticator.enforce_rate_limit(principal, clock.SYSTEM_CLOCK.now())


def test_한도초과시429와Retry_After를준다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    """Retry-After 가 없으면 OpenAI 클라이언트가 즉시 재시도해 거부가
    반복되고 서로 부하만 늘어난다."""
    # Arrange: 이 키의 분당 한도를 1로 좁힌다.
    stored = registry.get_api_key_by_hash(apikey.hash_api_key(api_key))
    assert stored is not None
    registry.update_api_key(stored.model_copy(update={"rpm_limit": 1}))

    # Act
    first = _chat(client, api_key)
    second = _chat(client, api_key)

    # Assert
    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
    retry_after = second.headers.get("retry-after")
    assert retry_after is not None, "Retry-After 헤더가 없다"
    assert 1 <= int(retry_after) <= 60


def test_한도초과요청도사용량에집계된다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
    api_key: str,
) -> None:
    """인증 이후의 실패는 실패 요청으로 집계돼야 에러율이 실제를 반영한다."""
    # Arrange
    stored = registry.get_api_key_by_hash(apikey.hash_api_key(api_key))
    assert stored is not None
    registry.update_api_key(stored.model_copy(update={"rpm_limit": 1}))

    # Act
    _chat(client, api_key)
    _chat(client, api_key)

    # Assert
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert totals.requests == 2
    assert totals.error_requests == 1


# ---------------------------------------------------------------------------
# API 키 만료
# ---------------------------------------------------------------------------


def test_만료된키는401이고삭제되지않는다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    """키를 지우면 사용량 집계의 KEY# 축에 이름을 붙일 수 없다."""
    # Arrange
    stored = registry.get_api_key_by_hash(apikey.hash_api_key(api_key))
    assert stored is not None
    registry.update_api_key(
        stored.model_copy(update={"expires_at": "2020-01-01T00:00:00Z"})
    )

    # Act
    response = _chat(client, api_key)

    # Assert
    assert response.status_code == 401
    # 키는 남아 있어야 한다.
    assert (
        registry.get_api_key_by_hash(apikey.hash_api_key(api_key)) is not None
    )


def test_만료전이면정상동작한다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    # Arrange
    stored = registry.get_api_key_by_hash(apikey.hash_api_key(api_key))
    assert stored is not None
    registry.update_api_key(
        stored.model_copy(update={"expires_at": "2099-01-01T00:00:00Z"})
    )

    # Act / Assert
    assert _chat(client, api_key).status_code == 200


def test_만료시각이깨져있으면거부한다(
    client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    """파싱 실패를 '무기한 유효' 로 해석하면 회수하려던 키가 영구히
    살아남는다."""
    # Arrange
    stored = registry.get_api_key_by_hash(apikey.hash_api_key(api_key))
    assert stored is not None
    registry.update_api_key(
        stored.model_copy(update={"expires_at": "not-a-date"})
    )

    # Act / Assert
    assert _chat(client, api_key).status_code == 401


def test_만료필드가없는기존키는영향받지않는다(
    client: testclient.TestClient,
    api_key: str,
) -> None:
    """하위 호환: 이미 발급된 키는 무기한이어야 한다."""
    # Arrange / Act / Assert
    assert _chat(client, api_key).status_code == 200


# ---------------------------------------------------------------------------
# 단가 미등록 정책
# ---------------------------------------------------------------------------


def _client_with_policy(
    app_services: services_module.Services, policy: str
) -> testclient.TestClient:
    """단가 정책만 바꾼 클라이언트를 만든다."""
    settings = app_services.settings.model_copy(
        update={"unpriced_model_policy": policy}
    )
    patched = dataclasses.replace(app_services, settings=settings)
    return testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    )


def test_allow정책은통과시키고비용0으로기록한다(
    app_services: services_module.Services,
    usage_store: repository.UsageStore,
    api_key: str,
) -> None:
    """기존 동작이다. 새 모델을 즉시 쓸 수 있지만 비용 귀속이 부정확해진다."""
    # Arrange
    client = _client_with_policy(app_services, "allow")

    # Act
    response = _chat(client, api_key, model=_UNPRICED_MODEL)

    # Assert
    assert response.status_code == 200, response.text
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert totals.cost_usd == decimal.Decimal("0")
    assert totals.unpriced_requests == 1


def test_reject정책은단가없는모델을거부한다(
    app_services: services_module.Services,
    api_key: str,
) -> None:
    """비용 귀속을 보장해야 하는 조직을 위한 선택지다."""
    # Arrange
    client = _client_with_policy(app_services, "reject")

    # Act
    response = _chat(client, api_key, model=_UNPRICED_MODEL)

    # Assert
    assert response.status_code == 400
    assert "단가" in response.json()["error"]["message"]


def test_reject정책도단가있는모델은통과시킨다(
    app_services: services_module.Services,
    api_key: str,
) -> None:
    # Arrange
    client = _client_with_policy(app_services, "reject")

    # Act / Assert
    assert _chat(client, api_key, model=_MODEL).status_code == 200


def test_hide정책은모델목록에서만감춘다(
    app_services: services_module.Services,
    api_key: str,
) -> None:
    """감추기만 하고 명시적 호출은 막지 않는다. 봉쇄가 필요하면 reject 다."""
    # Arrange
    client = _client_with_policy(app_services, "hide")

    # Act
    listed = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {api_key}"}
    )
    called = _chat(client, api_key, model=_UNPRICED_MODEL)

    # Assert
    ids = [item["id"] for item in listed.json()["data"]]
    assert _UNPRICED_MODEL not in ids
    assert _MODEL in ids
    assert called.status_code == 200, called.text


def test_기본정책은allow다(settings: config.Settings) -> None:
    """기본값이 기존 동작과 같아야 업그레이드가 안전하다."""
    # Arrange / Act / Assert
    assert settings.unpriced_model_policy == "allow"


def test_단가미등록시메트릭을올린다(
    client: testclient.TestClient,
    api_key: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """로그만 남기면 아무도 보지 않는다. 알람을 걸 수 있어야 한다.

    EMF 는 stdout 으로 나가므로 표준출력에서 메트릭 이름을 찾는다.
    """
    # Arrange / Act
    response = _chat(client, api_key, model=_UNPRICED_MODEL)
    captured = capsys.readouterr()

    # Assert
    assert response.status_code == 200, response.text
    assert (
        "UnpricedRequests" in captured.out
    ), "EMF 출력에 UnpricedRequests 메트릭이 없다"
