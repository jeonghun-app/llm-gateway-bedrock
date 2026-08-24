"""인증과 정책 검사 테스트."""

from __future__ import annotations

import datetime
import decimal

import pytest

import conftest
from llmgw import apikey
from llmgw import auth
from llmgw import domain
from llmgw import errors
from llmgw import repository


def _seed(
    registry: repository.RegistryRepository,
    *,
    account_budget: decimal.Decimal | None = None,
    team_budget: decimal.Decimal | None = None,
    user_budget: decimal.Decimal | None = None,
    key_budget: decimal.Decimal | None = None,
    account_status: domain.EntityStatus = domain.EntityStatus.ACTIVE,
    team_status: domain.EntityStatus = domain.EntityStatus.ACTIVE,
    user_status: domain.EntityStatus = domain.EntityStatus.ACTIVE,
    key_status: domain.EntityStatus = domain.EntityStatus.ACTIVE,
    allowed_models: tuple[str, ...] = (),
    team_id: str = "platform",
) -> str:
    """계정·팀·사용자·키를 심고 평문 키를 반환한다."""
    generated = apikey.generate_api_key("test")
    registry.put_account(
        domain.Account(
            account_id="acme",
            name="Acme",
            monthly_budget_usd=account_budget,
            status=account_status,
        )
    )
    if team_id:
        registry.put_team(
            domain.Team(
                account_id="acme",
                team_id=team_id,
                name="플랫폼",
                monthly_budget_usd=team_budget,
                status=team_status,
            )
        )
    registry.put_user(
        domain.User(
            account_id="acme",
            user_id="alice",
            name="앨리스",
            team_id=team_id,
            monthly_budget_usd=user_budget,
            status=user_status,
        )
    )
    registry.put_api_key(
        domain.ApiKey(
            key_id="key-1",
            key_hash=generated.key_hash,
            key_prefix=generated.key_prefix,
            account_id="acme",
            team_id=team_id,
            user_id="alice",
            allowed_models=allowed_models,
            monthly_budget_usd=key_budget,
            status=key_status,
        )
    )
    return generated.plaintext


# -- authenticate -----------------------------------------------------------


def test_authenticate_정상키_Principal을반환한다(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry)

    # Act
    principal = authenticator.authenticate(f"Bearer {plaintext}")

    # Assert
    assert principal.account_id == "acme"
    assert principal.team_id == "platform"
    assert principal.user_id == "alice"
    assert principal.key_id == "key-1"


def test_authenticate_Bearer대소문자를구분하지않는다(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry)

    # Act
    principal = authenticator.authenticate(f"bearer {plaintext}")

    # Assert
    assert principal.key_id == "key-1"


def test_authenticate_헤더없음_AuthenticationError(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError, match="Authorization"):
        authenticator.authenticate(None)


def test_authenticate_Bearer스킴아님_AuthenticationError(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError, match="Bearer"):
        authenticator.authenticate("Basic dXNlcjpwYXNz")


def test_authenticate_빈토큰_AuthenticationError(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError, match="비어"):
        authenticator.authenticate("Bearer    ")


def test_authenticate_없는키_AuthenticationError(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError, match="유효하지"):
        authenticator.authenticate("Bearer sk-llmgw-test-nonexistent")


def test_authenticate_비활성키_AuthenticationError(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry, key_status=domain.EntityStatus.DISABLED)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError, match="유효하지"):
        authenticator.authenticate(f"Bearer {plaintext}")


def test_authenticate_비활성계정_AuthenticationError(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry, account_status=domain.EntityStatus.DISABLED)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError, match="계정"):
        authenticator.authenticate(f"Bearer {plaintext}")


def test_authenticate_비활성팀_AuthenticationError(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry, team_status=domain.EntityStatus.DISABLED)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError, match="팀"):
        authenticator.authenticate(f"Bearer {plaintext}")


def test_authenticate_비활성사용자_AuthenticationError(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(registry, user_status=domain.EntityStatus.DISABLED)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError, match="사용자"):
        authenticator.authenticate(f"Bearer {plaintext}")


def test_authenticate_계정이삭제된키_AuthenticationError(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    """키만 남고 계정이 없어진 상태에서 통과시키면 안 된다."""
    # Arrange
    generated = apikey.generate_api_key("test")
    registry.put_api_key(
        domain.ApiKey(
            key_id="orphan",
            key_hash=generated.key_hash,
            key_prefix=generated.key_prefix,
            account_id="ghost",
            user_id="nobody",
        )
    )

    # Act / Assert
    with pytest.raises(errors.AuthenticationError, match="계정"):
        authenticator.authenticate(f"Bearer {generated.plaintext}")


def test_authenticate_예산을Principal에담는다(
    authenticator: auth.Authenticator,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = _seed(
        registry,
        account_budget=decimal.Decimal("100"),
        team_budget=decimal.Decimal("50"),
        user_budget=decimal.Decimal("10"),
        key_budget=decimal.Decimal("5"),
    )

    # Act
    principal = authenticator.authenticate(f"Bearer {plaintext}")

    # Assert
    assert principal.account_budget_usd == decimal.Decimal("100")
    assert principal.team_budget_usd == decimal.Decimal("50")
    assert principal.user_budget_usd == decimal.Decimal("10")
    assert principal.key_budget_usd == decimal.Decimal("5")


def test_authenticate_기본허용모델설정이키에적용된다(
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    from llmgw import cache
    from llmgw import config

    settings = config.Settings(
        env="test",
        registry_table=conftest.REGISTRY_TABLE,
        usage_table=conftest.USAGE_TABLE,
        usage_agg_table=conftest.USAGE_AGG_TABLE,
        default_allowed_models="amazon.nova-lite-v1:0, amazon.nova-pro-v1:0",
    )
    subject = auth.Authenticator(
        registry=registry,
        usage_store=usage_store,
        settings=settings,
        metadata_cache=cache.TtlCache(ttl_seconds=0),
    )
    plaintext = _seed(registry, allowed_models=())

    # Act
    principal = subject.authenticate(f"Bearer {plaintext}")

    # Assert
    assert principal.allowed_models == (
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
    )


# -- enforce_model ----------------------------------------------------------


def test_enforce_model_허용목록이비면모두허용한다(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange
    principal = domain.Principal(
        account_id="acme", user_id="alice", key_id="key-1"
    )

    # Act / Assert (예외가 없으면 통과)
    authenticator.enforce_model(principal, "anything.at.all")


def test_enforce_model_허용목록에있으면통과한다(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange
    principal = domain.Principal(
        account_id="acme",
        user_id="alice",
        key_id="key-1",
        allowed_models=("amazon.nova-lite-v1:0",),
    )

    # Act / Assert
    authenticator.enforce_model(principal, "amazon.nova-lite-v1:0")


def test_enforce_model_허용목록에없으면PermissionDeniedError(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange
    principal = domain.Principal(
        account_id="acme",
        user_id="alice",
        key_id="key-1",
        allowed_models=("amazon.nova-lite-v1:0",),
    )

    # Act / Assert
    with pytest.raises(errors.PermissionDeniedError, match="호출할 수 없는"):
        authenticator.enforce_model(principal, "amazon.nova-pro-v1:0")


def test_enforce_model_추론프로파일접두어를무시하고비교한다(
    authenticator: auth.Authenticator,
) -> None:
    """기반 모델로 등록해도 us. 프로파일 호출이 통과해야 한다."""
    # Arrange
    principal = domain.Principal(
        account_id="acme",
        user_id="alice",
        key_id="key-1",
        allowed_models=("anthropic.claude-3-haiku-20240307-v1:0",),
    )

    # Act / Assert
    authenticator.enforce_model(
        principal, "us.anthropic.claude-3-haiku-20240307-v1:0"
    )


# -- enforce_budget ---------------------------------------------------------


def test_enforce_budget_예산미설정시조회없이통과한다(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange
    principal = domain.Principal(
        account_id="acme", user_id="alice", key_id="key-1"
    )

    # Act / Assert
    authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_한도미달이면통과한다(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="1.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        account_budget_usd=decimal.Decimal("10"),
    )

    # Act / Assert
    authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_계정한도초과시BudgetExceededError(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="10.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        account_budget_usd=decimal.Decimal("5"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError, match="계정"):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_정확히한도와같으면차단한다(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    """경계값. 한도에 도달한 시점부터 막아 초과 지출을 만들지 않는다."""
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="5.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        account_budget_usd=decimal.Decimal("5"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_팀한도초과시BudgetExceededError(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="3.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        team_budget_usd=decimal.Decimal("2"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError, match="팀"):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_사용자한도초과시BudgetExceededError(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="3.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        user_budget_usd=decimal.Decimal("1"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError, match="사용자"):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_키한도초과시BudgetExceededError(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    usage_store.record(
        conftest.make_usage_record(request_id="r1", cost_usd="3.0")
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        key_budget_usd=decimal.Decimal("1"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError, match="API 키"):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_예산0이면첫요청부터차단한다(
    authenticator: auth.Authenticator,
) -> None:
    # Arrange
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        account_budget_usd=decimal.Decimal("0"),
    )

    # Act / Assert
    with pytest.raises(errors.BudgetExceededError):
        authenticator.enforce_budget(principal, conftest.FIXED_NOW)


def test_enforce_budget_다음달로넘어가면다시통과한다(
    authenticator: auth.Authenticator,
    usage_store: repository.UsageStore,
) -> None:
    """월 예산은 달이 바뀌면 초기화된다."""
    # Arrange
    usage_store.record(
        conftest.make_usage_record(
            request_id="r1",
            cost_usd="10.0",
            timestamp="2026-08-23T12:00:00Z",
        )
    )
    principal = domain.Principal(
        account_id="acme",
        team_id="platform",
        user_id="alice",
        key_id="key-1",
        account_budget_usd=decimal.Decimal("5"),
    )
    next_month = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.UTC)

    # Act / Assert
    authenticator.enforce_budget(principal, next_month)


# -- 관리 토큰 --------------------------------------------------------------


def test_verify_admin_token_일치하면통과한다() -> None:
    # Arrange / Act / Assert
    auth.verify_admin_token("secret", "secret")


def test_verify_admin_token_불일치_AuthenticationError() -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError):
        auth.verify_admin_token("wrong", "secret")


def test_verify_admin_token_토큰없음_AuthenticationError() -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.AuthenticationError):
        auth.verify_admin_token(None, "secret")


def test_verify_admin_token_서버설정없음_AdminNotConfiguredError() -> None:
    """토큰 미설정 상태에서 통과시키면 관리 API가 무인증으로 열린다."""
    # Arrange / Act / Assert
    with pytest.raises(errors.AdminNotConfiguredError):
        auth.verify_admin_token("anything", "")
