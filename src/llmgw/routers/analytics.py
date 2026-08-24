"""집계 조회 라우터.

대시보드가 쓰는 읽기 전용 API 다. 모든 엔드포인트가 `X-Admin-Token` 을
요구한다.

`/analytics/dashboard` 는 요약·시계열·4개 축 분해·최근 요청을 한 번에
반환한다. 브라우저가 7번 왕복하는 대신 1번으로 끝내기 위한 것이다. 서버
쪽에서는 일별 집계 파티션을 한 번만 읽어 모든 축을 계산하므로 DynamoDB
읽기량도 줄어든다.
"""

from __future__ import annotations

import datetime
import typing

import fastapi

from llmgw import analytics as analytics_module
from llmgw import domain
from llmgw import errors
from llmgw import services as services_module

router = fastapi.APIRouter(prefix="/analytics", tags=["analytics"])

_JsonDict = dict[str, typing.Any]

# 대시보드 표에 보여줄 최근 요청 기본 건수.
_DEFAULT_RECENT_LIMIT = 25


def _window(
    services: services_module.Services,
    start: str | None,
    end: str | None,
) -> analytics_module.DateWindow:
    """쿼리 파라미터를 조회 기간으로 변환한다."""
    return analytics_module.parse_window(
        start, end, today=services.clock.now().date()
    )


def _parse_dimension(value: str) -> domain.BreakdownDimension:
    """축 문자열을 열거형으로 바꾼다.

    Args:
        value: `team`, `user`, `model`, `key` 중 하나.

    Returns:
        해당 축.

    Raises:
        InvalidRequestError: 지원하지 않는 축인 경우.
    """
    try:
        return domain.BreakdownDimension(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in domain.BreakdownDimension)
        raise errors.InvalidRequestError(
            f"지원하지 않는 축이다: {value}. 사용 가능: {supported}"
        ) from exc


@router.get("/accounts")
def accounts_overview(
    services: services_module.AdminDep,
    start: str | None = None,
    end: str | None = None,
) -> _JsonDict:
    """모든 계정의 기간 합계를 반환한다.

    Args:
        services: 인증된 서비스 컨테이너.
        start: `YYYY-MM-DD` 시작일.
        end: `YYYY-MM-DD` 종료일.

    Returns:
        기간 정보와 계정별 요약.

    Raises:
        InvalidRequestError: 기간이 유효하지 않은 경우.
    """
    window = _window(services, start, end)
    return {
        "window": window.to_api_dict(),
        "data": services.analytics.accounts_overview(window),
    }


@router.get("/summary")
def summary(
    account_id: str,
    services: services_module.AdminDep,
    start: str | None = None,
    end: str | None = None,
) -> _JsonDict:
    """계정의 기간 합계를 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.
        start: 시작일.
        end: 종료일.

    Returns:
        KPI 수치.
    """
    window = _window(services, start, end)
    totals = services.analytics.summary(account_id, window)
    return {
        "account_id": account_id,
        "window": window.to_api_dict(),
        "totals": totals.to_api_dict(),
    }


@router.get("/timeseries")
def timeseries(
    account_id: str,
    services: services_module.AdminDep,
    start: str | None = None,
    end: str | None = None,
) -> _JsonDict:
    """계정의 일별 시계열을 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.
        start: 시작일.
        end: 종료일.

    Returns:
        날짜 오름차순 시계열.
    """
    window = _window(services, start, end)
    return {
        "account_id": account_id,
        "window": window.to_api_dict(),
        "data": services.analytics.timeseries(account_id, window),
    }


@router.get("/breakdown")
def breakdown(
    account_id: str,
    dimension: str,
    services: services_module.AdminDep,
    start: str | None = None,
    end: str | None = None,
) -> _JsonDict:
    """계정의 축별 집계를 반환한다.

    Args:
        account_id: 계정 ID.
        dimension: `team`, `user`, `model`, `key` 중 하나.
        services: 인증된 서비스 컨테이너.
        start: 시작일.
        end: 종료일.

    Returns:
        비용 내림차순 축별 집계.

    Raises:
        InvalidRequestError: 축이나 기간이 유효하지 않은 경우.
    """
    window = _window(services, start, end)
    axis = _parse_dimension(dimension)
    rows = services.analytics.breakdown(account_id, axis, window)
    return {
        "account_id": account_id,
        "dimension": axis.value,
        "window": window.to_api_dict(),
        "data": [row.to_api_dict() for row in rows],
    }


@router.get("/requests")
def recent_requests(
    account_id: str,
    services: services_module.AdminDep,
    date: str | None = None,
    limit: int = _DEFAULT_RECENT_LIMIT,
) -> _JsonDict:
    """특정 날짜의 최근 요청 목록을 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.
        date: `YYYY-MM-DD` 조회 날짜. 생략하면 오늘.
        limit: 최대 건수.

    Returns:
        최신순 요청 목록.

    Raises:
        InvalidRequestError: 날짜 형식이 잘못된 경우.
    """
    target = _parse_date_or_today(services, date)
    return {
        "account_id": account_id,
        "date": target.isoformat(),
        "data": services.analytics.recent_requests(
            account_id, target, limit=limit
        ),
    }


@router.get("/dashboard")
def dashboard(
    account_id: str,
    services: services_module.AdminDep,
    start: str | None = None,
    end: str | None = None,
    recent_limit: int = _DEFAULT_RECENT_LIMIT,
) -> _JsonDict:
    """대시보드 한 화면에 필요한 모든 데이터를 한 번에 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.
        start: 시작일.
        end: 종료일.
        recent_limit: 최근 요청 표에 담을 건수.

    Returns:
        요약, 시계열, 팀·사용자·모델·키 축 분해, 최근 요청.

    Raises:
        InvalidRequestError: 기간이 유효하지 않은 경우.
    """
    window = _window(services, start, end)
    service = services.analytics
    breakdowns = {
        axis.value: [
            row.to_api_dict()
            for row in service.breakdown(account_id, axis, window)
        ]
        for axis in domain.BreakdownDimension
    }
    return {
        "account_id": account_id,
        "window": window.to_api_dict(),
        "totals": service.summary(account_id, window).to_api_dict(),
        "timeseries": service.timeseries(account_id, window),
        "breakdowns": breakdowns,
        "recent_requests": service.recent_requests(
            account_id, window.end, limit=recent_limit
        ),
    }


def _parse_date_or_today(
    services: services_module.Services, value: str | None
) -> datetime.date:
    """날짜 문자열을 파싱하고, 없으면 오늘을 쓴다."""
    if not value:
        return services.clock.now().date()
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise errors.InvalidRequestError(
            "date 는 YYYY-MM-DD 형식이어야 한다."
        ) from exc
