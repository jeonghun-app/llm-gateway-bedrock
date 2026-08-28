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


def test_rotate_api_key_해시를교체하고아이템하나만남긴다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    old = _api_key("key-1")
    registry.put_api_key(old)
    new_generated = apikey.generate_api_key("test")
    rotated = old.model_copy(
        update={
            "key_hash": new_generated.key_hash,
            "key_prefix": new_generated.key_prefix,
        }
    )

    # Act
    registry.rotate_api_key(old.key_hash, rotated)

    # Assert: 옛 해시는 사라지고 새 해시로만 조회되며 목록에 하나뿐이다.
    assert registry.get_api_key_by_hash(old.key_hash) is None
    assert registry.get_api_key_by_hash(new_generated.key_hash) is not None
    remaining = registry.list_api_keys("acme")
    assert len(remaining) == 1
    assert remaining[0].key_id == "key-1"


def test_rotate_api_key_옛키가없으면롤백되어새키도안생긴다(
    registry: repository.RegistryRepository,
) -> None:
    """Delete 조건이 실패하면 트랜잭션 전체가 취소되어 새 키도 생기지 않는다."""
    # Arrange: 옛 키를 저장하지 않는다(이미 삭제된 상황).
    old = _api_key("key-1")
    new_generated = apikey.generate_api_key("test")
    rotated = old.model_copy(
        update={
            "key_hash": new_generated.key_hash,
            "key_prefix": new_generated.key_prefix,
        }
    )

    # Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.rotate_api_key(old.key_hash, rotated)
    # 롤백되어 새 해시 아이템이 만들어지지 않아야 한다.
    assert registry.get_api_key_by_hash(new_generated.key_hash) is None
    assert registry.list_api_keys("acme") == []


def test_rotate_api_key_멱등성토큰을트랜잭션에전달한다() -> None:
    """`ClientRequestToken` 을 TransactWriteItems 로 넘기는지 확인한다.

    이 토큰은 boto3 의 SDK 내부 재시도만 보호한다(HTTP `/rotate` 재호출까지
    멱등하게 만들지는 않는다). 실제 보호는 DynamoDB 몫이고 moto 는 이를
    구현하지 않으므로, 여기서는 우리 코드가 토큰을 트랜잭션 호출에 실어
    보내는지만 대역 클라이언트로 검증한다.
    """

    # Arrange: transact_write_items 호출 인자를 붙잡는 대역 클라이언트.
    class _CapturingClient:
        def __init__(self) -> None:
            self.captured: dict[str, object] = {}

        def transact_write_items(self, **kwargs: object) -> None:
            self.captured = kwargs

    client = _CapturingClient()

    class _NamedTable:
        name = "registry"

    repo = repository.RegistryRepository(_NamedTable(), client=client)
    key = _api_key("key-1")

    # Act
    repo.rotate_api_key(
        "old-hash", key, client_request_token="rotate-token-0001"
    )

    # Assert
    assert client.captured["ClientRequestToken"] == "rotate-token-0001"
    assert "TransactItems" in client.captured


def test_rotate_api_key_긴토큰은생략한다() -> None:
    """36자를 넘는 토큰은 DynamoDB 규격 위반이라 실어 보내지 않는다."""

    # Arrange
    class _CapturingClient:
        def __init__(self) -> None:
            self.captured: dict[str, object] = {}

        def transact_write_items(self, **kwargs: object) -> None:
            self.captured = kwargs

    client = _CapturingClient()

    class _NamedTable:
        name = "registry"

    repo = repository.RegistryRepository(_NamedTable(), client=client)
    key = _api_key("key-1")

    # Act
    repo.rotate_api_key("old-hash", key, client_request_token="x" * 37)

    # Assert
    assert "ClientRequestToken" not in client.captured


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
