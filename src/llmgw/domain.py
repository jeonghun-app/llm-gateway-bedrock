"""게이트웨이 도메인 모델.

계정 → 팀 → 사용자 → API 키의 4단 계층을 표현한다. 사용량은 항상 이 네
축과 모델 ID를 함께 기록하므로, 대시보드에서 어느 축으로든 집계할 수 있다.

금액은 부동소수 오차를 피하기 위해 `Decimal` 로만 다룬다. DynamoDB 도
숫자를 `Decimal` 로 주고받으므로 변환 지점이 줄어든다.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import typing

import pydantic


class EntityStatus(enum.StrEnum):
    """계정·팀·사용자·키의 활성 상태."""

    ACTIVE = "active"
    DISABLED = "disabled"


class AdminScope(enum.StrEnum):
    """관리 권한 범위.

    `PLATFORM` 은 모든 계정을 다룰 수 있고, `ACCOUNT` 는 자신에게 매핑된
    계정 하나만 다룰 수 있다. 공유 관리 토큰은 항상 `PLATFORM` 이다.
    """

    PLATFORM = "platform"
    ACCOUNT = "account"


class AdminAuthKind(enum.StrEnum):
    """관리 API 인증 방식."""

    SHARED_TOKEN = "shared_token"
    OIDC = "oidc"


class _Base(pydantic.BaseModel):
    """공통 설정을 가진 모델 기반 클래스."""

    model_config = pydantic.ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class AdminPrincipal(_Base):
    """관리 API 를 호출한 주체.

    공유 관리 토큰 하나로는 누가 무엇을 했는지 남지 않고 권한을 좁힐 수도
    없다. OIDC 로 들어온 경우 실제 사람의 식별자와 범위가 채워진다.

    Attributes:
        kind: 인증 방식.
        subject: 주체 식별자. 공유 토큰이면 고정 문자열, OIDC 면 사용자
            식별자(이메일 또는 `sub`).
        scope: 권한 범위.
        account_id: `ACCOUNT` 범위일 때 관리할 수 있는 계정 ID.
        groups: OIDC 토큰에서 읽은 그룹 목록. 감사에 쓴다.
    """

    kind: AdminAuthKind
    subject: str
    scope: AdminScope
    account_id: str = ""
    groups: tuple[str, ...] = ()

    def can_manage(self, account_id: str) -> bool:
        """지정한 계정을 관리할 수 있는지 여부.

        Args:
            account_id: 대상 계정 ID.

        Returns:
            관리 가능하면 `True`.
        """
        if self.scope is AdminScope.PLATFORM:
            return True
        return bool(account_id) and account_id == self.account_id


class AccountAuthConfig(_Base):
    """계정별 외부 인증(OIDC) 설정.

    고객이 이미 쓰는 인증 서버(Amazon Cognito, Okta, Azure AD, Google 등)를
    계정 단위로 붙인다. 발급자(`issuer`)로 토큰이 어느 계정 것인지 판별하므로
    발급자는 계정 간에 겹칠 수 없다.

    Attributes:
        account_id: 소속 계정 ID.
        issuer: OIDC 발급자 URL. 토큰의 `iss` 와 정확히 일치해야 한다.
        jwks_url: JWKS 문서 URL. 비우면 발급자에서 표준 경로를 만든다.
        audience: 허용 클라이언트 ID. 쉼표로 구분한다. 비우면 청중을
            검사하지 않는다.
        user_claim: 사용자 ID 로 쓸 클레임 이름. 비어 있으면 `sub` 를 쓴다.
        team_claim: 팀 ID 로 쓸 클레임 이름. 없으면 팀 없이 동작한다.
        groups_claim: 그룹 목록 클레임 이름. Cognito 는 `cognito:groups` 다.
        admin_groups: 이 계정의 관리자로 인정할 그룹. 쉼표로 구분한다.
            해당 그룹을 가진 사용자는 관리 토큰 없이 이 계정을 관리한다.
        auto_provision: 토큰은 유효하지만 사용자가 레지스트리에 없을 때 자동
            생성할지 여부. 기본은 끈다(fail-closed).
        provision_allowed_models: 자동 생성된 사용자의 키에 적용할 허용 모델.
            쉼표로 구분한다.
        provision_budget_usd: 자동 생성된 사용자의 월 예산. `None` 이면
            무제한.
        status: 활성 상태. 비활성이면 이 계정의 OIDC 인증을 즉시 차단한다.
        created_at: 생성 시각(ISO-8601 UTC).
        updated_at: 수정 시각(ISO-8601 UTC).
    """

    account_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    issuer: str = pydantic.Field(min_length=8, max_length=512)
    jwks_url: str = pydantic.Field(default="", max_length=512)
    audience: str = pydantic.Field(default="", max_length=512)
    user_claim: str = pydantic.Field(default="email", max_length=64)
    team_claim: str = pydantic.Field(default="", max_length=64)
    groups_claim: str = pydantic.Field(default="cognito:groups", max_length=64)
    admin_groups: str = pydantic.Field(default="", max_length=512)
    auto_provision: bool = False
    provision_allowed_models: str = pydantic.Field(default="", max_length=1024)
    provision_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""
    updated_at: str = ""

    @pydantic.model_validator(mode="after")
    def _require_budget_when_auto_provisioning(self) -> AccountAuthConfig:
        """자동 생성을 켰으면 예산을 반드시 지정하게 한다.

        `auto_provision` 은 IdP 에 계정이 있는 사람을 그대로 게이트웨이
        사용자로 만든다. 예산이 없으면 그 사용자는 무제한으로 Bedrock 을
        호출할 수 있고, 비용은 계정 소유자가 부담한다. 실수 한 번이 곧
        청구서로 오므로 설정 단계에서 막는다.

        Returns:
            검증된 자기 자신.

        Raises:
            ValueError: 자동 생성이 켜져 있는데 예산이 없는 경우.
        """
        if self.auto_provision and self.provision_budget_usd is None:
            raise ValueError(
                "auto_provision 을 켜면 provision_budget_usd 를 지정해야"
                " 한다. 예산이 없으면 자동 생성된 사용자가 무제한으로"
                " 호출할 수 있다."
            )
        return self

    @property
    def audience_list(self) -> tuple[str, ...]:
        """허용 클라이언트 ID 튜플."""
        return _split_csv(self.audience)

    @property
    def admin_group_list(self) -> tuple[str, ...]:
        """관리자 그룹 튜플."""
        return _split_csv(self.admin_groups)

    @property
    def provision_model_list(self) -> tuple[str, ...]:
        """자동 생성 사용자에게 줄 허용 모델 튜플."""
        return _split_csv(self.provision_allowed_models)

    @property
    def effective_jwks_url(self) -> str:
        """JWKS 문서 URL. 비어 있으면 발급자에서 표준 경로를 만든다."""
        if self.jwks_url:
            return self.jwks_url
        return f"{self.issuer.rstrip('/')}/.well-known/jwks.json"


def _split_csv(raw: str) -> tuple[str, ...]:
    """쉼표 구분 문자열을 공백 제거한 튜플로 바꾼다.

    Args:
        raw: 쉼표로 구분된 문자열.

    Returns:
        빈 항목이 제거된 튜플.
    """
    stripped = raw.strip()
    if not stripped:
        return ()
    return tuple(item.strip() for item in stripped.split(",") if item.strip())


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
        rpm_limit: 분당 요청 한도. `None` 이면 서버 기본값을 따른다.
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
    rpm_limit: int | None = pydantic.Field(default=None, ge=1, le=100_000)
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
        rpm_limit: 분당 요청 한도. `None` 이면 사용자 한도를 따른다.
        expires_at: 만료 시각(ISO-8601 UTC). 빈 문자열이면 무기한.
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
    rpm_limit: int | None = pydantic.Field(default=None, ge=1, le=100_000)
    expires_at: str = ""
    status: EntityStatus = EntityStatus.ACTIVE
    created_at: str = ""
    last_used_at: str = ""

    def is_expired(self, now: datetime.datetime) -> bool:
        """만료 시각이 지났는지 여부.

        만료된 키는 삭제하지 않고 거부만 한다. 사용량 집계의 정렬 키가
        `KEY#<key_id>` 라서 키를 지우면 과거 집계에서 이름을 붙일 수 없다.

        Args:
            now: 현재 시각(UTC).

        Returns:
            만료 시각이 설정돼 있고 그 시각이 지났으면 `True`.
        """
        if not self.expires_at:
            return False
        try:
            deadline = datetime.datetime.fromisoformat(
                self.expires_at.replace("Z", "+00:00")
            )
        except ValueError:
            # 저장된 값이 깨졌다면 만료로 다룬다. 파싱 실패를 "무기한 유효"
            # 로 해석하면 회수하려던 키가 영구히 살아남는다.
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=datetime.UTC)
        return now >= deadline


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
        rpm_limit: 적용할 분당 요청 한도. `None` 이면 제한 없음.
        rate_scope: 레이트 리밋 카운터를 셀 단위. 키가 있으면
            `KEY#<key_id>`, 없으면(OIDC 경로) `USER#<user_id>` 다.
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
    rpm_limit: int | None = None
    rate_scope: str = ""


class UsageRecord(_Base):
    """단일 게이트웨이 요청의 사용량 레코드.

    `usage_id` 는 게이트웨이가 요청마다 새로 만드는 저장소 키다. Bedrock
    호출은 그때마다 실제 비용이 발생하므로, 사용량은 언제나 한 건씩
    기록되어야 한다. `request_id` 는 클라이언트가 지정할 수 있는 추적용
    상관관계 ID 이며 키로 쓰지 않는다. 자세한 이유는 `usage` 모듈을
    참고한다.

    Attributes:
        usage_id: 사용량 레코드 식별자. 서버가 요청마다 생성한다.
        request_id: 요청 상관관계 ID. 클라이언트의 `X-Request-Id` 가 있으면
            그 값이고, 없으면 서버가 만든다. 로그 추적용이다.
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

    usage_id: str
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
