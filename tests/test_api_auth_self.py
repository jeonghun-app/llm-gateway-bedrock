"""셀프서비스 키 발급 테스트.

개발자가 관리자에게 요청하지 않고 스스로 키를 받는 경로다. 핵심 검증은
**권한 상승이 불가능한가**다.

1. 남에게 키를 발급하거나 남의 키를 지울 수 있는가.
2. 허용 모델을 계정 범위보다 넓힐 수 있는가.
3. 예산을 스스로 올릴 수 있는가.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import typing

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import testclient
import jwt
import pytest

from llmgw import app as app_module
from llmgw import cache as cache_module
from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import oidc
from llmgw import repository
from llmgw import services as services_module

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_SELF"
_KID = "self-key"


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


def _token(private_key: typing.Any, *, user: str = "alice") -> str:
    """서명된 액세스 토큰을 만든다."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload = {
        "iss": _ISSUER,
        "sub": f"sub-{user}",
        "preferred_username": user,
        "email": f"{user}@example.com",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
    }
    return jwt.encode(
        payload, private_key, algorithm="RS256", headers={"kid": _KID}
    )


@pytest.fixture
def self_client(
    app_services: services_module.Services,
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> testclient.TestClient:
    """셀프서비스 라우터를 쓰는 앱 클라이언트."""
    _, public = rsa_keys
    verifier = oidc.OidcVerifier(
        registry=registry,
        settings=app_services.settings,
        logger=app_services.logger,
        clock_source=clock.SYSTEM_CLOCK,
        config_cache=cache_module.TtlCache(ttl_seconds=0),
        jwks_factory=lambda _url: typing.cast(
            "oidc.JwksCache", _StubJwks(public)
        ),
    )
    patched = dataclasses.replace(app_services, oidc=verifier)
    return testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    )


def _register_auth(
    registry: repository.RegistryRepository, **overrides: object
) -> None:
    """계정 인증 설정을 등록한다."""
    values: dict[str, object] = {
        "account_id": "acme",
        "issuer": _ISSUER,
        "user_claim": "preferred_username",
    }
    values.update(overrides)
    registry.put_auth_config(
        domain.AccountAuthConfig(**values)  # type: ignore[arg-type]
    )


def _headers(token: str) -> dict[str, str]:
    """OIDC 인증 헤더."""
    return {"Authorization": f"Bearer {token}"}


def test_me가매핑결과를보여준다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """클레임 매핑이 틀리면 사용량이 엉뚱한 축에 집계된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)

    # Act
    response = self_client.get("/auth/me", headers=_headers(_token(private)))

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == "acme"
    assert body["user_id"] == "alice"
    assert body["registered"] is True
    assert body["is_account_admin"] is False


def test_me는토큰이없으면401(
    self_client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = self_client.get("/auth/me")

    # Assert
    assert response.status_code == 401


def test_본인에게키를발급하고평문을한번받는다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)

    # Act
    response = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "노트북"},
    )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == "alice"
    assert body["account_id"] == "acme"
    assert body["api_key"].startswith("sk-llmgw-")

    # 발급한 키로 실제 호출이 되어야 한다.
    chat = self_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {body['api_key']}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )
    assert chat.status_code == 200, chat.text


def test_등록되지않은사용자는키를만들수없다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """IdP 계정만으로 게이트웨이를 쓸 수 있게 되면 안 된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)

    # Act
    response = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private, user="stranger")),
        json={"name": "몰래"},
    )

    # Assert
    assert response.status_code == 403


def test_허용모델을계정범위보다넓힐수없다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """스스로 범위를 넓힐 수 있으면 모델 통제가 의미를 잃는다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry, provision_allowed_models="amazon.nova-lite-v1:0")

    # Act
    response = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={
            "name": "욕심",
            "allowed_models": [
                "amazon.nova-lite-v1:0",
                "anthropic.claude-3-5-sonnet-20241022-v2:0",
            ],
        },
    )

    # Assert
    assert response.status_code == 201, response.text
    assert response.json()["allowed_models"] == ["amazon.nova-lite-v1:0"]


def test_전부범위밖을요청하면계정범위가적용된다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """빈 목록은 '제한 없음' 을 뜻하므로 그대로 두면 권한이 넓어진다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry, provision_allowed_models="amazon.nova-lite-v1:0")

    # Act
    response = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "우회", "allowed_models": ["anthropic.claude-3-opus"]},
    )

    # Assert
    assert response.status_code == 201, response.text
    assert response.json()["allowed_models"] == ["amazon.nova-lite-v1:0"]


def test_예산은요청으로올릴수없다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """스스로 한도를 올릴 수 있으면 예산 통제가 의미를 잃는다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(
        registry,
        auto_provision=True,
        provision_budget_usd=decimal.Decimal("3"),
    )

    # Act: 본문에 예산을 넣어 보낸다.
    rejected = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "탐욕", "monthly_budget_usd": 9999},
    )
    normal = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "정상"},
    )

    # Assert: 스키마가 모르는 필드를 아예 거부한다. 무시보다 강한 방어다.
    assert rejected.status_code == 400
    # 예산은 계정 설정값이 그대로 붙는다.
    assert normal.status_code == 201, normal.text
    assert normal.json()["monthly_budget_usd"] == 3.0


def test_자기키목록만보인다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_user(domain.User(account_id="acme", user_id="bob", name="밥"))
    self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "내키"},
    )
    self_client.post(
        "/auth/keys",
        headers=_headers(_token(private, user="bob")),
        json={"name": "남의키"},
    )

    # Act
    response = self_client.get("/auth/keys", headers=_headers(_token(private)))

    # Assert
    assert response.status_code == 200, response.text
    names = [item["name"] for item in response.json()["data"]]
    # 픽스처가 심은 키와 방금 만든 키가 모두 alice 소유다. 남의 키(밥)는
    # 보이지 않아야 한다.
    assert "내키" in names
    assert "남의키" not in names
    # 평문은 목록에 없어야 한다.
    assert all("api_key" not in item for item in response.json()["data"])


def test_남의키는지울수없고존재도알려주지않는다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """키 ID 만 알아도 남의 키를 무효화할 수 있으면 안 된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    registry.put_user(domain.User(account_id="acme", user_id="bob", name="밥"))
    victim = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private, user="bob")),
        json={"name": "밥의키"},
    ).json()

    # Act
    response = self_client.delete(
        f"/auth/keys/{victim['key_id']}", headers=_headers(_token(private))
    )

    # Assert
    assert response.status_code == 404
    remaining = self_client.get(
        "/auth/keys", headers=_headers(_token(private, user="bob"))
    ).json()["data"]
    assert len(remaining) == 1


def test_자기키는폐기할수있다(
    self_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    created = self_client.post(
        "/auth/keys",
        headers=_headers(_token(private)),
        json={"name": "폐기대상"},
    ).json()

    # Act
    response = self_client.delete(
        f"/auth/keys/{created['key_id']}", headers=_headers(_token(private))
    )

    # Assert
    assert response.status_code == 204
    remaining = self_client.get(
        "/auth/keys", headers=_headers(_token(private))
    ).json()["data"]
    assert created["key_id"] not in [item["key_id"] for item in remaining]
