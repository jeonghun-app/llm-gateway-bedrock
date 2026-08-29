"""OIDC 토큰 검증 테스트.

실제 RSA 키쌍으로 토큰을 만들어 검증한다. 네트워크를 타지 않도록 JWKS 캐시를
대역 객체로 바꿔 넣는다.

가장 중요한 검증은 세 가지다.

1. 서명 위조와 `alg` 바꿔치기가 막히는가.
2. 등록되지 않은 발급자가 거부되는가. 통과하면 아무 IdP 의 토큰이나 받는다.
3. 관리 권한이 그룹으로만 부여되는가.
"""

from __future__ import annotations

import datetime
import typing

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from llmgw import clock
from llmgw import config
from llmgw import domain
from llmgw import errors
from llmgw import oidc
from llmgw import repository

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
_AUDIENCE = "client-abc"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[typing.Any, typing.Any]:
    """테스트용 RSA 키쌍. 생성 비용이 있어 모듈 단위로 재사용한다."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


class _StubJwks:
    """네트워크를 타지 않는 JWKS 캐시 대역."""

    def __init__(self, keys: dict[str, typing.Any]) -> None:
        """키 사전으로 대역을 만든다."""
        self._keys = keys

    def get_key(self, kid: str) -> typing.Any:
        """키를 반환한다. 없으면 인증 실패로 다룬다."""
        key = self._keys.get(kid)
        if key is None:
            raise errors.AuthenticationError(
                "토큰 서명 키를 인증 서버에서 찾을 수 없다."
            )
        return key


def _make_token(
    private_key: typing.Any,
    *,
    issuer: str = _ISSUER,
    audience: str | None = _AUDIENCE,
    algorithm: str = "RS256",
    kid: str | None = _KID,
    expires_in: int = 3600,
    extra: dict[str, typing.Any] | None = None,
) -> str:
    """서명된 테스트 토큰을 만든다."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload: dict[str, typing.Any] = {
        "iss": issuer,
        "sub": "sub-123",
        "email": "jiwon@example.com",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + expires_in,
    }
    if audience is not None:
        payload["aud"] = audience
    if extra:
        payload.update(extra)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(
        payload, private_key, algorithm=algorithm, headers=headers
    )


def _verifier(
    registry: repository.RegistryRepository,
    public_key: typing.Any,
    *,
    platform_admin_groups: str = "",
) -> oidc.OidcVerifier:
    """대역 JWKS 를 쓰는 검증기를 만든다."""
    settings = config.Settings(
        admin_token="t", oidc_platform_admin_groups=platform_admin_groups
    )
    return oidc.OidcVerifier(
        registry=registry,
        settings=settings,
        logger=oidc.observability.create_logger(
            service_name="test", level="CRITICAL"
        ),
        clock_source=clock.SYSTEM_CLOCK,
        jwks_factory=lambda _url: typing.cast(
            "oidc.JwksCache", _StubJwks({_KID: public_key})
        ),
    )


def _register(
    registry: repository.RegistryRepository, **overrides: object
) -> domain.AccountAuthConfig:
    """계정 인증 설정을 등록한다."""
    values: dict[str, object] = {
        "account_id": "acme",
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "admin_groups": "acme-admins",
        "groups_claim": "cognito:groups",
    }
    values.update(overrides)
    config_obj = domain.AccountAuthConfig(**values)  # type: ignore[arg-type]
    registry.put_auth_config(config_obj)
    return config_obj


def test_유효한토큰이계정과사용자로매핑된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)

    # Act
    identity = verifier.verify(_make_token(private))

    # Assert
    assert identity.account_id == "acme"
    assert identity.user_id == "jiwon@example.com"
    assert identity.subject == "sub-123"
    assert identity.is_admin is False


def test_등록되지않은발급자는거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """통과하면 아무 IdP 가 발급한 토큰이나 받아들이게 된다."""
    # Arrange
    private, public = rsa_keys
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(_make_token(private, issuer="https://evil.example"))


def test_다른키로서명한토큰은거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    _, public = rsa_keys
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _register(registry)
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(_make_token(attacker))


def test_HS256으로바꿔치기한토큰은거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """대칭키 알고리즘을 허용하면 공개키를 비밀로 써서 서명을 위조할 수
    있다."""
    # Arrange
    _, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)
    forged = _make_token(
        "any-shared-secret", algorithm="HS256"  # type: ignore[arg-type]
    )

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(forged)


def test_kid없는토큰은거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(_make_token(private, kid=None))


def test_만료된토큰은거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(_make_token(private, expires_in=-10))


def test_청중이다르면거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.AuthenticationError):
        verifier.verify(_make_token(private, audience="other-client"))


def test_Cognito액세스토큰은client_id를청중으로본다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """Cognito 액세스 토큰에는 aud 가 없고 client_id 가 들어온다."""
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)
    token = _make_token(
        private,
        audience=None,
        extra={"client_id": _AUDIENCE, "token_use": "access"},
    )

    # Act
    identity = verifier.verify(token)

    # Assert
    assert identity.account_id == "acme"


def test_설정이비활성이면거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """계정의 외부 인증을 즉시 차단할 수 있어야 한다."""
    # Arrange
    private, public = rsa_keys
    _register(registry, status=domain.EntityStatus.DISABLED)
    verifier = _verifier(registry, public)

    # Act / Assert
    with pytest.raises(errors.PermissionDeniedError):
        verifier.verify(_make_token(private))


def test_계정관리자그룹이면계정범위관리자가된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)
    token = _make_token(private, extra={"cognito:groups": ["acme-admins"]})

    # Act
    identity = verifier.verify(token)
    principal = identity.to_admin_principal()

    # Assert
    assert identity.is_account_admin is True
    assert identity.is_platform_admin is False
    assert principal.scope is domain.AdminScope.ACCOUNT
    assert principal.can_manage("acme") is True
    assert principal.can_manage("beta") is False


def test_플랫폼관리자그룹이면모든계정을관리한다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public, platform_admin_groups="llmgw-root")
    token = _make_token(private, extra={"cognito:groups": ["llmgw-root"]})

    # Act
    identity = verifier.verify(token)
    principal = identity.to_admin_principal()

    # Assert
    assert identity.is_platform_admin is True
    assert principal.scope is domain.AdminScope.PLATFORM
    assert principal.can_manage("beta") is True


def test_그룹이없으면관리주체로변환할수없다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """일반 사용자가 관리 API 를 쓰지 못해야 한다."""
    # Arrange
    private, public = rsa_keys
    _register(registry)
    verifier = _verifier(registry, public)
    identity = verifier.verify(_make_token(private))

    # Act / Assert
    with pytest.raises(errors.PermissionDeniedError):
        identity.to_admin_principal()


def test_관리자그룹설정이비어있으면아무도관리자가아니다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """빈 설정을 '전부 허용' 으로 해석하면 관리 API 가 열린다."""
    # Arrange
    private, public = rsa_keys
    _register(registry, admin_groups="")
    verifier = _verifier(registry, public)
    token = _make_token(private, extra={"cognito:groups": ["anything"]})

    # Act
    identity = verifier.verify(token)

    # Assert
    assert identity.is_admin is False


def test_그룹클레임이공백구분문자열도처리된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    """일부 IdP 는 그룹을 공백 구분 문자열로 넣는다."""
    # Arrange
    private, public = rsa_keys
    _register(registry, groups_claim="roles")
    verifier = _verifier(registry, public)
    token = _make_token(private, extra={"roles": "viewer acme-admins"})

    # Act
    identity = verifier.verify(token)

    # Assert
    assert "acme-admins" in identity.groups
    assert identity.is_account_admin is True


def test_팀클레임이설정되면팀이매핑된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    private, public = rsa_keys
    _register(registry, team_claim="custom:team")
    verifier = _verifier(registry, public)
    token = _make_token(private, extra={"custom:team": "backend"})

    # Act
    identity = verifier.verify(token)

    # Assert
    assert identity.team_id == "backend"


def test_형식이깨진토큰은거부된다(
    registry: repository.RegistryRepository,
    rsa_keys: tuple[typing.Any, typing.Any],
) -> None:
    # Arrange
    _, public = rsa_keys
    verifier = _verifier(registry, public)

    # Act / Assert
    for bad in ("", "not-a-jwt", "a.b.c", "Bearer x"):
        with pytest.raises(errors.AuthenticationError):
            verifier.verify(bad)


# ---------------------------------------------------------------------------
# JWKS URL 검증 (SSRF 방어)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Fargate 태스크 역할 자격증명 엔드포인트.
        "https://169.254.170.2/v2/credentials",
        # EC2 인스턴스 메타데이터.
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/jwks.json",
        "https://localhost/jwks.json",
        "https://10.0.4.69/jwks.json",
        "https://192.168.1.1/jwks.json",
        "https://172.16.0.1/jwks.json",
        # 평문 HTTP 는 중간자가 서명 키를 바꿔치기할 수 있다.
        "http://cognito-idp.us-east-1.amazonaws.com/x/jwks.json",
        "file:///etc/passwd",
        "https:///jwks.json",
    ],
)
def test_validate_jwks_url_내부주소와평문은거부한다(url: str) -> None:
    """JWKS URL 은 관리자가 설정하는 값이다. 검증 없이 호출하면 게이트웨이가
    내부망으로 요청을 보내는 SSRF 통로가 된다. Fargate 에서는
    169.254.170.2 가 태스크 역할 자격증명을 서빙한다."""
    # Arrange / Act / Assert
    with pytest.raises(errors.InvalidRequestError):
        oidc.validate_jwks_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_A/.well-known/jwks.json",
        "https://www.googleapis.com/oauth2/v3/certs",
    ],
)
def test_validate_jwks_url_공개IdP는통과한다(url: str) -> None:
    # Arrange / Act / Assert
    oidc.validate_jwks_url(url)


def test_JwksCache_생성시점에URL을검증한다() -> None:
    """설정 저장 시점에 막지 못했더라도 캐시 생성에서 걸러야 한다."""
    # Arrange / Act / Assert
    with pytest.raises(errors.InvalidRequestError):
        oidc.JwksCache(
            "https://169.254.169.254/jwks.json",
            clock_source=clock.SYSTEM_CLOCK,
            logger=oidc.observability.create_logger(
                service_name="test", level="CRITICAL"
            ),
        )
