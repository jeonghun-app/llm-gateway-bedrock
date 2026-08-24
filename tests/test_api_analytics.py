"""집계 조회 API 테스트."""

from __future__ import annotations

import typing

from fastapi import testclient
import pytest

import conftest
from llmgw import domain
from llmgw import repository


def _seed(usage_store: repository.UsageStore) -> None:
    """3일에 걸친 사용량을 심는다."""
    rows = [
        (
            "a1",
            "2026-08-21",
            "platform",
            "alice",
            "amazon.nova-lite-v1:0",
            "1.0",
            200,
        ),
        (
            "a2",
            "2026-08-22",
            "platform",
            "bob",
            "amazon.nova-pro-v1:0",
            "2.0",
            200,
        ),
        (
            "a3",
            "2026-08-23",
            "research",
            "carol",
            "amazon.nova-pro-v1:0",
            "4.0",
            200,
        ),
        (
            "a4",
            "2026-08-23",
            "research",
            "carol",
            "amazon.nova-lite-v1:0",
            "0",
            500,
        ),
    ]
    for request_id, day, team, user, model, cost, status in rows:
        usage_store.record(
            conftest.make_usage_record(
                request_id=request_id,
                timestamp=f"{day}T10:00:00Z",
                team_id=team,
                user_id=user,
                key_id=f"key-{user}",
                model_id=model,
                cost_usd=cost,
                status_code=status,
            )
        )


def test_analytics_토큰없으면401(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/analytics/summary?account_id=acme")

    # Assert
    assert response.status_code == 401


def test_summary_기간합계를반환한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/summary",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "start": "2026-08-21",
            "end": "2026-08-23",
        },
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window"] == {"start": "2026-08-21", "end": "2026-08-23"}
    assert body["totals"]["requests"] == 4
    assert body["totals"]["cost_usd"] == pytest.approx(7.0)
    assert body["totals"]["error_requests"] == 1
    assert body["totals"]["error_rate"] == pytest.approx(0.25)


def test_summary_기간생략시오늘기준30일(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/summary",
        headers=admin_headers,
        params={"account_id": "acme"},
    )

    # Assert
    body = response.json()
    assert body["window"] == {"start": "2026-07-25", "end": "2026-08-23"}
    assert body["totals"]["requests"] == 4


def test_summary_범위초과_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get(
        "/analytics/summary",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "start": "2026-01-01",
            "end": "2026-08-23",
        },
    )

    # Assert
    assert response.status_code == 400
    assert "너무 넓다" in response.json()["error"]["message"]


def test_summary_날짜형식오류_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get(
        "/analytics/summary",
        headers=admin_headers,
        params={"account_id": "acme", "start": "2026/08/01"},
    )

    # Assert
    assert response.status_code == 400


def test_summary_account_id누락_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get("/analytics/summary", headers=admin_headers)

    # Assert
    assert response.status_code == 400


def test_timeseries_빈날짜도0으로채운다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/timeseries",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "start": "2026-08-19",
            "end": "2026-08-23",
        },
    )

    # Assert
    series = response.json()["data"]
    assert [entry["date"] for entry in series] == [
        "2026-08-19",
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
        "2026-08-23",
    ]
    assert [entry["requests"] for entry in series] == [0, 0, 1, 1, 2]


@pytest.mark.parametrize(
    ("dimension", "expected_keys"),
    [
        ("team", {"platform", "research"}),
        ("user", {"alice", "bob", "carol"}),
        ("model", {"amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"}),
        ("key", {"key-alice", "key-bob", "key-carol"}),
    ],
)
def test_breakdown_축별로집계한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
    dimension: str,
    expected_keys: set[str],
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/breakdown",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "dimension": dimension,
            "start": "2026-08-21",
            "end": "2026-08-23",
        },
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dimension"] == dimension
    assert {row["key"] for row in body["data"]} == expected_keys


def test_breakdown_비용내림차순으로정렬된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    rows = client.get(
        "/analytics/breakdown",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "dimension": "team",
            "start": "2026-08-21",
            "end": "2026-08-23",
        },
    ).json()["data"]

    # Assert
    assert [row["key"] for row in rows] == ["research", "platform"]
    assert rows[0]["cost_usd"] == pytest.approx(4.0)
    assert rows[1]["cost_usd"] == pytest.approx(3.0)


def test_breakdown_지원하지않는축_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get(
        "/analytics/breakdown",
        headers=admin_headers,
        params={"account_id": "acme", "dimension": "region"},
    )

    # Assert
    assert response.status_code == 400
    assert "지원하지 않는 축" in response.json()["error"]["message"]


def test_breakdown_사용자축에팀라벨이붙는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    _seed(usage_store)
    registry.put_user(
        domain.User(
            account_id="acme",
            user_id="alice",
            name="앨리스",
            team_id="platform",
        )
    )

    # Act
    rows = client.get(
        "/analytics/breakdown",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "dimension": "user",
            "start": "2026-08-21",
            "end": "2026-08-23",
        },
    ).json()["data"]

    # Assert
    alice = next(row for row in rows if row["key"] == "alice")
    assert alice["label"] == "앨리스"
    assert alice["team_id"] == "platform"


def test_accounts_계정별합계를반환한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    registry.put_account(
        domain.Account(account_id="beta", name="Beta"), overwrite=True
    )
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/accounts",
        headers=admin_headers,
        params={"start": "2026-08-21", "end": "2026-08-23"},
    )

    # Assert
    rows = {item["account_id"]: item for item in response.json()["data"]}
    assert rows["acme"]["requests"] == 4
    assert rows["beta"]["requests"] == 0


def test_requests_최근요청을최신순으로반환한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="old", timestamp="2026-08-23T08:00:00Z"
        )
    )
    usage_store.record(
        conftest.make_usage_record(
            request_id="new", timestamp="2026-08-23T20:00:00Z"
        )
    )

    # Act
    response = client.get(
        "/analytics/requests",
        headers=admin_headers,
        params={"account_id": "acme", "date": "2026-08-23"},
    )

    # Assert
    assert [row["request_id"] for row in response.json()["data"]] == [
        "new",
        "old",
    ]


def test_requests_날짜생략시오늘을쓴다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="today", timestamp="2026-08-23T09:00:00Z"
        )
    )

    # Act
    response = client.get(
        "/analytics/requests",
        headers=admin_headers,
        params={"account_id": "acme"},
    )

    # Assert
    assert response.json()["date"] == "2026-08-23"
    assert len(response.json()["data"]) == 1


def test_requests_날짜형식오류_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get(
        "/analytics/requests",
        headers=admin_headers,
        params={"account_id": "acme", "date": "23-08-2026"},
    )

    # Assert
    assert response.status_code == 400


def test_dashboard_한번의호출로모든블록을반환한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed(usage_store)

    # Act
    response = client.get(
        "/analytics/dashboard",
        headers=admin_headers,
        params={
            "account_id": "acme",
            "start": "2026-08-21",
            "end": "2026-08-23",
        },
    )

    # Assert
    assert response.status_code == 200, response.text
    body: dict[str, typing.Any] = response.json()
    assert set(body) == {
        "account_id",
        "window",
        "totals",
        "timeseries",
        "breakdowns",
        "recent_requests",
    }
    assert set(body["breakdowns"]) == {"team", "user", "model", "key"}
    assert body["totals"]["requests"] == 4
    assert len(body["timeseries"]) == 3
    assert (
        len(body["recent_requests"]) == 2
    ), "종료일(8/23)의 요청 2건이 나와야 한다"


def test_dashboard_데이터가없어도구조를유지한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    """UI 가 빈 계정에서 깨지지 않아야 한다."""
    # Arrange / Act
    response = client.get(
        "/analytics/dashboard",
        headers=admin_headers,
        params={"account_id": "empty"},
    )

    # Assert
    body = response.json()
    assert body["totals"]["requests"] == 0
    assert body["breakdowns"]["team"] == []
    assert body["recent_requests"] == []
    assert len(body["timeseries"]) == 30
