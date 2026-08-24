"""관리 API 테스트."""

from __future__ import annotations

from fastapi import testclient

import conftest
from llmgw import repository


def _create_account(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    account_id: str = "acme",
    **extra: object,
) -> dict[str, object]:
    """계정을 만들고 응답 본문을 반환한다."""
    payload: dict[str, object] = {
        "account_id": account_id,
        "name": f"{account_id} Inc.",
    }
    payload.update(extra)
    response = client.post(
        "/admin/accounts", headers=admin_headers, json=payload
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# -- 인증 -------------------------------------------------------------------


def test_admin_토큰없으면401(client: testclient.TestClient) -> None:
    # Arrange / Act
    response = client.get("/admin/accounts")

    # Assert
    assert response.status_code == 401


def test_admin_잘못된토큰_401(client: testclient.TestClient) -> None:
    # Arrange / Act
    response = client.get("/admin/accounts", headers={"X-Admin-Token": "wrong"})

    # Assert
    assert response.status_code == 401


def test_admin_서버에토큰미설정시503(
    app_services: object, registry: repository.RegistryRepository
) -> None:
    """무인증으로 관리 API 가 열리는 것보다 사용 불가가 안전하다."""
    # Arrange
    import dataclasses

    from llmgw import app as app_module
    from llmgw import config

    services = app_services
    assert dataclasses.is_dataclass(services)
    open_settings = config.Settings(
        env="test",
        registry_table=conftest.REGISTRY_TABLE,
        usage_table=conftest.USAGE_TABLE,
        usage_agg_table=conftest.USAGE_AGG_TABLE,
        admin_token="",
    )
    unprotected = dataclasses.replace(services, settings=open_settings)
    application = app_module.create_app_with_services(unprotected)  # type: ignore[arg-type]

    # Act
    with testclient.TestClient(
        application, raise_server_exceptions=False
    ) as unprotected_client:
        response = unprotected_client.get(
            "/admin/accounts", headers={"X-Admin-Token": "anything"}
        )

    # Assert
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "admin_not_configured"


# -- 계정 -------------------------------------------------------------------


def test_계정을생성하고조회한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    created = _create_account(client, admin_headers, monthly_budget_usd=100)

    # Act
    response = client.get("/admin/accounts/acme", headers=admin_headers)

    # Assert
    assert created["account_id"] == "acme"
    assert response.status_code == 200
    body = response.json()
    assert body["monthly_budget_usd"] == 100.0
    assert body["status"] == "active"
    assert body["created_at"] == "2026-08-23T12:00:00Z"


def test_계정중복생성_409(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    _create_account(client, admin_headers)

    # Act
    response = client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "다시"},
    )

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_exists"


def test_없는계정조회_404(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get("/admin/accounts/nope", headers=admin_headers)

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_계정ID형식위반_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "Acme Inc", "name": "x"},
    )

    # Assert
    assert response.status_code == 400


def test_알수없는필드_400(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    """관리 API 는 오타를 조용히 무시하지 않는다."""
    # Arrange / Act
    response = client.post(
        "/admin/accounts",
        headers=admin_headers,
        json={"account_id": "acme", "name": "x", "budget": 100},
    )

    # Assert
    assert response.status_code == 400


def test_계정목록을반환한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    _create_account(client, admin_headers, "acme")
    _create_account(client, admin_headers, "beta")

    # Act
    response = client.get("/admin/accounts", headers=admin_headers)

    # Assert
    ids = [item["account_id"] for item in response.json()["data"]]
    assert ids == ["acme", "beta"]


def test_계정비활성화후해당키가차단된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    plaintext = conftest.seed_api_key(registry)

    # Act
    status_response = client.post(
        "/admin/accounts/acme/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )
    call_response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "disabled"
    assert call_response.status_code == 401


def test_상태값이올바르지않으면400(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.post(
        "/admin/accounts/acme/status",
        headers=admin_headers,
        json={"status": "paused"},
    )

    # Assert
    assert response.status_code == 400


# -- 팀 / 사용자 ------------------------------------------------------------


def test_팀을생성하고목록에나온다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    _create_account(client, admin_headers)

    # Act
    created = client.post(
        "/admin/accounts/acme/teams",
        headers=admin_headers,
        json={"team_id": "platform", "name": "플랫폼팀"},
    )
    listed = client.get("/admin/accounts/acme/teams", headers=admin_headers)

    # Assert
    assert created.status_code == 201
    assert [item["team_id"] for item in listed.json()["data"]] == ["platform"]


def test_없는계정에팀생성_404(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.post(
        "/admin/accounts/ghost/teams",
        headers=admin_headers,
        json={"team_id": "ghost-team", "name": "T"},
    )

    # Assert
    assert response.status_code == 404


def test_사용자를생성한다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    _create_account(client, admin_headers)
    client.post(
        "/admin/accounts/acme/teams",
        headers=admin_headers,
        json={"team_id": "platform", "name": "플랫폼팀"},
    )

    # Act
    response = client.post(
        "/admin/accounts/acme/users",
        headers=admin_headers,
        json={
            "user_id": "alice",
            "name": "앨리스",
            "email": "alice@example.com",
            "team_id": "platform",
        },
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["team_id"] == "platform"


def test_없는팀으로사용자생성_404(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    """사용자와 팀이 어긋나면 팀 축 집계가 틀어진다."""
    # Arrange
    _create_account(client, admin_headers)

    # Act
    response = client.post(
        "/admin/accounts/acme/users",
        headers=admin_headers,
        json={"user_id": "alice", "name": "A", "team_id": "ghost"},
    )

    # Assert
    assert response.status_code == 404


def test_팀없이사용자생성은허용된다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange
    _create_account(client, admin_headers)

    # Act
    response = client.post(
        "/admin/accounts/acme/users",
        headers=admin_headers,
        json={"user_id": "solo", "name": "혼자"},
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["team_id"] == ""


# -- API 키 -----------------------------------------------------------------


def test_키발급응답에평문키가한번만들어있다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    created = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={"user_id": "alice", "name": "CI 키"},
    )
    listed = client.get("/admin/accounts/acme/keys", headers=admin_headers)

    # Assert
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["api_key"].startswith("sk-llmgw-test-")
    assert body["team_id"] == "platform", "팀은 사용자에서 상속돼야 한다"
    listed_keys = listed.json()["data"]
    assert len(listed_keys) == 1
    assert (
        "api_key" not in listed_keys[0]
    ), "목록 응답에 평문 키가 노출되면 안 된다"
    assert "key_hash" not in listed_keys[0]


def test_발급한키로바로호출할수있다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={"user_id": "alice"},
    )
    plaintext = created.json()["api_key"]

    # Act
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert response.status_code == 200, response.text


def test_없는사용자로키발급_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={"user_id": "ghost"},
    )

    # Assert
    assert response.status_code == 404


def test_키삭제후호출이차단된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={"user_id": "alice"},
    ).json()

    # Act
    deleted = client.delete(
        f"/admin/accounts/acme/keys/{created['key_id']}",
        headers=admin_headers,
    )
    call_response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {created['api_key']}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )

    # Assert
    assert deleted.status_code == 204
    assert call_response.status_code == 401


def test_없는키삭제_404(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    response = client.delete(
        "/admin/accounts/acme/keys/nope", headers=admin_headers
    )

    # Assert
    assert response.status_code == 404


def test_허용모델을지정해키를발급한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)

    # Act
    created = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={
            "user_id": "alice",
            "allowed_models": ["amazon.nova-lite-v1:0"],
            "monthly_budget_usd": 25.5,
        },
    ).json()

    # Assert
    assert created["allowed_models"] == ["amazon.nova-lite-v1:0"]
    assert created["monthly_budget_usd"] == 25.5


def test_키사용후last_used_at이기록된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    conftest.seed_account_tree(registry)
    created = client.post(
        "/admin/accounts/acme/keys",
        headers=admin_headers,
        json={"user_id": "alice"},
    ).json()

    # Act
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {created['api_key']}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    listed = client.get(
        "/admin/accounts/acme/keys", headers=admin_headers
    ).json()["data"]

    # Assert
    assert listed[0]["last_used_at"] == "2026-08-23T12:00:00Z"


# -- 모델 -------------------------------------------------------------------


def test_모델목록에단가인지여부가함께나온다(
    client: testclient.TestClient, admin_headers: dict[str, str]
) -> None:
    # Arrange / Act
    response = client.get("/admin/models", headers=admin_headers)

    # Assert
    assert response.status_code == 200
    rows = {
        item["model_id"]: item["pricing_known"]
        for item in response.json()["data"]
    }
    assert rows["amazon.nova-lite-v1:0"] is True
    assert (
        rows["amazon.nova-pro-v1:0"] is False
    ), "픽스처 단가 표에 없는 모델은 False 여야 한다"
    assert (
        rows["us.anthropic.claude-3-haiku-20240307-v1:0"] is True
    ), "추론 프로파일도 기반 모델 단가로 인지돼야 한다"
