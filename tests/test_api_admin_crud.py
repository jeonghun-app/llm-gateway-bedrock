"""관리 API CRUD 확장 테스트 (v1.2).

계정·팀·사용자·키의 조회·수정·상태토글·삭제·재발급을 다룬다. 참조 무결성
(하위 리소스가 남아 있으면 삭제 거부)과 비활성화가 인증 경로에 실제로
반영되는지를 함께 검증한다.
"""

from __future__ import annotations

from fastapi import testclient

import conftest
from llmgw import repository


def _seed_key(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    **extra: object,
) -> dict[str, object]:
    """키를 발급하고 응답 본문을 반환한다."""
    payload: dict[str, object] = {"user_id": "alice"}
    payload.update(extra)
    response = client.post(
        "/admin/accounts/acme/keys", headers=admin_headers, json=payload
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _call(client: testclient.TestClient, plaintext: str) -> object:
    """발급된 키로 채팅 호출을 한 번 한다."""
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )


# -- 계정 update / delete ---------------------------------------------------


def test_계정을수정한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme",
        headers=admin_headers,
        json={"name": "새이름", "monthly_budget_usd": 300},
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "새이름"
    assert body["monthly_budget_usd"] == 300.0


def test_계정예산을null로되돌린다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
) -> None:
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "A", "monthly_budget_usd": 100},
    )

    # Act
    response = client.patch(
        "/admin/accounts/acme",
        headers=admin_headers,
        json={"monthly_budget_usd": None},
    )

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["monthly_budget_usd"] is None


def test_계정수정시이름은유지된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
) -> None:
    """예산만 보내면 이름이 지워지지 않아야 한다."""
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "원래이름"},
    )

    # Act
    response = client.patch(
        "/admin/accounts/acme",
        headers=admin_headers,
        json={"monthly_budget_usd": 50},
    )

    # Assert
    assert response.json()["name"] == "원래이름"


def test_없는계정수정_404(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.patch(
        "/admin/accounts/ghost", headers=admin_headers, json={"name": "x"}
    )

    # Assert
    assert response.status_code == 404


def test_빈계정을삭제한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "A"},
    )

    # Act
    deleted = client.delete("/admin/accounts/acme", headers=admin_headers)
    listed = client.get("/admin/accounts", headers=admin_headers)

    # Assert
    assert deleted.status_code == 204
    assert listed.json()["data"] == []


def test_하위팀이있으면계정삭제_409(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.delete("/admin/accounts/acme", headers=admin_headers)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_exists"


# -- 팀 get / update / status / delete --------------------------------------


def test_팀을조회하고수정한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    got = client.get(
        "/admin/accounts/acme/teams/platform", headers=admin_headers
    )
    patched = client.patch(
        "/admin/accounts/acme/teams/platform",
        headers=admin_headers,
        json={"name": "인프라팀", "monthly_budget_usd": 80},
    )

    # Assert
    assert got.status_code == 200
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "인프라팀"
    assert patched.json()["monthly_budget_usd"] == 80.0


def test_팀비활성화후해당키가차단된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = conftest.seed_api_key(registry)

    # Act
    status_response = client.post(
        "/admin/accounts/acme/teams/platform/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )
    call_response = _call(client, plaintext)

    # Assert
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "disabled"
    assert call_response.status_code == 401


def test_소속사용자가있으면팀삭제_409(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.delete(
        "/admin/accounts/acme/teams/platform", headers=admin_headers
    )

    # Assert
    assert response.status_code == 409


def test_빈팀을삭제한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "A"},
    )
    client.post(
        "/admin/accounts/acme/teams",
        headers=admin_headers,
        json={"team_id": "empty", "name": "빈팀"},
    )

    # Act
    response = client.delete(
        "/admin/accounts/acme/teams/empty", headers=admin_headers
    )

    # Assert
    assert response.status_code == 204


def test_없는팀조회_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.get(
        "/admin/accounts/acme/teams/ghost", headers=admin_headers
    )

    # Assert
    assert response.status_code == 404


# -- 사용자 get / update / status / delete ----------------------------------


def test_사용자를조회하고수정한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    got = client.get("/admin/accounts/acme/users/alice", headers=admin_headers)
    patched = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"name": "김앨리스", "monthly_budget_usd": 40},
    )

    # Assert
    assert got.status_code == 200
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "김앨리스"
    assert patched.json()["monthly_budget_usd"] == 40.0


def test_없는팀으로사용자수정_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """팀을 옮길 때 그 팀이 존재해야 한다."""
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"team_id": "ghost"},
    )

    # Assert
    assert response.status_code == 404


def test_사용자비활성화후해당키가차단된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = conftest.seed_api_key(registry)

    # Act
    status_response = client.post(
        "/admin/accounts/acme/users/alice/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )
    call_response = _call(client, plaintext)

    # Assert
    assert status_response.status_code == 200
    assert call_response.status_code == 401


def test_소유키가있으면사용자삭제_409(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_api_key(registry)

    # Act
    response = client.delete(
        "/admin/accounts/acme/users/alice", headers=admin_headers
    )

    # Assert
    assert response.status_code == 409


def test_키없는사용자를삭제한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.delete(
        "/admin/accounts/acme/users/alice", headers=admin_headers
    )

    # Assert
    assert response.status_code == 204


# -- API 키 get / update / status / rotate ----------------------------------


def test_키를수정한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)

    # Act
    patched = client.patch(
        f"/admin/accounts/acme/keys/{created['key_id']}",
        headers=admin_headers,
        json={
            "name": "수정된 키",
            "allowed_models": ["amazon.nova-lite-v1:0"],
            "monthly_budget_usd": 12.5,
        },
    )

    # Assert
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["name"] == "수정된 키"
    assert body["allowed_models"] == ["amazon.nova-lite-v1:0"]
    assert body["monthly_budget_usd"] == 12.5
    assert "api_key" not in body


def test_키수정후해시는유지되어호출이된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """수정은 해시를 바꾸지 않으므로 기존 평문 키가 계속 유효하다."""
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)
    plaintext = str(created["api_key"])

    # Act
    client.patch(
        f"/admin/accounts/acme/keys/{created['key_id']}",
        headers=admin_headers,
        json={"name": "이름만 변경"},
    )
    call_response = _call(client, plaintext)

    # Assert
    assert call_response.status_code == 200, call_response.text


def test_키를비활성화하면호출이차단된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)
    plaintext = str(created["api_key"])

    # Act
    status_response = client.post(
        f"/admin/accounts/acme/keys/{created['key_id']}/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )
    call_response = _call(client, plaintext)

    # Assert
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "disabled"
    assert call_response.status_code == 401


def test_키재발급시옛키는무효_새키는유효(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)
    old_plaintext = str(created["api_key"])

    # Act
    rotated = client.post(
        f"/admin/accounts/acme/keys/{created['key_id']}/rotate",
        headers=admin_headers,
    )
    new_plaintext = str(rotated.json()["api_key"])
    old_call = _call(client, old_plaintext)
    new_call = _call(client, new_plaintext)

    # Assert
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["key_id"] == created["key_id"], "key_id 는 유지된다"
    assert new_plaintext != old_plaintext
    assert old_call.status_code == 401, "옛 키는 무효여야 한다"
    assert new_call.status_code == 200, "새 키는 유효해야 한다"


def test_재발급후목록에키가하나만남는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """옛 해시 아이템이 남아 중복으로 보이면 안 된다."""
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)

    # Act
    client.post(
        f"/admin/accounts/acme/keys/{created['key_id']}/rotate",
        headers=admin_headers,
    )
    listed = client.get(
        "/admin/accounts/acme/keys", headers=admin_headers
    ).json()["data"]

    # Assert
    assert len(listed) == 1
    assert listed[0]["key_id"] == created["key_id"]


def test_없는키수정_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/keys/nope",
        headers=admin_headers,
        json={"name": "x"},
    )

    # Assert
    assert response.status_code == 404


def test_없는키재발급_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.post(
        "/admin/accounts/acme/keys/nope/rotate", headers=admin_headers
    )

    # Assert
    assert response.status_code == 404


# -- PATCH null 검증 (도메인 불변식) ----------------------------------------


def test_계정이름을null로수정하면거부한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
) -> None:
    """이름은 문자열이어야 한다. null 을 그대로 저장하면 불변식이 깨진다."""
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "A"},
    )

    # Act
    response = client.patch(
        "/admin/accounts/acme",
        headers=admin_headers,
        json={"name": None},
    )

    # Assert
    assert response.status_code == 400, response.text


def test_계정이름을빈문자열로수정하면거부한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
) -> None:
    # Arrange
    client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "A"},
    )

    # Act
    response = client.patch(
        "/admin/accounts/acme",
        headers=admin_headers,
        json={"name": ""},
    )

    # Assert
    assert response.status_code == 400, response.text


def test_사용자이름을null로수정하면거부한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"name": None},
    )

    # Assert
    assert response.status_code == 400, response.text


def test_사용자팀을null로수정하면거부한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """팀은 문자열이어야 한다. 팀을 비우려면 빈 문자열을 쓴다."""
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"team_id": None},
    )

    # Assert
    assert response.status_code == 400, response.text


def test_사용자팀을빈문자열로비운다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """빈 문자열은 팀 없음으로 허용된다."""
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"team_id": ""},
    )

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["team_id"] == ""


def test_키이름을null로수정하면거부한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)

    # Act
    response = client.patch(
        "/admin/accounts/acme/keys/" + str(created["key_id"]),
        headers=admin_headers,
        json={"name": None},
    )

    # Assert
    assert response.status_code == 400, response.text


def test_예산은null로되돌릴수있다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """예산 필드만 null 을 허용한다(무제한으로 되돌리기)."""
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.patch(
        "/admin/accounts/acme/users/alice",
        headers=admin_headers,
        json={"monthly_budget_usd": None},
    )

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["monthly_budget_usd"] is None


# -- 재발급 원자성 ----------------------------------------------------------


def test_재발급은옛해시아이템을남기지않는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    """트랜잭션으로 옛 해시 Put/Delete 를 묶어 옛 아이템이 남지 않아야 한다."""
    # Arrange
    conftest.seed_account_tree(registry)
    created = _seed_key(client, admin_headers)
    old_plaintext = str(created["api_key"])
    from llmgw import apikey

    old_hash = apikey.hash_api_key(old_plaintext)

    # Act
    client.post(
        "/admin/accounts/acme/keys/" + str(created["key_id"]) + "/rotate",
        headers=admin_headers,
    )

    # Assert: 옛 해시로는 더 이상 조회되지 않는다.
    assert registry.get_api_key_by_hash(old_hash) is None
    # 새 해시로 조회한 키는 하나뿐이고 key_id 가 유지된다.
    remaining = registry.list_api_keys("acme")
    assert len(remaining) == 1
    assert remaining[0].key_id == created["key_id"]
