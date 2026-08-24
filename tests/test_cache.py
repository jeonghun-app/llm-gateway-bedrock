"""TTL 캐시 테스트.

시간 경과는 `FrozenClock.advance` 로 시뮬레이션한다. `sleep` 을 쓰면
테스트가 느려지고 CI 부하에 따라 불안정해진다.
"""

from __future__ import annotations

import conftest
from llmgw import cache


def test_get_or_load_최초호출은loader를실행한다() -> None:
    # Arrange
    calls: list[int] = []
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=conftest.FrozenClock()
    )

    def loader() -> str:
        calls.append(1)
        return "value"

    # Act
    result = subject.get_or_load("k", loader)

    # Assert
    assert result == "value"
    assert len(calls) == 1


def test_get_or_load_TTL내재호출은loader를실행하지않는다() -> None:
    # Arrange
    calls: list[int] = []
    frozen = conftest.FrozenClock()
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=frozen
    )

    def loader() -> str:
        calls.append(1)
        return "value"

    # Act
    subject.get_or_load("k", loader)
    frozen.advance(29)
    subject.get_or_load("k", loader)

    # Assert
    assert len(calls) == 1, f"loader 가 {len(calls)}회 실행됐다"


def test_get_or_load_TTL만료후재호출하면loader를다시실행한다() -> None:
    # Arrange
    calls: list[int] = []
    frozen = conftest.FrozenClock()
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=frozen
    )

    def loader() -> str:
        calls.append(1)
        return "value"

    # Act
    subject.get_or_load("k", loader)
    frozen.advance(31)
    subject.get_or_load("k", loader)

    # Assert
    assert len(calls) == 2, f"loader 가 {len(calls)}회 실행됐다"


def test_get_or_load_TTL0이면캐시하지않는다() -> None:
    # Arrange
    calls: list[int] = []
    subject: cache.TtlCache[str] = cache.TtlCache(ttl_seconds=0)

    def loader() -> str:
        calls.append(1)
        return "value"

    # Act
    subject.get_or_load("k", loader)
    subject.get_or_load("k", loader)

    # Assert
    assert len(calls) == 2


def test_get_or_load_키가다르면따로캐시한다() -> None:
    # Arrange
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=conftest.FrozenClock()
    )

    # Act
    first = subject.get_or_load("a", lambda: "A")
    second = subject.get_or_load("b", lambda: "B")

    # Assert
    assert (first, second) == ("A", "B")


def test_get_or_load_None값도캐시한다() -> None:
    """계정이 없는 경우를 캐시하지 못하면 매 요청이 DB를 때린다."""
    # Arrange
    calls: list[int] = []
    subject: cache.TtlCache[str | None] = cache.TtlCache(
        ttl_seconds=30, time_source=conftest.FrozenClock()
    )

    def loader() -> str | None:
        calls.append(1)
        return None

    # Act
    subject.get_or_load("missing", loader)
    subject.get_or_load("missing", loader)

    # Assert
    assert len(calls) == 1


def test_invalidate_해당키만제거한다() -> None:
    # Arrange
    frozen = conftest.FrozenClock()
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=frozen
    )
    subject.get_or_load("a", lambda: "A")
    subject.get_or_load("b", lambda: "B")

    # Act
    subject.invalidate("a")

    # Assert
    assert subject.get_or_load("a", lambda: "A2") == "A2"
    assert subject.get_or_load("b", lambda: "B2") == "B"


def test_invalidate_없는키도예외없이동작한다() -> None:
    # Arrange
    subject: cache.TtlCache[str] = cache.TtlCache(ttl_seconds=30)

    # Act / Assert
    subject.invalidate("nope")


def test_clear_전체를비운다() -> None:
    # Arrange
    subject: cache.TtlCache[str] = cache.TtlCache(
        ttl_seconds=30, time_source=conftest.FrozenClock()
    )
    subject.get_or_load("a", lambda: "A")

    # Act
    subject.clear()

    # Assert
    assert subject.get_or_load("a", lambda: "A2") == "A2"
