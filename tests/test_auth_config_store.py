"""계정별 외부 인증(OIDC) 설정 저장소 테스트.

가장 중요한 검증은 발급자 소유권이다. 발급자(`iss`)로 토큰이 어느 계정
것인지 판별하므로, 다른 계정이 이미 쓰는 발급자를 가로챌 수 있으면 그
계정의 사용자로 위장할 수 있다.
"""

from __future__ import annotations

import decimal

import pydantic
import pytest

from llmgw import domain
from llmgw import errors
from llmgw import repository

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123"


def _config(
    *,
    account_id: str = "acme",
    issuer: str = _ISSUER,
    **overrides: object,
) -> domain.AccountAuthConfig:
    """테스트용 인증 설정을 만든다."""
    values: dict[str, object] = {
        "account_id": account_id,
        "issuer": issuer,
        "audience": "client-abc",
        "admin_groups": "llmgw-admins",
        "created_at": "2026-08-29T00:00:00Z",
    }
    values.update(overrides)
    return domain.AccountAuthConfig(**values)  # type: ignore[arg-type]


def test_put_and_get_설정을저장하고읽는다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    config = _config(
        auto_provision=True,
        provision_allowed_models="amazon.nova-lite-v1:0",
        provision_budget_usd=decimal.Decimal("25"),
    )

    # Act
    registry.put_auth_config(config)
    loaded = registry.get_auth_config("acme")

    # Assert
    assert loaded is not None
    assert loaded.issuer == _ISSUER
    assert loaded.audience_list == ("client-abc",)
    assert loaded.admin_group_list == ("llmgw-admins",)
    assert loaded.auto_provision is True
    assert loaded.provision_model_list == ("amazon.nova-lite-v1:0",)
    assert loaded.provision_budget_usd == decimal.Decimal("25")
    assert loaded.status is domain.EntityStatus.ACTIVE


def test_get_설정이없으면None(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    assert registry.get_auth_config("acme") is None


def test_find_account_by_issuer_발급자로계정을찾는다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_auth_config(_config())

    # Act / Assert
    assert registry.find_account_by_issuer(_ISSUER) == "acme"


def test_find_account_by_issuer_등록되지않은발급자는빈문자열(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    assert registry.find_account_by_issuer("https://evil.example") == ""
    assert registry.find_account_by_issuer("") == ""


def test_put_다른계정이쓰는발급자는가로챌수없다(
    registry: repository.RegistryRepository,
) -> None:
    """발급자를 가로채면 그 계정의 사용자로 위장할 수 있다."""
    # Arrange
    registry.put_auth_config(_config(account_id="acme"))

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.put_auth_config(_config(account_id="beta"))

    # 역인덱스가 원래 계정을 그대로 가리켜야 한다.
    assert registry.find_account_by_issuer(_ISSUER) == "acme"


def test_put_같은계정_중복생성은409(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_auth_config(_config())

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.put_auth_config(_config())


def test_put_overwrite_같은계정은갱신된다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_auth_config(_config())

    # Act
    registry.put_auth_config(
        _config(
            auto_provision=True,
            provision_budget_usd=decimal.Decimal("10"),
            updated_at="2026-08-30T00:00:00Z",
        ),
        overwrite=True,
    )
    loaded = registry.get_auth_config("acme")

    # Assert
    assert loaded is not None
    assert loaded.auto_provision is True
    assert loaded.updated_at == "2026-08-30T00:00:00Z"


def test_put_overwrite_설정이없으면404(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.put_auth_config(_config(), overwrite=True)


def test_delete_설정과역인덱스가함께사라진다(
    registry: repository.RegistryRepository,
) -> None:
    """역인덱스만 남으면 토큰이 계정으로 라우팅됐는데 설정이 없는 경로가
    생긴다."""
    # Arrange
    registry.put_auth_config(_config())

    # Act
    registry.delete_auth_config("acme")

    # Assert
    assert registry.get_auth_config("acme") is None
    assert registry.find_account_by_issuer(_ISSUER) == ""


def test_delete_설정이없으면404(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.delete_auth_config("acme")


def test_delete_후_다른계정이같은발급자를쓸수있다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_auth_config(_config(account_id="acme"))
    registry.delete_auth_config("acme")

    # Act
    registry.put_auth_config(_config(account_id="beta"))

    # Assert
    assert registry.find_account_by_issuer(_ISSUER) == "beta"


def test_계정삭제_인증설정이남아있으면거부한다(
    registry: repository.RegistryRepository,
) -> None:
    """인증 설정을 남긴 채 계정을 지우면 발급자 역인덱스가 고아로 남아
    다른 계정이 그 발급자를 영구히 쓸 수 없게 된다."""
    # Arrange
    registry.put_account(
        domain.Account(account_id="acme", name="Acme", created_at="2026-08-29")
    )
    registry.put_auth_config(_config(account_id="acme"))

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError, match="인증 설정"):
        registry.delete_account("acme")

    # 인증 설정을 먼저 지우면 계정 삭제가 통과해야 한다.
    registry.delete_auth_config("acme")
    registry.delete_account("acme")
    assert registry.get_account("acme") is None
    assert registry.find_account_by_issuer(_ISSUER) == ""


def test_auto_provision_예산없이는설정할수없다() -> None:
    """예산 없이 자동 생성을 켜면 IdP 계정만 있는 사람이 무제한으로
    Bedrock 을 호출할 수 있다."""
    # Arrange / Act / Assert
    with pytest.raises(pydantic.ValidationError, match="provision_budget_usd"):
        domain.AccountAuthConfig(
            account_id="acme", issuer=_ISSUER, auto_provision=True
        )

    # 예산을 주면 통과한다.
    config = domain.AccountAuthConfig(
        account_id="acme",
        issuer=_ISSUER,
        auto_provision=True,
        provision_budget_usd=decimal.Decimal("10"),
    )
    assert config.provision_budget_usd == decimal.Decimal("10")


def test_auto_provision_꺼져있으면예산은선택이다() -> None:
    # Arrange / Act
    config = domain.AccountAuthConfig(account_id="acme", issuer=_ISSUER)

    # Assert
    assert config.auto_provision is False
    assert config.provision_budget_usd is None
