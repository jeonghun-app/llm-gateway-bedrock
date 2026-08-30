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


def test_put_account_overwrite_삭제된계정을되살리지않는다(
    registry: repository.RegistryRepository,
) -> None:
    """조회와 갱신 사이에 삭제된 계정을 PutItem 으로 재생성하면 안 된다."""
    # Arrange: 라우터의 선행 조회 뒤 다른 요청이 삭제한 경쟁을 모사한다.
    account = _account()
    registry.put_account(account)
    registry._table.delete_item(  # noqa: SLF001 - 경쟁 상황 모사
        Key={"pk": repository.account_pk("acme"), "sk": "META"}
    )

    # Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.put_account(
            account.model_copy(update={"name": "되살아나면 안 됨"}),
            overwrite=True,
        )
    assert registry.get_account("acme") is None


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


def test_delete_account_빈계정을삭제한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account("acme"))

    # Act
    registry.delete_account("acme")

    # Assert
    assert registry.get_account("acme") is None


def test_delete_account_하위팀이있으면거부한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account("acme"))
    registry.put_team(
        domain.Team(account_id="acme", team_id="platform", name="플랫폼")
    )

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.delete_account("acme")
    assert registry.get_account("acme") is not None


def test_delete_account_이미삭제된계정은404(
    registry: repository.RegistryRepository,
) -> None:
    """조회-후-삭제 사이에 다른 요청이 지운 경우를 모사한다.

    조건부 삭제라, 이미 사라진 항목의 삭제는 조용히 성공하지 않고 404 로
    드러난다. 두 번째 삭제 호출이 첫 번째와 무관하게 성공한 것처럼 보이면
    호출자가 상태를 오해한다.
    """
    # Arrange: 검사만 통과하도록 계정을 만든 뒤, 밑단에서 직접 지운다.
    registry.put_account(_account("acme"))
    # 참조 검사(list_*)는 통과하지만 실제 삭제 직전 항목이 없는 상황을
    # 만들기 위해, 저수준으로 먼저 제거한다.
    registry._table.delete_item(  # noqa: SLF001 - 경쟁 상황 모사
        Key={"pk": repository.account_pk("acme"), "sk": "META"}
    )

    # Act / Assert
    with pytest.raises(errors.ResourceNotFoundError):
        registry.delete_account("acme")


def test_delete_team_소속사용자가있으면거부한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account("acme"))
    registry.put_team(
        domain.Team(account_id="acme", team_id="platform", name="플랫폼")
    )
    registry.put_user(
        domain.User(
            account_id="acme",
            user_id="alice",
            name="앨리스",
            team_id="platform",
        )
    )

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.delete_team("acme", "platform")


def test_delete_user_소유키가있으면거부한다(
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    registry.put_account(_account("acme"))
    registry.put_user(
        domain.User(account_id="acme", user_id="alice", name="앨리스")
    )
    registry.put_api_key(_api_key("key-1", user_id="alice", team_id=""))

    # Act / Assert
    with pytest.raises(errors.ResourceConflictError):
        registry.delete_user("acme", "alice")


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


def test_rotate_api_key_호출마다새멱등성토큰을쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 호출끼리 토큰이 겹치지 않고 SDK 내부 재시도만 보호한다.

    클라이언트가 같은 X-Request-Id 로 HTTP 요청을 다시 보내더라도 새 키의
    내용은 달라진다. 상관 ID 를 토큰으로 재사용하면 DynamoDB 가
    IdempotentParameterMismatch 를 내므로, 저장소 호출마다 별도 토큰을
    만들어야 한다.
    """

    # Arrange: transact_write_items 호출 인자를 모두 붙잡는 대역 클라이언트.
    class _CapturingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def transact_write_items(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    client = _CapturingClient()
    tokens = iter(
        [
            type("_Token", (), {"hex": "a" * 32})(),
            type("_Token", (), {"hex": "b" * 32})(),
        ]
    )
    monkeypatch.setattr(repository.uuid, "uuid4", lambda: next(tokens))

    class _NamedTable:
        name = "registry"

    repo = repository.RegistryRepository(_NamedTable(), client=client)
    key = _api_key("key-1")

    # Act: 서로 다른 HTTP 처리에 해당하는 저장소 호출을 두 번 한다.
    repo.rotate_api_key("old-hash", key)
    repo.rotate_api_key("old-hash", key)

    # Assert
    assert [call["ClientRequestToken"] for call in client.calls] == [
        "a" * 32,
        "b" * 32,
    ]
    assert all("TransactItems" in call for call in client.calls)


def test_delete_api_key_GSI조회후이미삭제됐으면404(
    registry: repository.RegistryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GSI 조회와 삭제 사이에 다른 요청이 키를 지운 경쟁을 모사한다."""
    # Arrange: 키가 GSI 에서 보였지만 실제 아이템은 이미 사라진 상태다.
    api_key = _api_key()
    registry.put_api_key(api_key)
    stale_item = {"pk": repository.key_pk(api_key.key_hash)}
    registry._table.delete_item(  # noqa: SLF001 - 경쟁 상황 모사
        Key={"pk": stale_item["pk"], "sk": "META"}
    )
    monkeypatch.setattr(
        registry, "_query_index", lambda **_kwargs: [stale_item]
    )

    # Act / Assert: 조건부 삭제가 두 번째 성공을 204 로 숨기지 않는다.
    with pytest.raises(errors.ResourceNotFoundError):
        registry.delete_api_key("acme", "key-1")


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


# ---------------------------------------------------------------------------
# 직렬화 왕복 — 필드 유실 재발 방지
#
# 저장 경로가 세 곳(put/update/rotate)이라 직렬화를 각자 나열하던 때
# 필드가 유실되는 일이 두 번 있었다. 1.7.0 에서 update 가 rpm_limit 과
# expires_at 을 저장하지 않았고, 그것을 고친 뒤에도 rotate 는 그대로 남아
# 키를 회전하면 두 값이 사라졌다. 만료된 키를 회전하면 무기한 유효해지는
# 통제 우회였다.
#
# 아래 테스트는 특정 필드 이름을 나열하지 않는다. 도메인 모델에 새 필드가
# 생기면 자동으로 검사 대상이 되어야 재발을 막을 수 있다.
# ---------------------------------------------------------------------------


def _모든필드를채운키(key_hash: str = "hash-full") -> domain.ApiKey:
    """선택 필드까지 모두 채운 API 키를 만든다."""
    return domain.ApiKey(
        key_id="key-full",
        key_hash=key_hash,
        key_prefix="sk-llmgw-dev-abcd",
        account_id="acme",
        team_id="platform",
        user_id="alice",
        name="전 필드 키",
        allowed_models=("amazon.nova-lite-v1:0", "amazon.nova-pro-v1:0"),
        monthly_budget_usd=decimal.Decimal("123.45"),
        rpm_limit=30,
        expires_at="2027-01-01T00:00:00Z",
        last_used_at="2026-08-30T00:00:00Z",
    )


def test_키생성후모든필드가그대로조회된다(
    registry: repository.RegistryRepository,
) -> None:
    key = _모든필드를채운키()
    registry.put_api_key(key)
    assert registry.get_api_key_by_hash(key.key_hash) == key


def test_키수정후모든필드가그대로조회된다(
    registry: repository.RegistryRepository,
) -> None:
    key = _모든필드를채운키()
    registry.put_api_key(key)
    # model_copy(update=...) 는 검증을 건너뛴다. Decimal 을 직접 넣어야 한다.
    # float 을 넣으면 DynamoDB 직렬화에서 TypeError 가 난다.
    changed = key.model_copy(
        update={
            "name": "이름 변경",
            "rpm_limit": 60,
            "monthly_budget_usd": decimal.Decimal("7.5"),
        }
    )
    registry.update_api_key(changed)
    assert registry.get_api_key_by_hash(key.key_hash) == changed


def test_키회전후해시외모든필드가보존된다(
    registry: repository.RegistryRepository,
) -> None:
    # 회전이 rpm_limit 과 expires_at 을 지우던 버그의 회귀 테스트다.
    key = _모든필드를채운키(key_hash="hash-before")
    registry.put_api_key(key)
    rotated = key.model_copy(
        update={"key_hash": "hash-after", "key_prefix": "sk-llmgw-dev-wxyz"}
    )
    registry.rotate_api_key(old_key_hash=key.key_hash, rotated=rotated)

    stored = registry.get_api_key_by_hash("hash-after")
    assert stored == rotated
    # 옛 해시는 무효가 되어야 한다.
    assert registry.get_api_key_by_hash("hash-before") is None


def test_세저장경로가같은아이템을만든다(
    registry: repository.RegistryRepository,
) -> None:
    # 필드 이름을 나열하지 않고 세 경로의 결과가 같은지 본다. 도메인에 새
    # 필드가 생겨도 자동으로 검사된다.
    key = _모든필드를채운키(key_hash="hash-a")
    registry.put_api_key(key)
    created = registry.get_api_key_by_hash("hash-a")

    registry.update_api_key(key)
    updated = registry.get_api_key_by_hash("hash-a")

    rotated_key = key.model_copy(update={"key_hash": "hash-b"})
    registry.rotate_api_key(old_key_hash="hash-a", rotated=rotated_key)
    rotated = registry.get_api_key_by_hash("hash-b")

    assert created is not None and updated is not None and rotated is not None
    # 해시만 다르고 나머지는 같아야 한다.
    assert created.model_dump() == updated.model_dump()
    assert rotated.model_dump(exclude={"key_hash"}) == created.model_dump(
        exclude={"key_hash"}
    )
