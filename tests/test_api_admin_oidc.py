"""관리 API 의 OIDC 인증과 권한 범위 테스트.

공유 관리 토큰 하나로는 누가 무엇을 했는지 남지 않고 권한을 좁힐 수도 없다.
고객이 IdP 를 연결하면 계정 관리자는 자기 계정만, 플랫폼 관리자는 전체를
다루게 된다.

마지막 테스트는 **계정을 경로로 받는 모든 관리 라우트를 열거**해 범위 강제가
빠진 곳이 없는지 확인한다. 엔드포인트가 늘어날 때 검사를 빠뜨리는 실수를
자동으로 잡기 위한 것이다.
"""

from __future__ import annotations

import dataclasses
import datetime
import typing

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import testclient
import jwt
import pytest

from llmgw import app as app_module
from llmgw import cache as cache_module
from llmgw import clock
from llmgw import config
from llmgw import domain
from llmgw import errors
from llmgw import oidc
from llmgw import repository
from llmgw import services as services_module

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ADMIN"
_KID = "admin-key"
_ACCOUNT_ADMIN_GROUP = "acme-admins"
_PLATFORM_ADMIN_GROUP = "llmgw-root"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[typing.Any, typing.Any]:
    """테스트용 RSA 키쌍."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


class _StubJwks:
    """네트워크를 타지 않는 JWKS 대역."""

    def __init__(self, public_key: typing.Any) -> None:
        """공개키 하나만 담는다."""
        self._public_key = public_key

    def get_key(self, kid: str) -> typing.Any:
        """등록된 kid 만 반환한다."""
        if kid != _KID:
            raise errors.AuthenticationError("키를 찾을 수 없다.")
        return self._public_key


def _token(
    private_key: typing.Any,
    *,
    user: str = "adminuser",
    groups: list[str] | None = None,
) -> str:
    """서명된 관리자 토큰을 만든다."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload: dict[str, typing.Any] = {
        "iss": _ISSUER,
        "sub": f"sub-{user}",
        "preferred_username": user,
        "email": f"{user}@example.com",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
    }
    if groups:
        payload["cognito:groups"] = groups
    return jwt.encode(
        payload, private_key, algorithm="RS256", headers={"kid": _KID}
    )


@pytest.fixture
def admin_oidc_client(
    app_services: services_module.Services,
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> testclient.TestClient:
    """플랫폼 관리자 그룹이 설정된 앱 클라이언트."""
    _, public = rsa_keys
    settings = dataclasses.replace(
        app_services, settings=app_services.settings
    ).settings.model_copy(
        update={"oidc_platform_admin_groups": _PLATFORM_ADMIN_GROUP}
    )
    verifier = oidc.OidcVerifier(
        registry=registry,
        settings=settings,
        logger=app_services.logger,
        clock_source=clock.SYSTEM_CLOCK,
        config_cache=cache_module.TtlCache(ttl_seconds=0),
        jwks_factory=lambda _url: typing.cast(
            "oidc.JwksCache", _StubJwks(public)
        ),
    )
    patched = dataclasses.replace(
        app_services, settings=settings, oidc=verifier
    )
    return testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    )


def _register_auth(registry: repository.RegistryRepository) -> None:
    """acme 계정에 인증 설정을 등록한다."""
    registry.put_auth_config(
        domain.AccountAuthConfig(
            account_id="acme",
            issuer=_ISSUER,
            user_claim="preferred_username",
            admin_groups=_ACCOUNT_ADMIN_GROUP,
        )
    )


def _headers(token: str) -> dict[str, str]:
    """OIDC 관리 인증 헤더."""
    return {"Authorization": f"Bearer {token}"}


def test_계정관리자토큰으로자기계정을조회한다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.get(
        "/admin/accounts/acme", headers=_headers(token)
    )

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == "acme"


def test_계정관리자는다른계정을조회할수없다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """권한 범위를 벗어난 계정은 존재 여부도 알려주지 않아야 한다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.get(
        "/admin/accounts/beta", headers=_headers(token)
    )

    # Assert
    assert response.status_code == 403


def test_플랫폼관리자는다른계정도조회한다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    token = _token(private, groups=[_PLATFORM_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.get(
        "/admin/accounts/beta", headers=_headers(token)
    )

    # Assert
    assert response.status_code == 200, response.text


def test_관리자그룹이없는토큰은관리API를쓸수없다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """일반 사용자 토큰으로 관리 API 가 열리면 안 된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    token = _token(private, groups=["viewers"])

    # Act
    response = admin_oidc_client.get(
        "/admin/accounts/acme", headers=_headers(token)
    )

    # Assert
    assert response.status_code == 403


def test_계정관리자는계정을생성할수없다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """계정 생성은 계정을 본문으로 받으므로 경로 기반 검사가 통하지 않는다.
    플랫폼 범위만 허용해야 한다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.post(
        "/admin/accounts",
        headers=_headers(token),
        json={"account_id": "newtenant", "name": "New"},
    )

    # Assert
    assert response.status_code == 403
    assert registry.get_account("newtenant") is None


def test_플랫폼관리자는계정을생성할수있다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    token = _token(private, groups=[_PLATFORM_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.post(
        "/admin/accounts",
        headers=_headers(token),
        json={"account_id": "newtenant", "name": "New"},
    )

    # Assert
    assert response.status_code == 201, response.text


def test_공유관리토큰은여전히플랫폼범위다(
    admin_oidc_client: testclient.TestClient,
    settings: config.Settings,
    api_key: str,
) -> None:
    """부트스트랩과 비상 접근 경로가 유지돼야 한다."""
    # Arrange
    del api_key

    # Act
    response = admin_oidc_client.get(
        "/admin/accounts/acme",
        headers={"X-Admin-Token": settings.admin_token},
    )

    # Assert
    assert response.status_code == 200, response.text


def test_자격증명이없으면401(
    admin_oidc_client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = admin_oidc_client.get("/admin/accounts")

    # Assert
    assert response.status_code == 401


def test_계정관리자의쓰기작업도범위밖이면차단된다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """읽기만 막고 쓰기를 놓치면 의미가 없다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    # Act
    created = admin_oidc_client.post(
        "/admin/accounts/beta/teams",
        headers=_headers(token),
        json={"team_id": "sneaky", "name": "침입"},
    )
    patched = admin_oidc_client.patch(
        "/admin/accounts/beta",
        headers=_headers(token),
        json={"name": "탈취"},
    )
    deleted = admin_oidc_client.delete(
        "/admin/accounts/beta", headers=_headers(token)
    )

    # Assert
    assert created.status_code == 403
    assert patched.status_code == 403
    assert deleted.status_code == 403
    assert registry.get_team("beta", "sneaky") is None
    beta = registry.get_account("beta")
    assert beta is not None
    assert beta.name == "Beta"


def test_계정을경로로받는모든관리라우트가범위를강제한다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """엔드포인트가 늘어날 때 범위 검사를 빠뜨리는 실수를 자동으로 잡는다.

    `{account_id}` 를 경로에 가진 모든 관리 라우트를 열거해, 권한 밖 계정으로
    호출하면 403 이 나오는지 확인한다. 404 나 200 이 나오면 그 라우트는
    범위 강제를 우회한 것이다.
    """
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    app = admin_oidc_client.app
    spec = typing.cast(
        "dict[str, typing.Any]",
        app.openapi(),  # type: ignore[attr-defined]
    )
    checked: list[str] = []
    for path, operations in sorted(spec["paths"].items()):
        if not path.startswith("/admin/") or "{account_id}" not in path:
            continue
        # 권한 밖 계정으로 치환한다. 나머지 경로 변수는 임의값이면 된다.
        concrete = path.replace("{account_id}", "beta")
        for name in ("team_id", "user_id", "key_id"):
            concrete = concrete.replace(f"{{{name}}}", "placeholder")
        if "{" in concrete:
            continue
        for method in sorted(operations):
            if method.upper() not in {
                "GET",
                "POST",
                "PATCH",
                "PUT",
                "DELETE",
            }:
                continue
            response = admin_oidc_client.request(
                method.upper(), concrete, headers=_headers(token), json={}
            )
            checked.append(f"{method.upper()} {concrete}")

            # Assert
            assert response.status_code == 403, (
                f"{method.upper()} {concrete} 가 범위 강제를 우회했다:"
                f" {response.status_code}"
            )

    assert len(checked) >= 20, f"검사한 라우트가 너무 적다: {checked}"


# ---------------------------------------------------------------------------
# 인증 설정 관리 API
# ---------------------------------------------------------------------------


def test_인증설정을만들고조회하고삭제한다(
    admin_oidc_client: testclient.TestClient,
    settings: config.Settings,
    api_key: str,
) -> None:
    # Arrange
    del api_key
    headers = {"X-Admin-Token": settings.admin_token}
    body = {
        "issuer": _ISSUER,
        "audience": "client-1",
        "user_claim": "preferred_username",
        "admin_groups": _ACCOUNT_ADMIN_GROUP,
    }

    # Act
    empty = admin_oidc_client.get("/admin/accounts/acme/auth", headers=headers)
    created = admin_oidc_client.put(
        "/admin/accounts/acme/auth", headers=headers, json=body
    )
    fetched = admin_oidc_client.get(
        "/admin/accounts/acme/auth", headers=headers
    )
    removed = admin_oidc_client.delete(
        "/admin/accounts/acme/auth", headers=headers
    )
    after = admin_oidc_client.get("/admin/accounts/acme/auth", headers=headers)

    # Assert
    assert empty.status_code == 200
    assert empty.json()["configured"] is False
    assert created.status_code == 200, created.text
    assert fetched.json()["configured"] is True
    assert fetched.json()["issuer"] == _ISSUER
    assert fetched.json()["effective_jwks_url"].endswith("/jwks.json")
    assert removed.status_code == 204
    assert after.json()["configured"] is False


def test_인증설정을끄면토큰이거부된다(
    admin_oidc_client: testclient.TestClient,
    settings: config.Settings,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """UI 에서 즉시 차단할 수 있어야 한다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    headers = {"X-Admin-Token": settings.admin_token}
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])
    assert (
        admin_oidc_client.get(
            "/admin/accounts/acme", headers=_headers(token)
        ).status_code
        == 200
    )

    # Act
    toggled = admin_oidc_client.post(
        "/admin/accounts/acme/auth/status",
        headers=headers,
        json={"status": "disabled"},
    )
    blocked = admin_oidc_client.get(
        "/admin/accounts/acme", headers=_headers(token)
    )

    # Assert
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["status"] == "disabled"
    assert blocked.status_code == 403


def test_내부주소JWKS는저장단계에서거부된다(
    admin_oidc_client: testclient.TestClient,
    settings: config.Settings,
    api_key: str,
) -> None:
    """잘못된 값을 저장해 두면 그 계정의 모든 토큰 검증이 실패하고 원인이
    설정에 있다는 것을 알기 어렵다. SSRF 통로이기도 하다."""
    # Arrange
    del api_key
    headers = {"X-Admin-Token": settings.admin_token}

    # Act
    response = admin_oidc_client.put(
        "/admin/accounts/acme/auth",
        headers=headers,
        json={
            "issuer": _ISSUER,
            "jwks_url": "https://169.254.169.254/latest/meta-data/",
        },
    )

    # Assert
    assert response.status_code == 400
    assert "내부 네트워크" in response.json()["error"]["message"]


def test_다른계정이쓰는발급자는409(
    admin_oidc_client: testclient.TestClient,
    settings: config.Settings,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    """발급자를 가로채면 그 계정 사용자로 위장할 수 있다."""
    # Arrange
    del api_key
    headers = {"X-Admin-Token": settings.admin_token}
    registry.put_account(domain.Account(account_id="beta", name="Beta"))
    _register_auth(registry)

    # Act
    response = admin_oidc_client.put(
        "/admin/accounts/beta/auth",
        headers=headers,
        json={"issuer": _ISSUER},
    )

    # Assert
    assert response.status_code == 409


def test_계정관리자는자기계정인증설정을바꿀수있다(
    admin_oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """고객이 자기 IdP 설정을 스스로 관리하는 것이 이 기능의 목적이다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    token = _token(private, groups=[_ACCOUNT_ADMIN_GROUP])

    # Act
    response = admin_oidc_client.put(
        "/admin/accounts/acme/auth",
        headers=_headers(token),
        json={
            "issuer": _ISSUER,
            "user_claim": "preferred_username",
            "admin_groups": _ACCOUNT_ADMIN_GROUP,
            "audience": "new-client",
        },
    )

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["audience"] == "new-client"
