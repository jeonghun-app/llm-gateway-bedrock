"""짧은 TTL 을 갖는 인메모리 캐시.

인증 경로에서 계정·팀·사용자 메타데이터를 매 요청마다 읽으면 요청당
GetItem 이 4번 발생한다. 이 정보는 거의 바뀌지 않으므로 짧게 캐시한다.

TTL 을 짧게(기본 30초) 유지하는 이유는 계정이나 팀을 비활성화했을 때
반영 지연을 사람이 체감하지 않을 수준으로 묶기 위해서다. API 키 자체는
즉시 차단되어야 하므로 캐시하지 않는다.

캐시는 태스크 로컬이다. ECS 태스크가 여러 개면 각자 자기 캐시를 갖고,
최대 TTL 만큼 서로 다른 값을 볼 수 있다. 예산·상태 판단에 쓰는 값이라
이 정도 수렴 지연은 허용 범위로 판단했다.
"""

from __future__ import annotations

import threading
import typing

from llmgw import clock

_DEFAULT_TTL_SECONDS = 30.0

# 캐시가 무한히 커지는 것을 막는 상한. 초과하면 전체를 비운다. LRU 를
# 쓰지 않는 이유는 항목 수가 테넌트 규모(수백)로 제한되고, 단순한 편이
# 동작을 예측하기 쉽기 때문이다.
_MAX_ENTRIES = 5000


class TtlCache[T]:
    """만료 시간이 있는 키-값 캐시. 스레드 안전하다."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: clock.Clock | None = None,
    ) -> None:
        """캐시를 만든다.

        Args:
            ttl_seconds: 항목 유효 기간(초). 0 이하면 캐시를 사용하지 않는다.
            time_source: 시각 제공자. 테스트에서 시간을 고정하기 위해
                주입할 수 있다.
        """
        self._ttl_seconds = ttl_seconds
        self._clock = time_source or clock.SYSTEM_CLOCK
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, T]] = {}

    def get_or_load(self, key: str, loader: typing.Callable[[], T]) -> T:
        """캐시에서 값을 얻고, 없으면 `loader` 로 채운다.

        `loader` 는 락 밖에서 실행한다. 락을 잡은 채로 DynamoDB 를 호출하면
        같은 태스크의 다른 요청이 전부 대기하게 된다. 그 대가로 캐시 미스가
        동시에 발생하면 `loader` 가 중복 실행될 수 있는데, 읽기 전용
        연산이라 문제되지 않는다.

        Args:
            key: 캐시 키.
            loader: 값을 만들어내는 호출 가능 객체.

        Returns:
            캐시된 값 또는 새로 로드한 값.
        """
        if self._ttl_seconds <= 0:
            return loader()

        now = self._clock.now().timestamp()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]

        value = loader()

        with self._lock:
            if len(self._entries) >= _MAX_ENTRIES:
                self._entries.clear()
            self._entries[key] = (now + self._ttl_seconds, value)
        return value

    def invalidate(self, key: str) -> None:
        """특정 키를 즉시 만료시킨다.

        Args:
            key: 제거할 캐시 키.
        """
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """캐시를 전부 비운다."""
        with self._lock:
            self._entries.clear()
