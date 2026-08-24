"""게이트웨이 도메인 모델.

계정 → 팀 → 사용자 → API 키의 4단 계층을 표현한다. 사용량은 항상 이 네
축과 모델 ID를 함께 기록하므로, 대시보드에서 어느 축으로든 집계할 수 있다.

금액은 부동소수 오차를 피하기 위해 `Decimal` 로만 다룬다. DynamoDB 도
숫자를 `Decimal` 로 주고받으므로 변환 지점이 줄어든다.
"""

from __future__ import annotations

import decimal
import enum
import typing

import pydantic


class EntityStatus(enum.StrEnum):
    """계정·팀·사용자·키의 활성 상태."""

    ACTIVE = "active"
    DISABLED = "disabled"


class Granularity(enum.StrEnum):
    """집계 시간 단위."""

    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


class BreakdownDimension(enum.StrEnum):
    """집계 축.

    대시보드가 요구하는 계정·팀·사용자 뷰에 모델과 키를 더한 것이다.
    """

    TEAM = "team"
    USER = "user"
    MODEL = "model"
    KEY = "key"


class _Base(pydantic.BaseModel):
    """공통 설정을 가진 모델 기반 클래스."""

    model_config = pydantic.ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class Account(_Base):
    """테넌트 단위 계정.

    Attributes:
        account_id: 소문자 영숫자와 하이픈으로 된 식별자.
        name: 표시용 이름.
        monthly_budget_usd: 월 예산. `None` 이면 무제한.
        status: 활성 상태. 비활성이면 모든 하위 키가 거부된다.
        created_at: 생성 시각(ISO-8601 UTC).
    """

    account_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""


class Team(_Base):
    """계정 안의 팀.

    Attributes:
        account_id: 소속 계정 ID.
        team_id: 계정 범위에서 유일한 팀 식별자.
        name: 표시용 이름.
        monthly_budget_usd: 팀 월 예산. `None` 이면 계정 예산만 적용된다.
        status: 활성 상태.
        created_at: 생성 시각(ISO-8601 UTC).
    """

    account_id: str
    team_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""


class User(_Base):
    """계정 안의 사용자.

    Attributes:
        account_id: 소속 계정 ID.
        user_id: 계정 범위에서 유일한 사용자 식별자.
        name: 표시용 이름.
        email: 연락용 메일. 선택 사항.
        team_id: 소속 팀 ID. 팀이 없으면 빈 문자열.
        monthly_budget_usd: 사용자 월 예산. `None` 이면 상위 예산만 적용된다.
        status: 활성 상태.
        created_at: 생성 시각(ISO-8601 UTC).
    """

    account_id: str
    user_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    email: str = ""
    team_id: str = ""
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""


class ApiKey(_Base):
    """발급된 API 키의 메타데이터.

    평문 키는 저장하지 않는다. 조회는 SHA-256 해시로만 하고, 사람이 키를
    구분할 수 있도록 접두어 일부만 보관한다.

    Attributes:
        key_id: 키 식별자. 관리 API에서 삭제·조회에 쓴다.
        key_hash: 평문 키의 SHA-256 16진수 해시.
        key_prefix: 평문 키의 앞부분. 화면 표시용이며 인증에 쓰지 않는다.
        account_id: 소속 계정 ID.
        team_id: 소속 팀 ID. 없으면 빈 문자열.
        user_id: 소속 사용자 ID.
        name: 키 용도 메모.
        allowed_models: 허용 모델 목록. 비어 있으면 설정 기본값을 따른다.
        monthly_budget_usd: 키 단위 월 예산. `None` 이면 상위 예산만 적용.
        status: 활성 상태.
        created_at: 생성 시각(ISO-8601 UTC).
        last_used_at: 마지막 사용 시각. 미사용이면 빈 문자열.
    """

    key_id: str
    key_hash: str
    key_prefix: str
    account_id: str
    team_id: str = ""
    user_id: str
    name: str = ""
    allowed_models: tuple[str, ...] = ()
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""
    last_used_at: str = ""


class Principal(_Base):
    """인증을 통과한 호출 주체.

    라우터와 사용량 기록기가 함께 쓰는 값 객체다. 예산 한도는 인증 시점에
    한 번만 읽어 여기에 담아두고, 요청 처리 중 다시 조회하지 않는다.

    Attributes:
        account_id: 계정 ID.
        team_id: 팀 ID. 없으면 빈 문자열.
        user_id: 사용자 ID.
        key_id: 사용된 API 키 ID.
        key_hash: 사용된 키의 SHA-256 해시. 마지막 사용 시각을 갱신할 때
            파티션 키로 필요하다. 해시만으로는 인증할 수 없으므로 프로세스
            메모리에 두는 것이 문제되지 않는다. API 응답에는 넣지 않는다.
        allowed_models: 이 키가 호출할 수 있는 모델 목록. 비어 있으면 제한
            없음을 뜻한다.
        account_budget_usd: 계정 월 예산.
        team_budget_usd: 팀 월 예산.
        user_budget_usd: 사용자 월 예산.
        key_budget_usd: 키 월 예산.
    """

    account_id: str
    team_id: str = ""
    user_id: str
    key_id: str
    key_hash: str = ""
    allowed_models: tuple[str, ...] = ()
    account_budget_usd: decimal.Decimal | None = None
    team_budget_usd: decimal.Decimal | None = None
    user_budget_usd: decimal.Decimal | None = None
    key_budget_usd: decimal.Decimal | None = None


class UsageRecord(_Base):
    """단일 게이트웨이 요청의 사용량 레코드.

    `request_id` 는 게이트웨이가 생성하며, 같은 값으로 두 번 기록해도
    저장소에는 한 건만 남는다. 자세한 보장 방식은 `usage` 모듈을 참고한다.

    Attributes:
        request_id: 요청 식별자. 멱등성 키로도 쓰인다.
        timestamp: 요청 시작 시각(ISO-8601 UTC).
        account_id: 계정 ID.
        team_id: 팀 ID.
        user_id: 사용자 ID.
        key_id: API 키 ID.
        model_id: 클라이언트가 요청한 모델 ID.
        input_tokens: 입력 토큰 수.
        output_tokens: 출력 토큰 수.
        cost_usd: 계산된 비용(USD).
        latency_ms: 게이트웨이 관점의 처리 시간(밀리초).
        status_code: 클라이언트에게 반환한 HTTP 상태 코드.
        error_code: 실패 시 도메인 에러 코드. 성공이면 빈 문자열.
        streamed: 스트리밍 응답이었는지 여부.
        pricing_known: 단가 표에서 모델을 찾았는지 여부. `False` 면 비용이
            0으로 기록되므로 대시보드에서 과소 집계를 인지할 수 있다.
    """

    request_id: str
    timestamp: str
    account_id: str
    team_id: str = ""
    user_id: str
    key_id: str
    model_id: str
    input_tokens: int = pydantic.Field(default=0, ge=0)
    output_tokens: int = pydantic.Field(default=0, ge=0)
    cost_usd: decimal.Decimal = pydantic.Field(
        default=decimal.Decimal("0"), ge=0
    )
    latency_ms: int = pydantic.Field(default=0, ge=0)
    status_code: int = 200
    error_code: str = ""
    streamed: bool = False
    pricing_known: bool = True

    @property
    def total_tokens(self) -> int:
        """입력과 출력 토큰의 합."""
        return self.input_tokens + self.output_tokens

    @property
    def is_success(self) -> bool:
        """2xx 로 끝난 요청인지 여부."""
        return 200 <= self.status_code < 300


class UsageTotals(_Base):
    """집계된 사용량 수치.

    Attributes:
        requests: 총 요청 수.
        success_requests: 성공 요청 수.
        error_requests: 실패 요청 수.
        input_tokens: 입력 토큰 합.
        output_tokens: 출력 토큰 합.
        cost_usd: 비용 합(USD).
        latency_ms_sum: 지연 합. 평균 계산에 쓴다. DynamoDB 의 원자적 ADD
            로는 최댓값을 누적할 수 없어 최대·백분위 지연은 집계 테이블에
            두지 않고 CloudWatch 메트릭으로 본다.
        unpriced_requests: 단가 표에 없는 모델로 처리된 요청 수. 이 값이
            0이 아니면 `cost_usd` 가 실제 지출보다 작다. 비용 누락이 조용히
            지나가지 않게 하려고 집계 축에 함께 넣는다. 예를 들어 새 Claude
            모델이 추가됐는데 `pricing.json` 을 갱신하지 않은 경우 여기에
            잡힌다.
    """

    requests: int = 0
    success_requests: int = 0
    error_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: decimal.Decimal = decimal.Decimal("0")
    latency_ms_sum: int = 0
    unpriced_requests: int = 0

    @property
    def total_tokens(self) -> int:
        """입력과 출력 토큰의 합."""
        return self.input_tokens + self.output_tokens

    @property
    def avg_latency_ms(self) -> int:
        """요청당 평균 지연(밀리초). 요청이 없으면 0."""
        if self.requests <= 0:
            return 0
        return round(self.latency_ms_sum / self.requests)

    @property
    def error_rate(self) -> float:
        """실패 비율(0.0~1.0). 요청이 없으면 0.0."""
        if self.requests <= 0:
            return 0.0
        return self.error_requests / self.requests

    @property
    def is_cost_complete(self) -> bool:
        """비용 합계가 모든 요청을 반영하는지 여부.

        `False` 면 단가를 모르는 모델이 섞여 있어 `cost_usd` 가 실제 지출보다
        작다. 대시보드는 이 값을 근거로 경고를 표시한다.
        """
        return self.unpriced_requests == 0

    def merged_with(self, other: UsageTotals) -> UsageTotals:
        """다른 집계값과 합산한 새 객체를 반환한다.

        여러 날짜 파티션의 결과를 하나로 접을 때 쓴다.

        Args:
            other: 합산할 집계값.

        Returns:
            합산된 새 `UsageTotals`.
        """
        return UsageTotals(
            requests=self.requests + other.requests,
            success_requests=self.success_requests + other.success_requests,
            error_requests=self.error_requests + other.error_requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            latency_ms_sum=self.latency_ms_sum + other.latency_ms_sum,
            unpriced_requests=(
                self.unpriced_requests + other.unpriced_requests
            ),
        )

    def to_api_dict(self) -> dict[str, typing.Any]:
        """대시보드 API 응답용 딕셔너리로 변환한다.

        `Decimal` 은 JSON 직렬화가 안 되므로 비용만 `float` 로 내린다.
        표시용 값이라 정밀도 손실이 문제되지 않는다.

        Returns:
            KPI 카드와 차트가 바로 쓸 수 있는 평평한 딕셔너리.
        """
        return {
            "requests": self.requests,
            "success_requests": self.success_requests,
            "error_requests": self.error_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": float(self.cost_usd),
            "avg_latency_ms": self.avg_latency_ms,
            "error_rate": round(self.error_rate, 6),
            "unpriced_requests": self.unpriced_requests,
            "cost_complete": self.is_cost_complete,
        }


EMPTY_TOTALS = UsageTotals()
