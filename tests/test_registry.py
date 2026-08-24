"""레지스트리 저장소 테스트."""

from __future__ import annotations

import decimal

import pytest

from llmgw import apikey
from llmgw import domain
from llmgw import errors
from llmgw import repository


def _account(account_id: str = "acme", **overrides: object) -> domain.Account:
    """테스트용 계정을 만든다."""
    payload: dict[str, object] = {
        "account_id": account_id,
        "name": f"{account_id} Inc.",
        "created_at": "2026-08-01T00:00:00Z",
    }
    payload.update(overrides)
    return domain.Account.model_validate(payload)


def _api_key(key_id: str = "key-1", **overrides: object) -> domain.ApiKey:
    """테스트용 API 키를 만든다."""
    generated = apikey.generate_api_key("test")
    payload: dict[str, object] = {
        "key_id": key_id,
        "key_hash": generated.key_hash,
        "key_prefix": generated.key_prefix,
        "account_id": "acme",
        "team_id": "platform",
        "user_id": "alice",
        "name": "테스트 키",
        "created_at": "2026-08-01T00:00:00Z",
    }
    payload.update(overrides)
    return domain.ApiKey.model_validate(payload)


# -- 계정 -------------------------------------------------------------------


def test_put_get_account_왕복한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    account = _account(monthly_budget_usd=decimal.Decimal("100.50"))

    # Act
    registry.put_account(account)
    loaded = registry.get_account("acme")

    # Assert
    assert loaded is not None
    assert loaded.account_id == "acme"
    assert loaded.name == "acme Inc."
    assert loaded.monthly_budget_usd == decimal.Decimal("100.50")
    assert loaded.status is domain.EntityStatus.ACTIVE


def test_get_account_없으면None(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    assert registry.get_account("nope") is None


def test_put_account_중복생성_ResourceConflictError(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account())

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.put_account(_account())


def test_put_account_overwrite면덮어쓴다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account())

    # Act
    registry.put_account(
        _account(status=domain.EntityStatus.DISABLED), overwrite=True
    )

    # Assert
    loaded = registry.get_account("acme")
    assert loaded is not None
    assert loaded.status is domain.EntityStatus.DISABLED


def test_list_accounts_전체를정렬해반환한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    for account_id in ("zeta", "acme", "middle"):
        registry.put_account(_account(account_id))

    # Act
    accounts = registry.list_accounts()

    # Assert
    assert [item.account_id for item in accounts] == [
        "acme",
        "middle",
        "zeta",
    ]


def test_list_accounts_없으면빈리스트(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    assert registry.list_accounts() == []


def test_account_예산없음은None으로왕복한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account(monthly_budget_usd=None))

    # Act
    loaded = registry.get_account("acme")

    # Assert
    assert loaded is not None
    assert loaded.monthly_budget_usd is None


def test_account_예산0도유효한값이다(
    registry: repository.RegistryRepository,
) -> None:
    """예산 0은 '무제한'이 아니라 '전면 차단'을 뜻한다."""
    # Arrange
    registry.put_account(_account(monthly_budget_usd=decimal.Decimal("0")))

    # Act
    loaded = registry.get_account("acme")

    # Assert
    assert loaded is not None
    assert loaded.monthly_budget_usd == decimal.Decimal("0")


# -- 팀 / 사용자 ------------------------------------------------------------


def test_put_get_team_왕복한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    team = domain.Team(account_id="acme", team_id="platform", name="플랫폼팀")

    # Act
    registry.put_team(team)
    loaded = registry.get_team("acme", "platform")

    # Assert
    assert loaded is not None
    assert loaded.name == "플랫폼팀"


def test_list_teams_계정범위로격리된다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_team(
        domain.Team(account_id="acme", team_id="platform", name="A")
    )
    registry.put_team(
        domain.Team(account_id="other", team_id="platform", name="B")
    )

    # Act
    teams = registry.list_teams("acme")

    # Assert
    assert len(teams) == 1
    assert teams[0].name == "A"


def test_put_get_user_팀소속을보존한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    user = domain.User(
        account_id="acme",
        user_id="alice",
        name="앨리스",
        email="alice@example.com",
        team_id="platform",
    )

    # Act
    registry.put_user(user)
    loaded = registry.get_user("acme", "alice")

    # Assert
    assert loaded is not None
    assert loaded.team_id == "platform"
    assert loaded.email == "alice@example.com"


def test_list_users_사용자ID순으로반환한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    for user_id in ("carol", "alice", "bob"):
        registry.put_user(
            domain.User(
                account_id="acme", user_id=user_id, name=user_id.title()
            )
        )

    # Act
    users = registry.list_users("acme")

    # Assert
    assert [item.user_id for item in users] == ["alice", "bob", "carol"]


def test_put_team_중복생성_ResourceConflictError(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    team = domain.Team(account_id="acme", team_id="platform", name="A")
    registry.put_team(team)

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.put_team(team)


# -- API 키 -----------------------------------------------------------------


def test_put_get_api_key_해시로조회한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    api_key = _api_key(allowed_models=("amazon.nova-lite-v1:0",))

    # Act
    registry.put_api_key(api_key)
    loaded = registry.get_api_key_by_hash(api_key.key_hash)

    # Assert
    assert loaded is not None
    assert loaded.key_id == "key-1"
    assert loaded.allowed_models == ("amazon.nova-lite-v1:0",)
    assert loaded.user_id == "alice"


def test_get_api_key_by_hash_없으면None(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    assert registry.get_api_key_by_hash("0" * 64) is None


def test_list_api_keys_계정별로반환한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_api_key(_api_key("key-1"))
    registry.put_api_key(_api_key("key-2"))
    registry.put_api_key(_api_key("key-9", account_id="other"))

    # Act
    keys = registry.list_api_keys("acme")

    # Assert
    assert sorted(item.key_id for item in keys) == ["key-1", "key-2"]


def test_delete_api_key_삭제후조회되지않는다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    api_key = _api_key()
    registry.put_api_key(api_key)

    # Act
    registry.delete_api_key("acme", "key-1")

    # Assert
    assert registry.get_api_key_by_hash(api_key.key_hash) is None
    assert registry.list_api_keys("acme") == []


def test_delete_api_key_없는키_ResourceNotFoundError(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.delete_api_key("acme", "nope")


def test_delete_api_key_다른계정의키는삭제하지못한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    api_key = _api_key("key-1", account_id="acme")
    registry.put_api_key(api_key)

    # Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.delete_api_key("attacker", "key-1")
    assert registry.get_api_key_by_hash(api_key.key_hash) is not None


def test_touch_api_key_마지막사용시각을갱신한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    api_key = _api_key()
    registry.put_api_key(api_key)

    # Act
    registry.touch_api_key(api_key.key_hash, "2026-08-23T12:00:00Z")

    # Assert
    loaded = registry.get_api_key_by_hash(api_key.key_hash)
    assert loaded is not None
    assert loaded.last_used_at == "2026-08-23T12:00:00Z"


def test_touch_api_key_삭제된키는조건실패한다(
    registry: repository.RegistryRepository,
) -> None:
    """존재하지 않는 키를 되살려 만들지 않아야 한다."""
    # Arrange
    import botocore.exceptions

    # Act / Assert
    with pytest.raises(botocore.exceptions.ClientError) as caught:
        registry.touch_api_key("f" * 64, "2026-08-23T12:00:00Z")
    assert (
        caught.value.response["Error"]["Code"]
        == "ConditionalCheckFailedException"
    )
    assert registry.get_api_key_by_hash("f" * 64) is None


def test_api_key_평문은저장되지않는다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    generated = apikey.generate_api_key("test")
    api_key = _api_key(
        key_hash=generated.key_hash, key_prefix=generated.key_prefix
    )
    registry.put_api_key(api_key)

    # Act
    loaded = registry.get_api_key_by_hash(generated.key_hash)

    # Assert
    assert loaded is not None
    serialized = loaded.model_dump_json()
    assert (
        generated.plaintext not in serialized
    ), "평문 키가 저장소에 남아 있으면 안 된다"
