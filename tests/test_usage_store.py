"""사용량 저장소 테스트.

가장 중요한 검증은 멱등성이다. 게이트웨이는 클라이언트가 같은
`X-Request-Id` 로 재시도하거나 내부 재시도가 발생해도 집계를 두 번 더하지
않아야 한다.
"""

from __future__ import annotations

import datetime
import decimal
import typing

import pytest

import conftest
from llmgw import domain
from llmgw import repository


def test_record_신규요청_원본과집계가함께기록된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(
        request_id="req-new",
        input_tokens=1000,
        output_tokens=500,
        cost_usd="0.002",
        latency_ms=150,
    )

    # Act
    newly_recorded = usage_store.record(record)

    # Assert
    assert newly_recorded is True
    day_totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    total = day_totals["TOTAL"]
    assert total.requests == 1, f"기대 1, 실제 {total.requests}"
    assert total.input_tokens == 1000
    assert total.output_tokens == 500
    assert total.cost_usd == decimal.Decimal("0.002")
    assert total.latency_ms_sum == 150


def test_record_동일request_id_2회_집계가중복되지않는다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(request_id="req-dup")

    # Act
    first = usage_store.record(record)
    second = usage_store.record(record)

    # Assert
    assert first is True, "첫 기록은 신규여야 한다"
    assert second is False, "두 번째 기록은 중복으로 판정돼야 한다"
    total = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert (
        total.requests == 1
    ), f"중복 호출에도 요청 수는 1이어야 한다. 실제 {total.requests}"
    assert (
        total.input_tokens == 1000
    ), f"토큰도 중복 합산되지 않아야 한다. 실제 {total.input_tokens}"


def test_record_동일request_id_3회_원본레코드는1건이다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(request_id="req-triple")

    # Act
    for _ in range(3):
        usage_store.record(record)

    # Assert
    records = usage_store.list_records("acme", "2026-08-23")
    assert len(records) == 1, f"기대 1건, 실제 {len(records)}건"


def test_record_모든축이집계된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        model_id="amazon.nova-lite-v1:0",
    )

    # Act
    usage_store.record(record)

    # Assert
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    expected_keys = {
        "TOTAL",
        "TEAM#platform",
        "USER#alice",
        "KEY#key-1",
        "MODEL#amazon.nova-lite-v1:0",
    }
    assert (
        set(totals) == expected_keys
    ), f"기대 축 {sorted(expected_keys)}, 실제 {sorted(totals)}"


def test_record_월집계도함께갱신된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    first = conftest.make_usage_record(
        request_id="req-a", timestamp="2026-08-01T00:00:00Z"
    )
    second = conftest.make_usage_record(
        request_id="req-b", timestamp="2026-08-31T23:59:59Z"
    )

    # Act
    usage_store.record(first)
    usage_store.record(second)

    # Assert
    month_totals = usage_store.query_totals(
        "acme", domain.Granularity.MONTH, "2026-08"
    )
    assert month_totals["TOTAL"].requests == 2
    day_totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-01"
    )
    assert day_totals["TOTAL"].requests == 1, "일 집계는 날짜별로 분리돼야 한다"


def test_record_실패요청은error_requests로집계된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(
        request_id="req-fail",
        status_code=429,
        error_code="budget_exceeded",
        input_tokens=0,
        output_tokens=0,
        cost_usd="0",
    )

    # Act
    usage_store.record(record)

    # Assert
    total = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert total.requests == 1
    assert total.error_requests == 1
    assert total.success_requests == 0
    assert total.error_rate == 1.0


def test_record_팀미지정시_팀축은생성되지않는다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(request_id="req-noteam", team_id="")

    # Act
    usage_store.record(record)

    # Assert
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert not any(
        key.startswith("TEAM#") for key in totals
    ), f"팀 축이 생성되지 않아야 한다. 실제 {sorted(totals)}"


def test_record_토큰0건_경계값이정상기록된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(
        request_id="req-zero",
        input_tokens=0,
        output_tokens=0,
        cost_usd="0",
        latency_ms=0,
    )

    # Act
    newly_recorded = usage_store.record(record)

    # Assert
    assert newly_recorded is True
    total = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert total.requests == 1
    assert total.total_tokens == 0
    assert total.avg_latency_ms == 0


def test_get_totals_요청한축만반환한다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(conftest.make_usage_record(request_id="req-1"))

    # Act
    totals = usage_store.get_totals(
        "acme",
        domain.Granularity.MONTH,
        "2026-08",
        ["TOTAL", "USER#alice", "USER#does-not-exist"],
    )

    # Assert
    assert set(totals) == {
        "TOTAL",
        "USER#alice",
    }, f"존재하는 축만 와야 한다. 실제 {sorted(totals)}"
    assert totals["USER#alice"].requests == 1


def test_get_totals_빈목록이면조회하지않는다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange / Act
    totals = usage_store.get_totals(
        "acme", domain.Granularity.MONTH, "2026-08", []
    )

    # Assert
    assert totals == {}


def test_query_partitions_여러파티션을한번에읽는다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="req-1", timestamp="2026-08-21T01:00:00Z"
        )
    )
    usage_store.record(
        conftest.make_usage_record(
            request_id="req-2", timestamp="2026-08-23T01:00:00Z"
        )
    )
    partitions = [
        repository.agg_pk("acme", domain.Granularity.DAY, day)
        for day in ("2026-08-21", "2026-08-22", "2026-08-23")
    ]

    # Act
    result = usage_store.query_partitions(partitions)

    # Assert
    assert set(result) == set(partitions), "모든 파티션 키가 채워져야 한다"
    assert result[partitions[0]]["TOTAL"].requests == 1
    assert result[partitions[1]] == {}, "데이터 없는 날짜는 빈 결과여야 한다"
    assert result[partitions[2]]["TOTAL"].requests == 1


def test_list_records_시간역순으로반환한다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="older", timestamp="2026-08-23T09:00:00Z"
        )
    )
    usage_store.record(
        conftest.make_usage_record(
            request_id="newer", timestamp="2026-08-23T18:00:00Z"
        )
    )

    # Act
    records = usage_store.list_records("acme", "2026-08-23")

    # Assert
    assert [item["request_id"] for item in records] == ["newer", "older"]


def test_record_ttl속성이설정된다(
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = conftest.make_usage_record(request_id="req-ttl")

    # Act
    usage_store.record(record)

    # Assert
    stored = usage_store.list_records("acme", "2026-08-23")[0]
    # 레코드 시각 + TTL(30일). 매직 넘버를 쓰지 않고 계산해 비교한다.
    recorded_at = datetime.datetime(2026, 8, 23, 12, 0, 0, tzinfo=datetime.UTC)
    expected = int(recorded_at.timestamp()) + 30 * 86400
    assert (
        int(stored["expires_at"]) == expected
    ), f"기대 {expected}, 실제 {stored['expires_at']}"


@pytest.mark.parametrize(
    ("granularity", "period", "expected"),
    [
        (domain.Granularity.DAY, "2026-08-23", "acme#DAY#2026-08-23"),
        (domain.Granularity.MONTH, "2026-08", "acme#MONTH#2026-08"),
    ],
)
def test_agg_pk_파티션키형식(
    granularity: domain.Granularity, period: str, expected: str
) -> None:
    # Arrange / Act
    actual = repository.agg_pk("acme", granularity, period)

    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    ("dimension", "value", "expected"),
    [
        (None, "", "TOTAL"),
        (domain.BreakdownDimension.TEAM, "platform", "TEAM#platform"),
        (domain.BreakdownDimension.USER, "alice", "USER#alice"),
        (domain.BreakdownDimension.MODEL, "amazon.nova", "MODEL#amazon.nova"),
        (domain.BreakdownDimension.KEY, "key-1", "KEY#key-1"),
    ],
)
def test_dimension_sk_정렬키형식(
    dimension: typing.Any, value: str, expected: str
) -> None:
    # Arrange / Act
    actual = repository.dimension_sk(dimension, value)

    # Assert
    assert actual == expected
