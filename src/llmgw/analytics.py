"""대시보드용 집계 조회 서비스.

읽기는 항상 사전 집계 테이블(`usage-agg`)에서만 한다. 원본 레코드를
스캔하지 않으므로 요청 수가 늘어도 대시보드 응답 시간이 변하지 않는다.

기간 조회는 일 단위 파티션을 병렬로 읽어 합산한다. 월 파티션을 쓰면 조회
수가 줄지만 임의 구간(예: 8월 10일~9월 5일)을 정확히 잘라낼 수 없다.
정확도를 택하고 대신 병렬 조회로 지연을 줄였다.
"""

from __future__ import annotations

import dataclasses
import datetime
import typing

from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import repository

# 한 번에 조회할 수 있는 최대 일수. DynamoDB Query 를 이 수만큼 병렬로
# 호출하므로 상한이 없으면 대시보드 한 번 클릭이 수백 회 조회로 번진다.
MAX_RANGE_DAYS = 93

_TOTAL_SORT_KEY = "TOTAL"


@dataclasses.dataclass(frozen=True)
class DateWindow:
    """조회 기간.

    Attributes:
        start: 시작일(포함).
        end: 종료일(포함).
    """

    start: datetime.date
    end: datetime.date

    @property
    def days(self) -> list[datetime.date]:
        """기간에 포함된 모든 날짜."""
        return clock.date_range(self.start, self.end)

    def to_api_dict(self) -> dict[str, str]:
        """API 응답에 포함할 기간 표현을 만든다."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclasses.dataclass(frozen=True)
class BreakdownRow:
    """축별 집계 한 줄.

    Attributes:
        key: 축의 식별자(팀 ID, 사용자 ID, 모델 ID, 키 ID).
        label: 화면 표시용 이름. 레지스트리에서 찾지 못하면 식별자를 쓴다.
        team_id: 사용자 축일 때 소속 팀 ID. 그 외에는 빈 문자열.
        totals: 집계 수치.
    """

    key: str
    label: str
    team_id: str
    totals: domain.UsageTotals

    def to_api_dict(self) -> dict[str, typing.Any]:
        """API 응답용 딕셔너리로 변환한다."""
        payload: dict[str, typing.Any] = {
            "key": self.key,
            "label": self.label,
        }
        if self.team_id:
            payload["team_id"] = self.team_id
        payload.update(self.totals.to_api_dict())
        return payload


def parse_window(
    start: str | None,
    end: str | None,
    *,
    today: datetime.date,
    default_days: int = 30,
) -> DateWindow:
    """쿼리 문자열의 기간을 검증해 `DateWindow` 로 만든다.

    Args:
        start: `YYYY-MM-DD` 시작일. 비어 있으면 `end` 기준으로 역산한다.
        end: `YYYY-MM-DD` 종료일. 비어 있으면 `today`.
        today: 기본값 계산에 쓸 오늘 날짜.
        default_days: `start` 가 없을 때 되돌아갈 일수.

    Returns:
        검증된 기간.

    Raises:
        InvalidRequestError: 날짜 형식이 틀렸거나, 시작일이 종료일보다
            늦거나, 허용 범위를 넘은 경우.
    """
    try:
        end_date = clock.parse_date(end) if end else today
        start_date = (
            clock.parse_date(start)
            if start
            else end_date - datetime.timedelta(days=default_days - 1)
        )
    except ValueError as exc:
        raise errors.InvalidRequestError(
            "날짜는 YYYY-MM-DD 형식이어야 한다."
        ) from exc

    if start_date > end_date:
        raise errors.InvalidRequestError(
            f"시작일이 종료일보다 늦다: {start_date} > {end_date}"
        )
    span = (end_date - start_date).days + 1
    if span > MAX_RANGE_DAYS:
        raise errors.InvalidRequestError(
            f"조회 범위가 너무 넓다: {span}일. 최대 {MAX_RANGE_DAYS}일."
        )
    return DateWindow(start=start_date, end=end_date)


class AnalyticsService:
    """집계 조회 서비스."""

    def __init__(
        self,
        *,
        usage_store: repository.UsageStore,
        registry: repository.RegistryRepository,
    ) -> None:
        """서비스를 만든다.

        Args:
            usage_store: 집계 테이블을 읽을 저장소.
            registry: 표시용 이름을 얻을 레지스트리 저장소.
        """
        self._usage_store = usage_store
        self._registry = registry

    def daily_totals(
        self, account_id: str, window: DateWindow
    ) -> dict[datetime.date, dict[str, domain.UsageTotals]]:
        """기간의 일별 축 집계를 모두 읽는다.

        여러 엔드포인트가 같은 데이터를 쓰므로 이 메서드 하나로 모아
        한 번만 조회한다.

        Args:
            account_id: 계정 ID.
            window: 조회 기간.

        Returns:
            날짜 → (정렬 키 → 집계) 매핑. 데이터가 없는 날짜도 빈
            딕셔너리로 포함된다.
        """
        days = window.days
        partitions = {
            repository.agg_pk(
                account_id, domain.Granularity.DAY, day.isoformat()
            ): day
            for day in days
        }
        raw = self._usage_store.query_partitions(list(partitions))
        return {
            day: raw.get(partition, {}) for partition, day in partitions.items()
        }

    def summary(
        self, account_id: str, window: DateWindow
    ) -> domain.UsageTotals:
        """기간 전체 합계를 반환한다.

        Args:
            account_id: 계정 ID.
            window: 조회 기간.

        Returns:
            합산된 집계 수치.
        """
        return self._sum_total(self.daily_totals(account_id, window))

    def timeseries(
        self, account_id: str, window: DateWindow
    ) -> list[dict[str, typing.Any]]:
        """일별 시계열을 반환한다.

        Args:
            account_id: 계정 ID.
            window: 조회 기간.

        Returns:
            날짜 오름차순 리스트. 데이터가 없는 날짜도 0으로 채워 넣어
            차트에 구멍이 생기지 않게 한다.
        """
        per_day = self.daily_totals(account_id, window)
        series: list[dict[str, typing.Any]] = []
        for day in window.days:
            totals = per_day.get(day, {}).get(
                _TOTAL_SORT_KEY, domain.EMPTY_TOTALS
            )
            entry: dict[str, typing.Any] = {"date": day.isoformat()}
            entry.update(totals.to_api_dict())
            series.append(entry)
        return series

    def breakdown(
        self,
        account_id: str,
        dimension: domain.BreakdownDimension,
        window: DateWindow,
    ) -> list[BreakdownRow]:
        """축별 집계를 비용 내림차순으로 반환한다.

        Args:
            account_id: 계정 ID.
            dimension: 집계 축.
            window: 조회 기간.

        Returns:
            비용이 큰 순서로 정렬된 축별 집계.
        """
        prefix = f"{dimension.name}#"
        merged: dict[str, domain.UsageTotals] = {}
        for axis_totals in self.daily_totals(account_id, window).values():
            for sort_key, totals in axis_totals.items():
                if not sort_key.startswith(prefix):
                    continue
                axis_value = sort_key[len(prefix) :]
                current = merged.get(axis_value, domain.EMPTY_TOTALS)
                merged[axis_value] = current.merged_with(totals)

        labels, team_of_user = self._labels_for(account_id, dimension)
        rows = [
            BreakdownRow(
                key=axis_value,
                label=labels.get(axis_value, axis_value),
                team_id=team_of_user.get(axis_value, ""),
                totals=totals,
            )
            for axis_value, totals in merged.items()
        ]
        # 비용이 같으면 요청 수, 그다음 키 이름으로 정렬해 결과 순서를
        # 결정적으로 만든다. 테스트가 순서에 의존할 수 있게 하기 위함이다.
        rows.sort(
            key=lambda row: (
                -row.totals.cost_usd,
                -row.totals.requests,
                row.key,
            )
        )
        return rows

    def accounts_overview(
        self, window: DateWindow
    ) -> list[dict[str, typing.Any]]:
        """모든 계정의 기간 합계를 반환한다.

        계정 수 × 일수 만큼의 파티션을 한 번에 병렬 조회한다.

        Args:
            window: 조회 기간.

        Returns:
            비용 내림차순 계정 요약 리스트.
        """
        accounts = self._registry.list_accounts()
        if not accounts:
            return []

        days = window.days
        partition_owner: dict[str, str] = {}
        for account in accounts:
            for day in days:
                partition = repository.agg_pk(
                    account.account_id,
                    domain.Granularity.DAY,
                    day.isoformat(),
                )
                partition_owner[partition] = account.account_id

        raw = self._usage_store.query_partitions(list(partition_owner))
        per_account: dict[str, domain.UsageTotals] = {
            account.account_id: domain.EMPTY_TOTALS for account in accounts
        }
        for partition, account_id in partition_owner.items():
            totals = raw.get(partition, {}).get(
                _TOTAL_SORT_KEY, domain.EMPTY_TOTALS
            )
            per_account[account_id] = per_account[account_id].merged_with(
                totals
            )

        rows: list[dict[str, typing.Any]] = []
        for account in accounts:
            entry: dict[str, typing.Any] = {
                "account_id": account.account_id,
                "label": account.name,
                "status": account.status.value,
                "monthly_budget_usd": (
                    float(account.monthly_budget_usd)
                    if account.monthly_budget_usd is not None
                    else None
                ),
            }
            entry.update(per_account[account.account_id].to_api_dict())
            rows.append(entry)
        rows.sort(key=lambda row: (-row["cost_usd"], row["account_id"]))
        return rows

    def recent_requests(
        self, account_id: str, day: datetime.date, *, limit: int = 50
    ) -> list[dict[str, typing.Any]]:
        """특정 날짜의 최근 요청 목록을 반환한다.

        원본 레코드를 읽는 유일한 조회다. 하루 파티션에 대해 시간 역순
        상위 N건만 읽으므로 비용이 제한된다.

        Args:
            account_id: 계정 ID.
            day: 조회 날짜.
            limit: 최대 건수.

        Returns:
            최신순 요청 요약 리스트.
        """
        items = self._usage_store.list_records(
            account_id, day.isoformat(), limit=limit
        )
        rows: list[dict[str, typing.Any]] = []
        for item in items:
            rows.append(
                {
                    "request_id": str(item.get("request_id", "")),
                    "timestamp": str(item.get("ts", "")),
                    "team_id": str(item.get("team_id", "")),
                    "user_id": str(item.get("user_id", "")),
                    "key_id": str(item.get("key_id", "")),
                    "model_id": str(item.get("model_id", "")),
                    "input_tokens": int(item.get("input_tokens", 0) or 0),
                    "output_tokens": int(item.get("output_tokens", 0) or 0),
                    "cost_usd": float(item.get("cost_usd", 0) or 0),
                    "latency_ms": int(item.get("latency_ms", 0) or 0),
                    "status_code": int(item.get("status_code", 0) or 0),
                    "error_code": str(item.get("error_code", "")),
                    "streamed": bool(item.get("streamed", False)),
                }
            )
        return rows

    # -- 내부 ---------------------------------------------------------------

    @staticmethod
    def _sum_total(
        per_day: dict[datetime.date, dict[str, domain.UsageTotals]],
    ) -> domain.UsageTotals:
        """일별 전체 합계 행을 하나로 접는다."""
        result = domain.EMPTY_TOTALS
        for axis_totals in per_day.values():
            result = result.merged_with(
                axis_totals.get(_TOTAL_SORT_KEY, domain.EMPTY_TOTALS)
            )
        return result

    def _labels_for(
        self, account_id: str, dimension: domain.BreakdownDimension
    ) -> tuple[dict[str, str], dict[str, str]]:
        """축 식별자를 표시용 이름으로 바꿀 매핑을 만든다.

        Args:
            account_id: 계정 ID.
            dimension: 집계 축.

        Returns:
            (식별자 → 표시 이름, 사용자 ID → 팀 ID) 튜플. 모델 축은
            표시 이름이 곧 모델 ID 라 빈 매핑을 반환한다.
        """
        if dimension is domain.BreakdownDimension.TEAM:
            teams = self._registry.list_teams(account_id)
            return {team.team_id: team.name for team in teams}, {}
        if dimension is domain.BreakdownDimension.USER:
            users = self._registry.list_users(account_id)
            return (
                {user.user_id: user.name for user in users},
                {user.user_id: user.team_id for user in users},
            )
        if dimension is domain.BreakdownDimension.KEY:
            keys = self._registry.list_api_keys(account_id)
            return (
                {
                    api_key.key_id: (
                        api_key.name or api_key.key_prefix or api_key.key_id
                    )
                    for api_key in keys
                },
                {api_key.key_id: api_key.team_id for api_key in keys},
            )
        return {}, {}
