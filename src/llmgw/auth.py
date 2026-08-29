"""API 키 인증과 정책 검사.

인증은 세 단계로 나뉜다.

1. `authenticate` — 키 해시로 키를 찾고, 키·계정·팀·사용자의 활성 상태를
   확인해 `Principal` 을 만든다.
2. `enforce_model` — 요청한 모델이 키의 허용 목록에 있는지 확인한다.
3. `enforce_budget` — 이번 달 누적 비용이 계정·팀·사용자·키 예산을
   넘지 않았는지 확인한다.

세 단계를 분리한 이유는 사용량 기록 정책 때문이다. 1단계 실패는 호출
주체를 특정할 수 없으므로 사용량을 남길 수 없다. 2·3단계 실패는 주체가
확정되어 있어 실패 요청으로 집계된다.
"""

from __future__ import annotations

import datetime
import decimal
import typing

from llmgw import apikey
from llmgw import cache
from llmgw import clock
from llmgw import config
from llmgw import domain
from llmgw import errors
from llmgw import oidc
from llmgw import pricing
from llmgw import repository

_BEARER_PREFIX = "bearer "

# 예산 검사는 이번 달 누적치를 본다. 월 경계에서 값이 0으로 리셋되는 것은
# 의도된 동작이다.
_BUDGET_GRANULARITY = domain.Granularity.MONTH


class _BudgetCheck(typing.NamedTuple):
    """예산 검사 대상 하나."""

    scope: str
    limit: decimal.Decimal
    sort_key: str


class Authenticator:
    """API 키 기반 인증기."""

    def __init__(
        self,
        *,
        registry: repository.RegistryRepository,
        usage_store: repository.UsageStore,
        settings: config.Settings,
        metadata_cache: cache.TtlCache[typing.Any] | None = None,
        oidc_verifier: oidc.OidcVerifier | None = None,
        clock_source: clock.Clock | None = None,
    ) -> None:
        """인증기를 만든다.

        Args:
            registry: 레지스트리 저장소.
            usage_store: 예산 검사에 쓸 사용량 저장소.
            settings: 런타임 설정.
            metadata_cache: 계정·팀·사용자 캐시. 생략하면 기본 TTL 캐시를
                만든다.
            oidc_verifier: 외부 인증 토큰 검증기. `None` 이면 JWT 자격증명을
                받지 않는다(미구성을 통과로 해석하지 않는다).
            clock_source: 시각 제공자. 사용자 자동 생성 시각에 쓴다.
        """
        self._registry = registry
        self._usage_store = usage_store
        self._settings = settings
        self._oidc = oidc_verifier
        self._clock = clock_source or clock.SYSTEM_CLOCK
        self._cache: cache.TtlCache[typing.Any] = (
            metadata_cache or cache.TtlCache()
        )

    def authenticate(self, authorization: str | None) -> domain.Principal:
        """`Authorization` 헤더를 검증해 호출 주체를 만든다.

        자격증명은 두 형태를 받는다. 접두어로 구분한다.

        - `sk-llmgw-...`: 게이트웨이가 발급한 API 키.
        - 그 외: 고객 IdP 가 발급한 OIDC 액세스 토큰. 발급자로 계정을 찾아
          그 계정에 등록된 설정으로 검증한다.

        어느 쪽이든 실패 메시지는 같다. "API 키 형식은 맞는데 값이 틀렸다"
        와 "JWT 서명이 틀렸다" 를 구분해 알려주면 유효한 자격증명을 찾는
        단서가 된다.

        Args:
            authorization: `Bearer <credential>` 형태의 헤더 값.

        Returns:
            인증된 `Principal`.

        Raises:
            AuthenticationError: 헤더가 없거나 형식이 틀렸거나, 자격증명이
                유효하지 않거나, 관련 엔티티가 비활성인 경우.
            PermissionDeniedError: 토큰은 유효하지만 사용자가 등록되지 않아
                호출을 허용할 수 없는 경우.
        """
        credential = self._extract_bearer(authorization)
        if credential.startswith(apikey.KEY_PREFIX):
            return self._authenticate_api_key(credential)
        return self._authenticate_oidc(credential)

    def _authenticate_api_key(self, plaintext: str) -> domain.Principal:
        """API 키로 호출 주체를 만든다."""
        api_key = self._registry.get_api_key_by_hash(
            apikey.hash_api_key(plaintext)
        )
        # 키가 없는 경우와 비활성인 경우를 같은 메시지로 응답한다. 존재
        # 여부를 구분해 알려주면 키 열거 공격의 신호가 된다.
        if api_key is None or api_key.status is not domain.EntityStatus.ACTIVE:
            raise errors.AuthenticationError(
                "API 키가 유효하지 않거나 비활성 상태다."
            )
        # 만료된 키는 삭제하지 않고 거부만 한다. 지우면 사용량 집계의
        # `KEY#<key_id>` 축에 이름을 붙일 수 없다. 만료 사실을 별도 메시지로
        # 알려주지 않는 것은 키 열거 공격의 신호를 줄이기 위해서다.
        if api_key.is_expired(self._clock.now()):
            raise errors.AuthenticationError(
                "API 키가 유효하지 않거나 비활성 상태다."
            )

        account = self._load_account(api_key.account_id)
        if account is None or account.status is not domain.EntityStatus.ACTIVE:
            raise errors.AuthenticationError(
                "API 키가 유효하지 않거나 비활성 상태다."
            )

        team: domain.Team | None = None
        if api_key.team_id:
            team = self._load_team(api_key.account_id, api_key.team_id)
            # 팀이 사라졌으면(삭제되었거나 최종 일관성으로 아직 안 보이면)
            # 거부한다. 예전에는 팀이 None 이면 통과시켜, 삭제된 팀을 참조하는
            # 고아 키가 계속 인증되는 fail-open 이었다. 존재 여부를 외부에
            # 구분해 알리지 않도록 키 무효와 같은 메시지를 쓴다.
            if team is None or team.status is not domain.EntityStatus.ACTIVE:
                raise errors.AuthenticationError(
                    "API 키가 유효하지 않거나 비활성 상태다."
                )

        # 사용자도 마찬가지로 fail-closed 다. 키는 반드시 사용자에 귀속되므로
        # 사용자가 사라졌으면 그 키는 더 이상 유효하지 않다.
        user = self._load_user(api_key.account_id, api_key.user_id)
        if user is None or user.status is not domain.EntityStatus.ACTIVE:
            raise errors.AuthenticationError(
                "API 키가 유효하지 않거나 비활성 상태다."
            )

        allowed = api_key.allowed_models or (
            self._settings.default_allowed_model_list
        )
        return domain.Principal(
            account_id=api_key.account_id,
            team_id=api_key.team_id,
            user_id=api_key.user_id,
            key_id=api_key.key_id,
            key_hash=api_key.key_hash,
            allowed_models=allowed,
            account_budget_usd=account.monthly_budget_usd,
            team_budget_usd=team.monthly_budget_usd if team else None,
            user_budget_usd=user.monthly_budget_usd if user else None,
            key_budget_usd=api_key.monthly_budget_usd,
            # 키 한도가 사용자 한도를 덮어쓴다. 더 좁은 범위가 우선한다.
            rpm_limit=(
                api_key.rpm_limit
                if api_key.rpm_limit is not None
                else user.rpm_limit
            ),
            rate_scope=f"KEY#{api_key.key_id}",
        )

    def _authenticate_oidc(self, token: str) -> domain.Principal:
        """OIDC 액세스 토큰으로 호출 주체를 만든다.

        키 없이 IdP 토큰만으로 호출하는 경로다. 계정·팀·사용자 예산은 API 키
        경로와 동일하게 적용된다. 키 단위 예산은 키가 없으므로 없다.

        Args:
            token: 검증할 JWT.

        Returns:
            인증된 `Principal`.

        Raises:
            AuthenticationError: 외부 인증이 구성되지 않았거나 토큰이 유효
                하지 않은 경우.
            PermissionDeniedError: 사용자가 등록되지 않았고 자동 생성도 꺼져
                있는 경우.
        """
        if self._oidc is None:
            # 미구성을 통과로 해석하면 아무 IdP 의 토큰이나 받게 된다.
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않거나 비활성 상태다."
            )
        identity = self._oidc.verify(token)

        account = self._load_account(identity.account_id)
        if account is None or account.status is not domain.EntityStatus.ACTIVE:
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않거나 비활성 상태다."
            )

        team: domain.Team | None = None
        if identity.team_id:
            team = self._load_team(identity.account_id, identity.team_id)
            if team is None or team.status is not domain.EntityStatus.ACTIVE:
                raise errors.PermissionDeniedError(
                    "토큰이 가리키는 팀이 없거나 비활성이다:"
                    f" {identity.team_id}"
                )

        user = self._load_user(identity.account_id, identity.user_id)
        if user is None:
            user = self._provision_user(identity)
        if user.status is not domain.EntityStatus.ACTIVE:
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않거나 비활성 상태다."
            )

        allowed = (
            identity.config.provision_model_list
            or self._settings.default_allowed_model_list
        )
        return domain.Principal(
            account_id=identity.account_id,
            team_id=user.team_id or identity.team_id,
            user_id=identity.user_id,
            # 키가 없는 호출이다. 집계에서 키 축이 비어 있음을 나타낸다.
            key_id="",
            key_hash="",
            allowed_models=allowed,
            account_budget_usd=account.monthly_budget_usd,
            team_budget_usd=team.monthly_budget_usd if team else None,
            user_budget_usd=user.monthly_budget_usd,
            key_budget_usd=None,
            rpm_limit=user.rpm_limit,
            # 키가 없으므로 사용자 단위로 센다.
            rate_scope=f"USER#{identity.user_id}",
        )

    def _provision_user(self, identity: oidc.VerifiedIdentity) -> domain.User:
        """토큰 주체를 사용자로 자동 생성한다.

        자동 생성이 꺼져 있으면 거부한다. 켜져 있으면 설정된 예산을 붙여
        만든다. 예산은 설정 단계에서 필수라 여기서 무제한이 될 수 없다.

        Args:
            identity: 검증된 주체.

        Returns:
            생성된(또는 경합으로 이미 생성돼 있던) 사용자.

        Raises:
            PermissionDeniedError: 자동 생성이 꺼져 있는 경우.
        """
        account_config = identity.config
        if not account_config.auto_provision:
            raise errors.PermissionDeniedError(
                "이 토큰의 사용자가 등록되지 않았다:"
                f" {identity.user_id}. 관리자가 사용자를 만들거나 계정의"
                " 자동 생성을 켜야 한다."
            )
        user = domain.User(
            account_id=identity.account_id,
            user_id=identity.user_id,
            name=identity.display_name,
            team_id=identity.team_id,
            monthly_budget_usd=account_config.provision_budget_usd,
            created_at=clock.to_iso(self._clock.now()),
        )
        try:
            self._registry.put_user(user)
        except errors.ResourceConflictError:
            # 같은 사용자가 동시에 두 번 로그인하면 경합이 난다. 이미 만들어진
            # 것을 읽어 쓰면 되므로 실패로 다루지 않는다.
            existing = self._registry.get_user(
                identity.account_id, identity.user_id
            )
            if existing is None:
                raise
            return existing
        self._cache.invalidate(f"user:{identity.account_id}:{identity.user_id}")
        return user

    def enforce_model(self, principal: domain.Principal, model_id: str) -> None:
        """요청 모델이 허용 목록에 있는지 확인한다.

        허용 목록은 정규화 후 비교한다. `us.` 접두어가 붙은 추론 프로파일과
        기반 모델 ID 를 사람이 구분해 등록해야 하는 부담을 없애기 위해서다.

        Args:
            principal: 인증된 호출 주체.
            model_id: 요청한 모델 ID.

        Raises:
            PermissionDeniedError: 허용 목록에 없는 모델인 경우.
        """
        if not principal.allowed_models:
            return
        requested = pricing.normalize_model_id(model_id)
        permitted = {
            pricing.normalize_model_id(allowed)
            for allowed in principal.allowed_models
        }
        if requested not in permitted:
            raise errors.PermissionDeniedError(
                f"이 키로 호출할 수 없는 모델이다: {model_id}"
            )

    def enforce_rate_limit(
        self, principal: domain.Principal, now: datetime.datetime
    ) -> None:
        """분당 요청 한도를 확인하고 소비한다.

        월 예산은 사후 통제다. 한 달 한도가 남아 있어도 폭주 루프가 몇 분 안에
        큰 비용을 만들 수 있다. 이 검사는 그 앞을 막는다.

        한도가 설정돼 있지 않으면 DynamoDB 를 건드리지 않는다. 한도 미설정이
        기본값이므로 이 경우 요청 경로에 왕복이 늘지 않는다.

        분당 요청만 센다. 분당 **토큰**은 요청 전에 출력 토큰 수를 알 수
        없어서 정확히 예약할 수 없다. `max_tokens` 는 선택 필드라 비어 있는
        요청이 많고, 그 값으로 예약하면 지정한 클라이언트만 불리해진다.

        Args:
            principal: 인증된 호출 주체.
            now: 현재 시각. 분 버킷을 정하는 데 쓴다.

        Raises:
            RateLimitExceededError: 이번 분의 한도를 이미 채운 경우.
        """
        limit = principal.rpm_limit
        if limit is None or not principal.rate_scope:
            return
        minute = now.strftime("%Y-%m-%dT%H:%M")
        allowed = self._usage_store.try_consume_rate_limit(
            account_id=principal.account_id,
            scope=principal.rate_scope,
            minute=minute,
            limit=limit,
            now=now,
        )
        if not allowed:
            # 다음 분 경계까지 남은 초를 알려준다. 클라이언트가 그보다 빨리
            # 재시도하면 다시 거부되어 서로 부하만 늘어난다.
            retry_after = max(1, 60 - now.second)
            raise errors.RateLimitExceededError(
                f"분당 요청 한도를 초과했다: {limit} RPM."
                f" {retry_after}초 뒤에 다시 시도한다.",
                retry_after_seconds=retry_after,
            )

    def enforce_budget(
        self, principal: domain.Principal, now: datetime.datetime
    ) -> None:
        """이번 달 누적 비용이 예산 안에 있는지 확인한다.

        예산이 하나도 설정돼 있지 않으면 DynamoDB 를 조회하지 않는다.
        예산 미설정이 기본값이므로, 이 경우 인증 경로에 추가 왕복이 생기지
        않는다.

        Args:
            principal: 인증된 호출 주체.
            now: 현재 시각. 월 파티션 키를 만드는 데 쓴다.

        Raises:
            BudgetExceededError: 어느 한 축이라도 예산을 초과한 경우.
        """
        checks = self._budget_checks(principal)
        if not checks:
            return

        totals = self._usage_store.get_totals(
            principal.account_id,
            _BUDGET_GRANULARITY,
            clock.month_key(now),
            [check.sort_key for check in checks],
        )
        for check in checks:
            spent = totals.get(check.sort_key, domain.EMPTY_TOTALS).cost_usd
            if spent >= check.limit:
                raise errors.BudgetExceededError(
                    f"{check.scope} 월 예산을 초과했다: "
                    f"{spent} USD / 한도 {check.limit} USD"
                )

    # -- 내부 ---------------------------------------------------------------

    @staticmethod
    def _budget_checks(
        principal: domain.Principal,
    ) -> list[_BudgetCheck]:
        """설정된 예산만 골라 검사 목록을 만든다."""
        candidates: list[tuple[str, decimal.Decimal | None, str]] = [
            (
                "계정",
                principal.account_budget_usd,
                repository.dimension_sk(None, ""),
            ),
            (
                "팀",
                principal.team_budget_usd,
                repository.dimension_sk(
                    domain.BreakdownDimension.TEAM, principal.team_id
                ),
            ),
            (
                "사용자",
                principal.user_budget_usd,
                repository.dimension_sk(
                    domain.BreakdownDimension.USER, principal.user_id
                ),
            ),
            (
                "API 키",
                principal.key_budget_usd,
                repository.dimension_sk(
                    domain.BreakdownDimension.KEY, principal.key_id
                ),
            ),
        ]
        return [
            _BudgetCheck(scope=scope, limit=limit, sort_key=sort_key)
            for scope, limit, sort_key in candidates
            if limit is not None
        ]

    @staticmethod
    def _extract_bearer(authorization: str | None) -> str:
        """`Authorization` 헤더에서 API 키를 꺼낸다."""
        if not authorization:
            raise errors.AuthenticationError(
                "Authorization 헤더가 없다. 'Bearer <api-key>' 형식으로"
                " 보내야 한다."
            )
        if not authorization.lower().startswith(_BEARER_PREFIX):
            raise errors.AuthenticationError(
                "Authorization 헤더는 Bearer 스킴이어야 한다."
            )
        token = authorization[len(_BEARER_PREFIX) :].strip()
        if not token:
            raise errors.AuthenticationError("API 키가 비어 있다.")
        return token

    def _load_account(self, account_id: str) -> domain.Account | None:
        """계정을 캐시를 거쳐 조회한다."""
        return typing.cast(
            "domain.Account | None",
            self._cache.get_or_load(
                f"account:{account_id}",
                lambda: self._registry.get_account(account_id),
            ),
        )

    def _load_team(self, account_id: str, team_id: str) -> domain.Team | None:
        """팀을 캐시를 거쳐 조회한다."""
        return typing.cast(
            "domain.Team | None",
            self._cache.get_or_load(
                f"team:{account_id}:{team_id}",
                lambda: self._registry.get_team(account_id, team_id),
            ),
        )

    def _load_user(self, account_id: str, user_id: str) -> domain.User | None:
        """사용자를 캐시를 거쳐 조회한다."""
        return typing.cast(
            "domain.User | None",
            self._cache.get_or_load(
                f"user:{account_id}:{user_id}",
                lambda: self._registry.get_user(account_id, user_id),
            ),
        )


def verify_admin_token(provided: str | None, expected: str) -> None:
    """관리 API 토큰을 검증한다.

    Args:
        provided: 요청이 제시한 토큰.
        expected: 설정된 기대 토큰.

    Raises:
        AdminNotConfiguredError: 서버에 관리 토큰이 설정되지 않은 경우.
        AuthenticationError: 토큰이 없거나 일치하지 않는 경우.
    """
    if not expected:
        raise errors.AdminNotConfiguredError(
            "관리 토큰이 설정되지 않아 관리 API를 사용할 수 없다."
        )
    if not provided or not apikey.constant_time_equals(provided, expected):
        raise errors.AuthenticationError("관리 토큰이 유효하지 않다.")
