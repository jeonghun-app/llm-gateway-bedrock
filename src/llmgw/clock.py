"""시간과 식별자 생성을 주입 가능하게 만드는 어댑터.

테스트에서 `sleep` 이나 실제 시계에 의존하지 않기 위해, 시간과 UUID 를
직접 호출하지 않고 이 모듈의 프로토콜을 통해 얻는다.
"""

from __future__ import annotations

import datetime
import typing
import uuid


class Clock(typing.Protocol):
    """현재 시각 제공자."""

    def now(self) -> datetime.datetime:
        """UTC 기준 현재 시각을 반환한다.

        Returns:
            timezone 정보가 붙은 `datetime`.
        """
        ...


class IdFactory(typing.Protocol):
    """식별자 생성기."""

    def new_id(self) -> str:
        """새 식별자를 반환한다."""
        ...


class SystemClock:
    """운영체제 시계를 쓰는 기본 구현."""

    def now(self) -> datetime.datetime:
        """UTC 기준 현재 시각을 반환한다."""
        return datetime.datetime.now(datetime.UTC)


class UuidIdFactory:
    """UUID4 기반 식별자 생성기."""

    def new_id(self) -> str:
        """하이픈 없는 32자 16진수 문자열을 반환한다."""
        return uuid.uuid4().hex


SYSTEM_CLOCK = SystemClock()
UUID_ID_FACTORY = UuidIdFactory()


def to_iso(moment: datetime.datetime) -> str:
    """`datetime` 을 초 단위 ISO-8601 UTC 문자열로 변환한다.

    DynamoDB 정렬 키로 쓰기 위해 항상 같은 길이·같은 오프셋 표기를
    보장해야 한다. 그래서 마이크로초를 버리고 `+00:00` 을 `Z` 로 바꾼다.

    Args:
        moment: 변환할 시각. naive 이면 UTC 로 간주한다.

    Returns:
        `2026-08-23T12:34:56Z` 형태의 문자열.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return (
        moment.astimezone(datetime.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def day_key(moment: datetime.datetime) -> str:
    """일 단위 집계 파티션 키 조각(`YYYY-MM-DD`)을 만든다."""
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%d")


def month_key(moment: datetime.datetime) -> str:
    """월 단위 집계 파티션 키 조각(`YYYY-MM`)을 만든다."""
    return moment.astimezone(datetime.UTC).strftime("%Y-%m")


def hour_key(moment: datetime.datetime) -> str:
    """시 단위 집계 파티션 키 조각(`YYYY-MM-DDTHH`)을 만든다."""
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H")


def parse_date(value: str) -> datetime.date:
    """`YYYY-MM-DD` 문자열을 `date` 로 파싱한다.

    Args:
        value: 날짜 문자열.

    Returns:
        파싱된 `date`.

    Raises:
        ValueError: 형식이 맞지 않는 경우.
    """
    return datetime.datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """시작일과 종료일을 모두 포함하는 날짜 리스트를 만든다.

    Args:
        start: 시작일(포함).
        end: 종료일(포함).

    Returns:
        오름차순 날짜 리스트. `start` 가 `end` 보다 늦으면 빈 리스트.
    """
    if start > end:
        return []
    span = (end - start).days
    return [
        start + datetime.timedelta(days=offset) for offset in range(span + 1)
    ]


def month_range(start: datetime.date, end: datetime.date) -> list[str]:
    """기간에 걸친 `YYYY-MM` 키 목록을 만든다.

    Args:
        start: 시작일(포함).
        end: 종료일(포함).

    Returns:
        오름차순 월 키 리스트. 중복은 제거된다.
    """
    if start > end:
        return []
    keys: list[str] = []
    cursor = start.replace(day=1)
    last = end.replace(day=1)
    while cursor <= last:
        keys.append(cursor.strftime("%Y-%m"))
        # 다음 달 1일로 이동한다. 12월에서 넘어갈 때 연도를 올린다.
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return keys
