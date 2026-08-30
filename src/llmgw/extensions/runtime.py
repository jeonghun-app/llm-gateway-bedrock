"""확장 로딩과 실행.

## 왜 자동 발견을 하지 않는가

설치만으로 요청 경로에 코드가 끼어들면 안 된다. 파이썬 모듈은 import 만으로
최상위 코드가 실행되고, 확장은 프롬프트와 태스크 역할 자격증명에 접근할 수
있다. 의존성을 하나 추가한 것이 곧 그 권한을 준 것이 되어서는 안 된다.

그래서 `LLMGW_REQUEST_FILTERS` 에 **명시적으로 나열한 것만** import 하고
실행한다. 나열하지 않은 모듈은 import 조차 하지 않는다.

`entry_points` 기반 발견을 쓰지 않은 이유는 두 가지다. 확장이 정식 배포
패키지로 설치돼 메타데이터를 가져야 하는 요구가 생기는데, 파생 이미지에
모듈만 얹는 가장 단순한 경로에서는 그 조건이 성립하지 않는다. 그리고
`entry_points` 의 이점인 "설치된 것 자동 발견" 은 우리가 의도적으로
하지 않으려는 동작이다.

## 왜 시작 시 실패하는가

설정에 적은 확장을 불러오지 못했는데 확장 없이 기동하면, 운영자는 필터가
동작한다고 믿는 상태에서 필터 없이 트래픽을 받는다. PII 마스킹을 켰다고
믿는 배포가 마스킹 없이 도는 것이 가장 나쁜 결과다. 그래서 로딩 실패는
기동 실패다.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import importlib
import typing

from llmgw import observability
from llmgw.extensions import v1

# 확장 하나의 기본 제한 시간. Bedrock 읽기 타임아웃(기본 300초)과 달리 짧게
# 잡는다. 필터는 지역 검사나 짧은 정책 조회여야 하고, 여기서 오래 기다리면
# 게이트웨이의 워커가 묶인다.
_DEFAULT_TIMEOUT_SECONDS = 1.0


class ExtensionLoadError(Exception):
    """확장을 불러오지 못했다. 기동을 중단시키기 위한 예외다."""


@dataclasses.dataclass(frozen=True, slots=True)
class LoadedFilter:
    """불러온 요청 필터 하나.

    Attributes:
        name: 설정에 적힌 이름. 로그와 메트릭의 식별자로 쓴다. 확장이 스스로
            보고한 이름을 쓰지 않는다. 거짓 보고를 막으려는 것이다.
        instance: 확장 인스턴스.
    """

    name: str
    instance: v1.RequestFilter


def _import_filter(spec: str) -> v1.RequestFilter:
    """`module:Class` 명세로 확장 인스턴스를 만든다.

    Args:
        spec: `패키지.모듈:클래스` 형태의 문자열.

    Returns:
        인자 없이 생성한 확장 인스턴스.

    Raises:
        ExtensionLoadError: 형식이 틀렸거나, import·생성에 실패했거나,
            계약에 맞는 메서드가 없는 경우.
    """
    if spec.count(":") != 1:
        raise ExtensionLoadError(
            f"확장 명세는 'module:Class' 형태여야 한다: {spec!r}"
        )
    module_name, class_name = spec.split(":")
    if not module_name or not class_name:
        raise ExtensionLoadError(
            f"확장 명세는 'module:Class' 형태여야 한다: {spec!r}"
        )

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - import 는 임의 코드를 실행한다
        raise ExtensionLoadError(
            f"확장 모듈을 import 할 수 없다: {module_name!r} ({exc})"
        ) from exc

    factory = getattr(module, class_name, None)
    if factory is None:
        raise ExtensionLoadError(f"{module_name!r} 에 {class_name!r} 가 없다")

    try:
        instance = factory()
    except Exception as exc:  # noqa: BLE001 - 확장 생성자는 임의 코드다
        raise ExtensionLoadError(
            f"확장을 생성할 수 없다: {spec!r} ({exc})"
        ) from exc

    # Protocol 은 런타임 검사를 하지 않으므로 필요한 메서드가 있는지 직접
    # 본다. 없으면 첫 요청에서야 AttributeError 가 나는데, 그때는 이미
    # 필터가 동작한다고 믿고 트래픽을 받은 상태다.
    if not callable(getattr(instance, "filter_request", None)):
        raise ExtensionLoadError(
            f"확장에 filter_request 메서드가 없다: {spec!r}"
        )
    return typing.cast(v1.RequestFilter, instance)


def load_request_filters(
    specs: typing.Sequence[str],
) -> tuple[LoadedFilter, ...]:
    """설정에 나열된 요청 필터를 순서대로 불러온다.

    Args:
        specs: `module:Class` 명세 목록. 설정 순서가 적용 순서다.

    Returns:
        불러온 필터 튜플.

    Raises:
        ExtensionLoadError: 하나라도 불러오지 못했거나 이름이 중복된 경우.
    """
    loaded: list[LoadedFilter] = []
    seen: set[str] = set()
    for spec in specs:
        if spec in seen:
            # 같은 확장을 두 번 적용하면 변형이 두 번 일어난다. 의도한
            # 것인지 실수인지 알 수 없으므로 거부한다.
            raise ExtensionLoadError(f"확장이 중복 지정됐다: {spec!r}")
        seen.add(spec)
        loaded.append(LoadedFilter(name=spec, instance=_import_filter(spec)))
    return tuple(loaded)


class RequestFilterChain:
    """요청 필터들을 순서대로 실행한다.

    확장은 신뢰된 코드지만 고장날 수 있다. 이 클래스가 책임지는 것은 세
    가지다.

    1. **제한 시간.** 확장을 전용 단일 워커 스레드에서 실행하고 제한 시간까지
       기다린다. 넘기면 결과를 버리고 503 으로 실패한다.
    2. **차단.** 제한 시간을 넘긴 확장은 이후 요청에서 즉시 실패시킨다. 워커가
       묶여 있으므로 계속 작업을 넣으면 큐만 쌓인다.
    3. **감사.** 확장 이름과 결과·소요 시간을 남긴다. 프롬프트 본문은 남기지
       않는다.

    **이것은 샌드박스가 아니다.** 파이썬 스레드는 강제로 멈출 수 없다. 제한
    시간은 "늦게 온 결과를 쓰지 않는다" 는 보장일 뿐, 확장 코드가 CPU 를
    계속 쓰거나 외부 부작용을 일으키는 것을 막지 못한다.
    """

    def __init__(
        self,
        *,
        filters: tuple[LoadedFilter, ...],
        logger: observability.Logger,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """체인을 만든다.

        Args:
            filters: 적용 순서대로 정렬된 필터.
            logger: 구조화 로거.
            timeout_seconds: 확장 하나의 제한 시간.
        """
        self._filters = filters
        self._logger = logger
        self._timeout = timeout_seconds
        # 확장별로 워커를 하나만 둔다. 한 확장이 묶여도 다른 확장은 계속
        # 동작하고, 확장 내부에서 스레드 안전성을 가정하지 않아도 된다.
        self._executors = {
            item.name: concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"ext-{item.name}"
            )
            for item in filters
        }
        self._blocked: set[str] = set()

    @property
    def is_empty(self) -> bool:
        """활성 확장이 없으면 True. 호출부가 오버헤드를 건너뛰는 데 쓴다."""
        return not self._filters

    @property
    def names(self) -> tuple[str, ...]:
        """활성 확장 이름. 기동 로그에 남긴다."""
        return tuple(item.name for item in self._filters)

    def apply(
        self, payload: v1.RequestPayload, *, context: v1.RequestContext
    ) -> v1.RequestPayload:
        """필터를 순서대로 적용한 결과를 반환한다.

        Args:
            payload: 원본 요청 본문.
            context: 요청 컨텍스트.

        Returns:
            마지막 필터가 반환한 요청 본문.

        Raises:
            RequestRejectedError: 확장이 요청을 거부한 경우.
            ExtensionUnavailableError: 확장이 고장났거나 제한 시간을 넘긴
                경우. 통과시키지 않는다. 검사가 동작하지 않는 상태에서
                요청을 흘려보내면 확장을 켠 의미가 없다.
        """
        current = payload
        for item in self._filters:
            current = self._apply_one(item, current, context)
        return current

    def _apply_one(
        self,
        item: LoadedFilter,
        payload: v1.RequestPayload,
        context: v1.RequestContext,
    ) -> v1.RequestPayload:
        """확장 하나를 제한 시간 안에서 실행한다."""
        if item.name in self._blocked:
            # 이미 묶인 확장이다. 기다릴 이유가 없다.
            raise v1.ExtensionUnavailableError(
                "요청 필터가 응답하지 않는 상태다"
            )

        started = datetime.datetime.now(datetime.UTC)
        future = self._executors[item.name].submit(
            item.instance.filter_request, payload, context=context
        )
        # 확장은 계약을 지킨다고 선언했을 뿐 임의 값을 반환할 수 있다.
        # 타입 검사기가 반환 타입을 믿어 아래 isinstance 검사를 없는 코드로
        # 보지 않도록 object 로 받는다.
        result: object
        try:
            result = typing.cast(object, future.result(timeout=self._timeout))
        except concurrent.futures.TimeoutError as exc:
            # 스레드를 멈출 수 없으므로 이 확장을 차단 상태로 표시한다.
            # 그러지 않으면 이후 요청이 묶인 워커에 계속 쌓인다.
            self._blocked.add(item.name)
            self._log(item, context, "timeout", started)
            raise v1.ExtensionUnavailableError(
                "요청 필터가 제한 시간을 넘겼다"
            ) from exc
        except v1.RequestRejectedError:
            self._log(item, context, "rejected", started)
            raise
        except Exception as exc:  # noqa: BLE001 - 확장은 임의 코드다
            # 확장이 던진 임의 예외를 그대로 올리지 않는다. 확장이 HTTP
            # 상태 코드를 고를 수 있게 되면 안 되고, 예외 문구에 프롬프트
            # 조각이나 자격증명이 섞여 클라이언트로 나갈 수 있다.
            self._logger.exception(
                "요청 필터가 예외를 던졌다",
                extra={
                    "extension": item.name,
                    "request_id": context.request_id,
                },
            )
            self._log(item, context, "failed", started)
            raise v1.ExtensionUnavailableError(
                "요청 필터가 요청을 처리하지 못했다"
            ) from exc

        if not isinstance(result, v1.RequestPayload):
            self._log(item, context, "invalid_return", started)
            raise v1.ExtensionUnavailableError(
                "요청 필터가 잘못된 형식을 반환했다"
            )
        if not result.messages:
            # 메시지가 없으면 Bedrock 이 ValidationException 을 던진다.
            # 원인을 확장으로 지목할 수 있게 여기서 막는다.
            self._log(item, context, "empty_messages", started)
            raise v1.ExtensionUnavailableError("요청 필터가 빈 대화를 반환했다")

        outcome = "modified" if result != payload else "allowed"
        self._log(item, context, outcome, started)
        return result

    def _log(
        self,
        item: LoadedFilter,
        context: v1.RequestContext,
        outcome: str,
        started: datetime.datetime,
    ) -> None:
        """확장 실행 결과를 남긴다.

        프롬프트 본문과 그 해시를 남기지 않는다. 짧은 개인정보는 해시에서
        사전 대입으로 복원될 수 있다.
        """
        elapsed = datetime.datetime.now(datetime.UTC) - started
        self._logger.info(
            "요청 필터를 실행했다",
            extra={
                "extension": item.name,
                "outcome": outcome,
                "duration_ms": int(elapsed.total_seconds() * 1000),
                "request_id": context.request_id,
                "account_id": context.principal.account_id,
            },
        )
