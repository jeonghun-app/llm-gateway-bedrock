"""시간 유틸리티 테스트."""

from __future__ import annotations

import datetime

import pytest

from llmgw import clock


def test_to_iso_마이크로초를버리고Z표기를쓴다() -> None:
    # Arrange
    moment = datetime.datetime(
        2026, 8, 23, 12, 34, 56, 789_000, tzinfo=datetime.UTC
    )

    # Act
    actual = clock.to_iso(moment)

    # Assert
    assert actual == "2026-08-23T12:34:56Z"


def test_to_iso_naive입력은UTC로간주한다() -> None:
    # Arrange
    moment = datetime.datetime(2026, 8, 23, 1, 2, 3)  # noqa: DTZ001

    # Act
    actual = clock.to_iso(moment)

    # Assert
    assert actual == "2026-08-23T01:02:03Z"


def test_to_iso_다른타임존은UTC로변환한다() -> None:
    # Arrange
    kst = datetime.timezone(datetime.timedelta(hours=9))
    moment = datetime.datetime(2026, 8, 23, 9, 0, 0, tzinfo=kst)

    # Act
    actual = clock.to_iso(moment)

    # Assert
    assert actual == "2026-08-23T00:00:00Z"


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (clock.day_key, "2026-08-23"),
        (clock.month_key, "2026-08"),
        (clock.hour_key, "2026-08-23T12"),
    ],
)
def test_기간키형식(builder: object, expected: str) -> None:
    # Arrange
    moment = datetime.datetime(2026, 8, 23, 12, 34, 56, tzinfo=datetime.UTC)

    # Act
    actual = builder(moment)  # type: ignore[operator]

    # Assert
    assert actual == expected


def test_parse_date_정상형식() -> None:
    # Arrange / Act
    actual = clock.parse_date("2026-08-23")

    # Assert
    assert actual == datetime.date(2026, 8, 23)


@pytest.mark.parametrize("bad", ["2026/08/23", "20260823", "abc", ""])
def test_parse_date_잘못된형식_ValueError(bad: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):  # noqa: PT011
        clock.parse_date(bad)


def test_date_range_시작과종료를모두포함한다() -> None:
    # Arrange
    start = datetime.date(2026, 8, 21)
    end = datetime.date(2026, 8, 23)

    # Act
    actual = clock.date_range(start, end)

    # Assert
    assert actual == [
        datetime.date(2026, 8, 21),
        datetime.date(2026, 8, 22),
        datetime.date(2026, 8, 23),
    ]


def test_date_range_같은날짜_1건반환() -> None:
    # Arrange
    day = datetime.date(2026, 8, 23)

    # Act / Assert
    assert clock.date_range(day, day) == [day]


def test_date_range_역순범위_빈리스트() -> None:
    # Arrange / Act
    actual = clock.date_range(
        datetime.date(2026, 8, 23), datetime.date(2026, 8, 21)
    )

    # Assert
    assert actual == []


def test_month_range_연말경계를넘어간다() -> None:
    # Arrange
    start = datetime.date(2026, 11, 15)
    end = datetime.date(2027, 2, 3)

    # Act
    actual = clock.month_range(start, end)

    # Assert
    assert actual == ["2026-11", "2026-12", "2027-01", "2027-02"]


def test_month_range_같은달_1건반환() -> None:
    # Arrange / Act
    actual = clock.month_range(
        datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)
    )

    # Assert
    assert actual == ["2026-08"]


def test_month_range_역순범위_빈리스트() -> None:
    # Arrange / Act
    actual = clock.month_range(
        datetime.date(2026, 8, 31), datetime.date(2026, 8, 1)
    )

    # Assert
    assert actual == []


def test_system_clock_UTC시각을반환한다() -> None:
    # Arrange / Act
    now = clock.SYSTEM_CLOCK.now()

    # Assert
    assert now.tzinfo is not None, "naive datetime 을 반환하면 안 된다"
    assert now.utcoffset() == datetime.timedelta(0)


def test_uuid_id_factory_매번다른값을반환한다() -> None:
    # Arrange / Act
    first = clock.UUID_ID_FACTORY.new_id()
    second = clock.UUID_ID_FACTORY.new_id()

    # Assert
    assert first != second
    assert len(first) == 32
