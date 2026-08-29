"""OIDC 토큰으로 `/v1` 을 호출하는 종단간 테스트.

API 키 없이 고객 IdP 토큰만으로 LLM 을 호출하는 경로를 검증한다. 핵심은
세 가지다.

1. 유효한 토큰이 계정·팀·사용자로 매핑되어 사용량이 그 축에 집계되는가.
2. 사용자가 등록되지 않았을 때 자동 생성이 꺼져 있으면 거부하는가(fail-closed).
3. 예산·모델 제한이 API 키 경로와 동일하게 적용되는가.

`api_key` 픽스처는 기본 계정·팀·사용자 트리를 심는 부수 효과가 있어, 계정이
필요한 테스트에서 그 목적으로 함께 받는다.
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
from llmgw import auth as auth_module
from llmgw import cache as cache_module
from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import oidc
from llmgw import repository
from llmgw import services as services_module

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_E2E"
_AUDIENCE = "e2e-client"
_KID = "e2e-key"
_MODEL = "amazon.nova-lite-v1:0"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[typing.Any, typing.Any]:
    """테스트용 RSA 키쌍. 생성 비용이 있어 모듈 단위로 재사용한다."""
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
    user: str = "alice",
    groups: list[str] | None = None,
    team: str = "",
    expires_in: int = 3600,
) -> str:
    """서명된 액세스 토큰을 만든다."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload: dict[str, typing.Any] = {
        "iss": _ISSUER,
        "sub": f"sub-{user}",
        # user_claim 으로 이 값을 쓴다. domain.User 의 user_id 패턴이 `@` 를
        # 허용하지 않으므로 IdP 에서 로컬파트만 넣도록 매핑하는 구성을 흉내
        # 낸다.
        "preferred_username": user,
        "client_id": _AUDIENCE,
        "token_use": "access",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + expires_in,
    }
    if groups:
        payload["cognito:groups"] = groups
    if team:
        payload["custom:team"] = team
    return jwt.encode(
        payload, private_key, algorithm="RS256", headers={"kid": _KID}
    )


@pytest.fixture
def oidc_client(
    app_services: services_module.Services,
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> testclient.TestClient:
    """OIDC 검증기가 대역 JWKS 를 쓰는 앱 클라이언트."""
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
    patched = dataclasses.replace(
        app_services,
        oidc=verifier,
        authenticator=auth_module.Authenticator(
            registry=registry,
            usage_store=app_services.usage_store,
            settings=app_services.settings,
            oidc_verifier=verifier,
            clock_source=app_services.clock,
            metadata_cache=cache_module.TtlCache(ttl_seconds=0),
        ),
    )
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
        "audience": _AUDIENCE,
        "user_claim": "preferred_username",
        "team_claim": "custom:team",
        "admin_groups": "acme-admins",
    }
    values.update(overrides)
    registry.put_auth_config(
        domain.AccountAuthConfig(**values)  # type: ignore[arg-type]
    )


def _chat(client: testclient.TestClient, token: str) -> typing.Any:
    """채팅 완성을 호출한다."""
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "안녕"}],
        },
    )


def test_등록된사용자가OIDC토큰으로호출하고집계된다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """API 키 픽스처가 심는 alice 사용자를 토큰으로 인증한다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)

    # Act
    response = _chat(oidc_client, _token(private, team="platform"))

    # Assert
    assert response.status_code == 200, response.text
    totals = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )
    assert totals["TOTAL"].requests == 1
    assert totals["USER#alice"].requests == 1
    assert totals["TEAM#platform"].requests == 1


def test_사용자미등록_자동생성이꺼져있으면거부한다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """IdP 에 계정만 있으면 게이트웨이를 쓸 수 있게 되면 안 된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry, auto_provision=False)

    # Act
    response = _chat(oidc_client, _token(private, user="stranger"))

    # Assert
    assert response.status_code == 403


def test_자동생성이켜져있으면사용자가만들어지고예산이붙는다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(
        registry,
        auto_provision=True,
        provision_budget_usd=decimal.Decimal("5"),
    )

    # Act
    response = _chat(oidc_client, _token(private, user="newcomer"))

    # Assert
    assert response.status_code == 200, response.text
    created = registry.get_user("acme", "newcomer")
    assert created is not None
    assert created.monthly_budget_usd == decimal.Decimal("5")


def test_계정이비활성이면거부한다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry)
    account = registry.get_account("acme")
    assert account is not None
    registry.put_account(
        account.model_copy(update={"status": domain.EntityStatus.DISABLED}),
        overwrite=True,
    )

    # Act
    response = _chat(oidc_client, _token(private))

    # Assert
    assert response.status_code == 401


def test_인증설정이비활성이면거부한다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """계정의 외부 인증을 UI 에서 즉시 끌 수 있어야 한다."""
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(registry, status=domain.EntityStatus.DISABLED)

    # Act
    response = _chat(oidc_client, _token(private))

    # Assert
    assert response.status_code == 403


def test_발급자가등록되지않으면거부한다(
    oidc_client: testclient.TestClient,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """설정을 등록하지 않은 상태에서는 어떤 토큰도 통과하면 안 된다."""
    # Arrange
    del api_key
    private, _ = rsa_keys

    # Act
    response = _chat(oidc_client, _token(private))

    # Assert
    assert response.status_code == 401


def test_모델허용목록이OIDC경로에도적용된다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    del api_key
    private, _ = rsa_keys
    _register_auth(
        registry,
        auto_provision=True,
        provision_budget_usd=decimal.Decimal("5"),
        provision_allowed_models="amazon.nova-pro-v1:0",
    )

    # Act
    response = _chat(oidc_client, _token(private, user="limited"))

    # Assert
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"


def test_API키와OIDC토큰이같은엔드포인트에서모두동작한다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """접두어로 자격증명 종류를 구분한다."""
    # Arrange
    private, _ = rsa_keys
    _register_auth(registry)

    # Act
    with_key = _chat(oidc_client, api_key)
    with_jwt = _chat(oidc_client, _token(private))

    # Assert
    assert with_key.status_code == 200, with_key.text
    assert with_jwt.status_code == 200, with_jwt.text


def test_잘못된자격증명은종류를구분해알려주지않는다(
    oidc_client: testclient.TestClient,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    """어느 쪽이 틀렸는지 알려주면 유효한 자격증명을 찾는 단서가 된다."""
    # Arrange
    del api_key
    _register_auth(registry)

    # Act
    bad_key = _chat(oidc_client, "sk-llmgw-dev-invalid")
    bad_jwt = _chat(oidc_client, "eyJhbGciOiJub25lIn0.e30.")

    # Assert
    assert bad_key.status_code == 401
    assert bad_jwt.status_code == 401
    assert bad_key.json()["error"]["code"] == "invalid_api_key"
    assert bad_jwt.json()["error"]["code"] == "invalid_api_key"
