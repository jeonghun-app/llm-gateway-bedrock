"""가드레일 정책 해석.

## 무엇을 결정하는가

요청 하나에 Amazon Bedrock Guardrails 를 붙일지, 붙인다면 어느 것을 붙일지
결정한다. 판정 결과는 `domain.GuardrailDecision` 이다.

## 왜 게이트웨이가 하는가

고객이 콘솔에서 가드레일을 만들어도 그것만으로는 적용되지 않는다. Converse
호출마다 `guardrailConfig` 에 식별자를 실어야 한다. 즉 **애플리케이션이 빼면
통제가 사라진다.** 게이트웨이가 중앙에서 붙이면 클라이언트가 그것을 빼거나
다른 가드레일로 바꿀 수 없다. 그것이 이 기능의 요점이다.

## 계층과 면제

계정 기준선이 기본이고, 팀·사용자는 **면제만** 할 수 있다. 서로 다른 가드레일을
계층별로 고르는 기능은 넣지 않았다. 가드레일 ID 사이에는 강도의 전순서가 없어
"더 엄격한 것이 이긴다" 같은 규칙을 정의할 수 없기 때문이다. 한 정책은
개인정보에 강하고 다른 정책은 폭력 표현에 강할 수 있고, Converse 는 호출당
설정 하나만 받는다.

우선순위는 **사용자 → 팀 → 계정** 이다. 사용자 면제가 팀 면제보다 구체적이다.

## 면제를 안전하게 만드는 것

면제는 통제를 제거하는 일이다. 그래서 관리 API 쪽에서 두 가지를 요구한다.

1. **플랫폼 관리자만 바꿀 수 있다.** 계정 관리자가 자기 계정의 통제를 스스로
   해제하면 통제라고 부를 수 없다. 셀프서비스 경로에는 아예 없다.
2. **사유가 필수다.** 왜 면제했는지 남지 않으면 나중에 검토할 수 없다.

만료 시각과 승인자 기록은 다음 릴리스로 미뤘다. 지금은 사유와 감사 로그까지다.

## 실측으로 확인한 것

`scripts/` 밖에서 sandbox 가드레일을 만들어 확인한 결과를 설계에 반영했다.

- **스트리밍은 `sync` 로 강제한다.** `async` 는 차단 대상 텍스트를 클라이언트에
  먼저 보내고 나중에 개입을 알린다. 실측에서 차단어가 그대로 전달됐다.
- **`trace` 는 끈다.** 켜면 응답에 `modelOutput`(차단하려던 원문)이 들어와,
  그것을 로그에 남기면 막으려던 내용이 로그로 샌다.
- **잘못된 ID·버전·타계정 ARN 은 AWS 가 `ValidationException` 으로 거부한다.**
  조용히 검사를 건너뛰는 경우는 없다. 따라서 게이트웨이가 별도로 fail-closed
  처리를 겹칠 필요가 없고, 가드레일 없이 재시도해서도 안 된다.
- **`DRAFT` 버전도 런타임에서 동작한다.** 그래서 도메인 모델이 숫자 버전만
  받는다. 막지 않으면 조용히 바뀌는 정책을 강제하게 된다.
"""

from __future__ import annotations

from llmgw import cache as cache_module
from llmgw import domain
from llmgw import observability
from llmgw import repository

# 계정 가드레일 설정 캐시 TTL. 인증 메타데이터(30초)보다 짧게 잡는다. 통제를
# 켜거나 끈 변경이 오래 반영되지 않으면, 운영자는 이미 적용됐다고 믿는데
# 실제로는 아직 아닌 창이 생긴다.
_CACHE_TTL_SECONDS = 10.0


class GuardrailResolver:
    """주체별 유효 가드레일 정책을 판정한다."""

    def __init__(
        self,
        *,
        registry: repository.RegistryRepository,
        logger: observability.Logger,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        """해석기를 만든다.

        Args:
            registry: 레지스트리 저장소.
            logger: 구조화 로거.
            cache_ttl_seconds: 계정 설정 캐시 수명.
        """
        self._registry = registry
        self._logger = logger
        self._cache: cache_module.TtlCache[
            domain.AccountGuardrailConfig | None
        ] = cache_module.TtlCache(ttl_seconds=cache_ttl_seconds)

    def invalidate(self, account_id: str) -> None:
        """계정 설정 캐시를 버린다.

        관리 API 가 설정을 바꾼 직후 호출한다. 여러 태스크가 도는 배포에서는
        이 프로세스만 즉시 반영되고 나머지는 TTL 만큼 늦는다. 그래서 TTL 을
        짧게 잡았다.

        Args:
            account_id: 계정 ID.
        """
        self._cache.invalidate(account_id)

    def resolve(self, principal: domain.Principal) -> domain.GuardrailDecision:
        """이 요청에 적용할 가드레일을 판정한다.

        Args:
            principal: 인증된 요청 주체.

        Returns:
            적용할 가드레일과 면제 여부. 계정에 기준선이 없거나 꺼져 있으면
            비어 있는 판정을 반환한다.
        """
        config = self._config(principal.account_id)
        if config is None or not config.enabled:
            return domain.GuardrailDecision()

        # 사용자 면제가 팀 면제보다 구체적이므로 먼저 본다.
        exempt_scope = self._exempt_scope(principal)
        if exempt_scope:
            # 면제는 통제를 건너뛰는 일이므로 매 요청 남긴다. 사유는 관리
            # API 가 저장하고, 여기서는 어느 계층에서 면제됐는지만 남긴다.
            self._logger.info(
                "가드레일을 면제했다",
                extra={
                    "account_id": principal.account_id,
                    "user_id": principal.user_id,
                    "exempt_scope": exempt_scope,
                },
            )
            return domain.GuardrailDecision(exempt_scope=exempt_scope)

        return domain.GuardrailDecision(
            guardrail_id=config.guardrail_id,
            guardrail_version=config.guardrail_version,
        )

    def _config(self, account_id: str) -> domain.AccountGuardrailConfig | None:
        """계정 기준선을 캐시를 거쳐 조회한다."""
        return self._cache.get_or_load(
            account_id,
            lambda: self._registry.get_guardrail_config(account_id),
        )

    def _exempt_scope(self, principal: domain.Principal) -> str:
        """면제된 계층 이름을 반환한다. 면제가 아니면 빈 문자열."""
        user = self._registry.get_user(principal.account_id, principal.user_id)
        if user is not None and user.guardrail_exempt:
            return "user"
        if principal.team_id:
            team = self._registry.get_team(
                principal.account_id, principal.team_id
            )
            if team is not None and team.guardrail_exempt:
                return "team"
        return ""
