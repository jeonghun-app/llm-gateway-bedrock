"""집계 조회 서비스 테스트."""

from __future__ import annotations

import datetime
import decimal

import pytest

import conftest
from llmgw import analytics
from llmgw import domain
from llmgw import errors
from llmgw import repository

_TODAY = datetime.date(2026, 8, 23)


def _window(start: str, end: str) -> analytics.DateWindow:
    """문자열로 기간을 만든다."""
    return analytics.DateWindow(
        start=datetime.date.fromisoformat(start),
        end=datetime.date.fromisoformat(end),
    )


def _seed_usage(usage_store: repository.UsageStore) -> None:
    """3일에 걸친 여러 팀·사용자·모델 사용량을 심는다."""
    rows = [
        # (request_id, day, team, user, model, in, out, cost, latency, status)
        (
            "r1",
            "2026-08-21",
            "platform",
            "alice",
            "amazon.nova-lite-v1:0",
            1000,
            500,
            "1.0",
            100,
            200,
        ),
        (
            "r2",
            "2026-08-22",
            "platform",
            "alice",
            "amazon.nova-lite-v1:0",
            2000,
            1000,
            "2.0",
            200,
            200,
        ),
        (
            "r3",
            "2026-08-22",
            "platform",
            "bob",
            "amazon.nova-pro-v1:0",
            500,
            250,
            "4.0",
            300,
            200,
        ),
        (
            "r4",
            "2026-08-23",
            "research",
            "carol",
            "amazon.nova-pro-v1:0",
            100,
            50,
            "8.0",
            400,
            200,
        ),
        (
            "r5",
            "2026-08-23",
            "research",
            "carol",
            "amazon.nova-lite-v1:0",
            0,
            0,
            "0",
            50,
            429,
        ),
    ]
    for (
        request_id,
        day,
        team,
        user,
        model,
        input_tokens,
        output_tokens,
        cost,
        latency,
        status,
    ) in rows:
        usage_store.record(
            conftest.make_usage_record(
                request_id=request_id,
                timestamp=f"{day}T10:00:00Z",
                team_id=team,
                user_id=user,
                key_id=f"key-{user}",
                model_id=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=latency,
                status_code=status,
            )
        )


# -- parse_window -----------------------------------------------------------


def test_parse_window_둘다지정하면그대로쓴다() -> None:
    # Arrange / Act
    window = analytics.parse_window("2026-08-01", "2026-08-10", today=_TODAY)

    # Assert
    assert window.start == datetime.date(2026, 8, 1)
    assert window.end == datetime.date(2026, 8, 10)


def test_parse_window_기본값은오늘기준30일() -> None:
    # Arrange / Act
    window = analytics.parse_window(None, None, today=_TODAY)

    # Assert
    assert window.end == _TODAY
    assert window.start == datetime.date(2026, 7, 25)
    assert len(window.days) == 30


def test_parse_window_시작일만생략하면종료일기준으로역산한다() -> None:
    # Arrange / Act
    window = analytics.parse_window(
        None, "2026-08-10", today=_TODAY, default_days=5
    )

    # Assert
    assert window.start == datetime.date(2026, 8, 6)
    assert window.end == datetime.date(2026, 8, 10)


def test_parse_window_같은날짜_1일범위() -> None:
    # Arrange / Act
    window = analytics.parse_window("2026-08-23", "2026-08-23", today=_TODAY)

    # Assert
    assert len(window.days) == 1


def test_parse_window_시작이종료보다늦으면InvalidRequestError() -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="시작일"):
        analytics.parse_window("2026-08-10", "2026-08-01", today=_TODAY)


@pytest.mark.parametrize("bad", ["2026/08/01", "08-01-2026", "not-a-date"])
def test_parse_window_잘못된형식_InvalidRequestError(bad: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="YYYY-MM-DD"):
        analytics.parse_window(bad, None, today=_TODAY)


def test_parse_window_최대범위경계_정확히93일은허용() -> None:
    # Arrange
    end = datetime.date(2026, 8, 23)
    start = end - datetime.timedelta(days=analytics.MAX_RANGE_DAYS - 1)

    # Act
    window = analytics.parse_window(
        start.isoformat(), end.isoformat(), today=_TODAY
    )

    # Assert
    assert len(window.days) == analytics.MAX_RANGE_DAYS


def test_parse_window_최대범위초과_InvalidRequestError() -> None:
    # Arrange
    end = datetime.date(2026, 8, 23)
    start = end - datetime.timedelta(days=analytics.MAX_RANGE_DAYS)

    # Act / Assert
    with pytest.raises(errors.InvalidRequestError, match="너무 넓다"):
        analytics.parse_window(start.isoformat(), end.isoformat(), today=_TODAY)


# -- summary ----------------------------------------------------------------


def test_summary_기간전체를합산한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    totals = analytics_service.summary(
        "acme", _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert totals.requests == 5
    assert totals.success_requests == 4
    assert totals.error_requests == 1
    assert totals.cost_usd == decimal.Decimal("15")
    assert totals.input_tokens == 3600
    assert totals.output_tokens == 1800
    assert totals.total_tokens == 5400


def test_summary_기간밖데이터는제외한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    totals = analytics_service.summary(
        "acme", _window("2026-08-22", "2026-08-22")
    )

    # Assert
    assert totals.requests == 2, f"기대 2, 실제 {totals.requests}"
    assert totals.cost_usd == decimal.Decimal("6")


def test_summary_데이터없으면0을반환한다(
    analytics_service: analytics.AnalyticsService,
) -> None:
    # Arrange / Act
    totals = analytics_service.summary(
        "empty", _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert totals.requests == 0
    assert totals.cost_usd == decimal.Decimal("0")
    assert totals.avg_latency_ms == 0
    assert totals.error_rate == 0.0


def test_summary_평균지연을계산한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    totals = analytics_service.summary(
        "acme", _window("2026-08-21", "2026-08-23")
    )

    # Assert
    # (100+200+300+400+50) / 5 = 210
    assert (
        totals.avg_latency_ms == 210
    ), f"기대 210, 실제 {totals.avg_latency_ms}"


def test_summary_에러율을계산한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    totals = analytics_service.summary(
        "acme", _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert totals.error_rate == pytest.approx(0.2)


# -- timeseries -------------------------------------------------------------


def test_timeseries_날짜오름차순으로반환한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    series = analytics_service.timeseries(
        "acme", _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert [entry["date"] for entry in series] == [
        "2026-08-21",
        "2026-08-22",
        "2026-08-23",
    ]
    assert [entry["requests"] for entry in series] == [1, 2, 2]


def test_timeseries_데이터없는날도0으로채운다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    """차트에 구멍이 생기면 축이 어긋난다."""
    # Arrange
    _seed_usage(usage_store)

    # Act
    series = analytics_service.timeseries(
        "acme", _window("2026-08-19", "2026-08-23")
    )

    # Assert
    assert len(series) == 5
    assert series[0] == {
        "date": "2026-08-19",
        "requests": 0,
        "success_requests": 0,
        "error_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "avg_latency_ms": 0,
        "error_rate": 0.0,
    }


# -- breakdown --------------------------------------------------------------


def test_breakdown_팀축을비용내림차순으로반환한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    _seed_usage(usage_store)
    registry.put_team(
        domain.Team(account_id="acme", team_id="platform", name="플랫폼팀")
    )
    registry.put_team(
        domain.Team(account_id="acme", team_id="research", name="리서치팀")
    )

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.TEAM,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    assert [row.key for row in rows] == ["research", "platform"]
    assert [row.label for row in rows] == ["리서치팀", "플랫폼팀"]
    assert rows[0].totals.cost_usd == decimal.Decimal("8")
    assert rows[1].totals.cost_usd == decimal.Decimal("7")


def test_breakdown_사용자축에소속팀을붙인다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    _seed_usage(usage_store)
    registry.put_user(
        domain.User(
            account_id="acme",
            user_id="alice",
            name="앨리스",
            team_id="platform",
        )
    )

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.USER,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    by_key = {row.key: row for row in rows}
    assert by_key["alice"].label == "앨리스"
    assert by_key["alice"].team_id == "platform"
    assert by_key["alice"].totals.requests == 2


def test_breakdown_레지스트리에없으면식별자를라벨로쓴다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.USER,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    assert all(row.label == row.key for row in rows)


def test_breakdown_모델축을집계한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.MODEL,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    by_key = {row.key: row.totals for row in rows}
    assert by_key["amazon.nova-pro-v1:0"].requests == 2
    assert by_key["amazon.nova-lite-v1:0"].requests == 3


def test_breakdown_키축을집계한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    _seed_usage(usage_store)

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.KEY,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    assert {row.key for row in rows} == {
        "key-alice",
        "key-bob",
        "key-carol",
    }


def test_breakdown_데이터없으면빈리스트(
    analytics_service: analytics.AnalyticsService,
) -> None:
    # Arrange / Act
    rows = analytics_service.breakdown(
        "empty",
        domain.BreakdownDimension.TEAM,
        _window("2026-08-21", "2026-08-23"),
    )

    # Assert
    assert rows == []


def test_breakdown_비용동일시요청수와키로결정적정렬(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    for team in ("zulu", "alpha"):
        usage_store.record(
            conftest.make_usage_record(
                request_id=f"r-{team}",
                team_id=team,
                cost_usd="1.0",
                timestamp="2026-08-23T10:00:00Z",
            )
        )

    # Act
    rows = analytics_service.breakdown(
        "acme",
        domain.BreakdownDimension.TEAM,
        _window("2026-08-23", "2026-08-23"),
    )

    # Assert
    assert [row.key for row in rows] == ["alpha", "zulu"]


def test_breakdown_row_to_api_dict_팀없으면필드를생략한다() -> None:
    # Arrange
    row = analytics.BreakdownRow(
        key="amazon.nova-lite-v1:0",
        label="amazon.nova-lite-v1:0",
        team_id="",
        totals=domain.UsageTotals(requests=1),
    )

    # Act
    payload = row.to_api_dict()

    # Assert
    assert "team_id" not in payload
    assert payload["key"] == "amazon.nova-lite-v1:0"
    assert payload["requests"] == 1


# -- accounts_overview ------------------------------------------------------


def test_accounts_overview_계정이없으면빈리스트(
    analytics_service: analytics.AnalyticsService,
) -> None:
    # Arrange / Act
    rows = analytics_service.accounts_overview(
        _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert rows == []


def test_accounts_overview_계정별합계를비용순으로반환한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(
        domain.Account(
            account_id="acme",
            name="Acme",
            monthly_budget_usd=decimal.Decimal("100"),
        )
    )
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    _seed_usage(usage_store)
    usage_store.record(
        conftest.make_usage_record(
            request_id="b1",
            account_id="beta",
            cost_usd="99.0",
            timestamp="2026-08-23T10:00:00Z",
        )
    )

    # Act
    rows = analytics_service.accounts_overview(
        _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert [row["account_id"] for row in rows] == ["beta", "acme"]
    assert rows[0]["cost_usd"] == pytest.approx(99.0)
    assert rows[1]["cost_usd"] == pytest.approx(15.0)
    assert rows[1]["monthly_budget_usd"] == pytest.approx(100.0)
    assert rows[0]["monthly_budget_usd"] is None


def test_accounts_overview_사용량없는계정도0으로포함한다(
    analytics_service: analytics.AnalyticsService,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(domain.Account(account_id="idle", name="Idle"))

    # Act
    rows = analytics_service.accounts_overview(
        _window("2026-08-21", "2026-08-23")
    )

    # Assert
    assert len(rows) == 1
    assert rows[0]["requests"] == 0


# -- recent_requests --------------------------------------------------------


def test_recent_requests_최신순으로반환한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="early", timestamp="2026-08-23T08:00:00Z"
        )
    )
    usage_store.record(
        conftest.make_usage_record(
            request_id="late", timestamp="2026-08-23T20:00:00Z"
        )
    )

    # Act
    rows = analytics_service.recent_requests("acme", _TODAY)

    # Assert
    assert [row["request_id"] for row in rows] == ["late", "early"]
    assert rows[0]["model_id"] == "amazon.nova-lite-v1:0"
    assert rows[0]["status_code"] == 200


def test_recent_requests_데이터없으면빈리스트(
    analytics_service: analytics.AnalyticsService,
) -> None:
    # Arrange / Act
    rows = analytics_service.recent_requests("acme", _TODAY)

    # Assert
    assert rows == []


def test_recent_requests_limit을적용한다(
    analytics_service: analytics.AnalyticsService,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    for index in range(5):
        usage_store.record(
            conftest.make_usage_record(
                request_id=f"r{index}",
                timestamp=f"2026-08-23T1{index}:00:00Z",
            )
        )

    # Act
    rows = analytics_service.recent_requests("acme", _TODAY, limit=2)

    # Assert
    assert len(rows) == 2


def test_date_window_to_api_dict() -> None:
    # Arrange
    window = _window("2026-08-01", "2026-08-31")

    # Act / Assert
    assert window.to_api_dict() == {
        "start": "2026-08-01",
        "end": "2026-08-31",
    }
