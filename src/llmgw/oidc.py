"""OIDC(OpenAI 호환 게이트웨이의 외부 인증) 액세스 토큰 검증.

기업과 개인이 이미 쓰는 인증 서버(Amazon Cognito, Okta, Azure AD, Google 등)를
그대로 붙일 수 있게 한다. 클라이언트는 IdP 에서 받은 액세스 토큰을
`Authorization: Bearer <jwt>` 로 그대로 보내고, 게이트웨이가 서명을 검증해
호출 주체를 만든다. API 키를 사람이 나눠 주는 절차가 사라진다.

검증 정책
--------
- 서명은 IdP 의 JWKS 로 확인한다. 대칭키 알고리즘(HS*)과 `none` 은 받지
  않는다. 공유 비밀을 두지 않기 때문이고, `alg` 를 바꿔치기하는 공격을
  막는다.
- `iss` 는 설정값과 정확히 일치해야 한다.
- 청중(audience)은 `aud` 를 본다. Cognito **액세스 토큰**은 `aud` 가 없고
  `client_id` 에 클라이언트 ID 를 담으므로 그쪽도 확인한다.
- `exp`/`nbf`/`iat` 를 검증한다.
- 발급자가 설정되지 않았으면 JWT 인증을 아예 비활성화한다. 미설정을
  "누구나 통과" 로 해석하면 게이트웨이가 무인증으로 열린다.

JWKS 는 TTL 캐시로 들고 있고, 처음 보는 `kid` 가 오면 한 번 갱신한다. 키
회전 직후 요청이 실패하는 구간을 없애기 위해서다.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import socket
import threading
import typing
import urllib.error
import urllib.parse
import urllib.request

import jwt
from jwt import algorithms as jwt_algorithms

from llmgw import cache
from llmgw import clock
from llmgw import config
from llmgw import domain
from llmgw import errors
from llmgw import observability
from llmgw import repository

# 비대칭 서명만 허용한다.
_ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")

_JWKS_TTL_SECONDS = 600.0
_JWKS_TIMEOUT_SECONDS = 5.0
# 키 회전 폭주를 막는 최소 갱신 간격.
_JWKS_MIN_REFRESH_SECONDS = 30.0

# JWKS URL 은 관리자가 설정하는 값이다. 검증 없이 그대로 호출하면 게이트웨이가
# 내부망으로 요청을 보내는 SSRF 통로가 된다. Fargate 에서는
# 169.254.170.2 가 태스크 역할 자격증명을 서빙하므로 특히 위험하다.
# 그래서 https 만 허용하고, 해석된 IP 가 사설·링크로컬·루프백이면 거부한다.
_ALLOWED_JWKS_SCHEMES = ("https",)


def validate_jwks_url(url: str) -> None:
    """JWKS URL 이 외부 공개 HTTPS 주소인지 확인한다.

    호스트명을 실제로 해석해 결과 IP 를 검사한다. 이름만 보면
    `metadata.example.com` 이 링크로컬로 해석되는 우회를 막을 수 없다.

    Args:
        url: 검사할 URL.

    Raises:
        InvalidRequestError: 스킴이 https 가 아니거나, 호스트가 없거나,
            해석된 주소가 내부 대역인 경우.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_JWKS_SCHEMES:
        raise errors.InvalidRequestError(
            "JWKS URL 은 https 여야 한다. 받은 스킴:"
            f" {parsed.scheme or '(없음)'}"
        )
    host = parsed.hostname
    if not host:
        raise errors.InvalidRequestError("JWKS URL 에 호스트가 없다.")

    try:
        resolved = socket.getaddrinfo(host, parsed.port or 443)
    except socket.gaierror as exc:
        raise errors.InvalidRequestError(
            f"JWKS URL 의 호스트를 해석할 수 없다: {host}"
        ) from exc

    for entry in resolved:
        address = entry[4][0]
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed_ip.is_private
            or parsed_ip.is_loopback
            or parsed_ip.is_link_local
            or parsed_ip.is_reserved
            or parsed_ip.is_multicast
            or parsed_ip.is_unspecified
        ):
            # 어떤 주소로 해석됐는지 알려주면 내부망 스캔에 쓰일 수 있으므로
            # 주소는 응답에 넣지 않는다.
            raise errors.InvalidRequestError(
                "JWKS URL 이 내부 네트워크 주소로 해석된다. 외부에서 접근"
                f" 가능한 인증 서버 주소를 지정한다: {host}"
            )


@dataclasses.dataclass(frozen=True)
class VerifiedIdentity:
    """검증이 끝난 외부 인증 주체.

    Attributes:
        subject: IdP 의 안정적 식별자(`sub`).
        account_id: 발급자로 판별한 계정 ID.
        team_id: 매핑된 팀 ID. 없으면 빈 문자열.
        user_id: 매핑된 사용자 ID.
        email: 이메일 클레임. 없으면 빈 문자열.
        display_name: 표시 이름. 없으면 `user_id` 를 쓴다.
        groups: 토큰에서 읽은 그룹 목록.
        is_platform_admin: 모든 계정을 관리할 수 있는지 여부.
        is_account_admin: 자기 계정을 관리할 수 있는지 여부.
        config: 검증에 사용한 계정 설정. 자동 생성 정책을 읽는 데 쓴다.
        claims: 검증된 전체 클레임. 감사에 쓴다.
    """

    subject: str
    account_id: str
    team_id: str
    user_id: str
    email: str
    display_name: str
    groups: tuple[str, ...]
    is_platform_admin: bool
    is_account_admin: bool
    config: domain.AccountAuthConfig
    claims: dict[str, typing.Any]

    @property
    def is_admin(self) -> bool:
        """관리 API 를 쓸 수 있는지 여부."""
        return self.is_platform_admin or self.is_account_admin

    def to_admin_principal(self) -> domain.AdminPrincipal:
        """관리 주체로 변환한다.

        Returns:
            권한 범위가 채워진 `AdminPrincipal`.

        Raises:
            PermissionDeniedError: 관리자가 아닌 경우.
        """
        if self.is_platform_admin:
            scope = domain.AdminScope.PLATFORM
        elif self.is_account_admin:
            scope = domain.AdminScope.ACCOUNT
        else:
            raise errors.PermissionDeniedError("이 토큰에는 관리 권한이 없다.")
        return domain.AdminPrincipal(
            kind=domain.AdminAuthKind.OIDC,
            subject=self.subject_label,
            scope=scope,
            account_id=self.account_id,
            groups=self.groups,
        )

    @property
    def subject_label(self) -> str:
        """감사 로그에 남길 주체 표시값."""
        return self.email or self.user_id or self.subject


class JwksCache:
    """JWKS 를 TTL 로 캐시한다.

    스레드 안전하다. 동기 핸들러가 스레드풀에서 실행되므로 여러 요청이
    동시에 들어온다.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        clock_source: clock.Clock,
        logger: observability.Logger,
        ttl_seconds: float = _JWKS_TTL_SECONDS,
    ) -> None:
        """캐시를 만든다.

        Args:
            jwks_url: JWKS 문서 URL.
            clock_source: 시간 출처.
            logger: 구조화 로거.
            ttl_seconds: 캐시 유효 기간(초).
        """
        validate_jwks_url(jwks_url)
        self._url = jwks_url
        self._clock = clock_source
        self._logger = logger
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._keys: dict[str, typing.Any] = {}
        self._fetched_at = 0.0

    def get_key(self, kid: str) -> typing.Any:
        """`kid` 에 해당하는 공개키를 반환한다.

        캐시에 없으면 한 번 갱신하고 다시 찾는다. 키 회전 직후를 위한
        경로다.

        Args:
            kid: JWT 헤더의 키 ID.

        Returns:
            서명 검증에 쓸 공개키 객체.

        Raises:
            AuthenticationError: 갱신 후에도 키를 찾을 수 없는 경우.
        """
        key = self._lookup(kid, allow_stale=False)
        if key is not None:
            return key
        self._refresh(force=False)
        key = self._lookup(kid, allow_stale=True)
        if key is None:
            raise errors.AuthenticationError(
                "토큰 서명 키를 인증 서버에서 찾을 수 없다."
            )
        return key

    # -- 내부 ---------------------------------------------------------------

    def _lookup(self, kid: str, *, allow_stale: bool) -> typing.Any:
        """캐시에서 키를 찾는다. 만료됐으면 갱신한다."""
        with self._lock:
            fresh = (
                self._clock.now().timestamp() - self._fetched_at
            ) < self._ttl and bool(self._keys)
            if fresh or allow_stale:
                return self._keys.get(kid)
        self._refresh(force=False)
        with self._lock:
            return self._keys.get(kid)

    def _refresh(self, *, force: bool) -> None:
        """JWKS 를 다시 받아 캐시를 채운다."""
        with self._lock:
            elapsed = self._clock.now().timestamp() - self._fetched_at
            if not force and self._keys and elapsed < _JWKS_MIN_REFRESH_SECONDS:
                return
        document = self._fetch()
        parsed: dict[str, typing.Any] = {}
        for entry in document.get("keys", []):
            kid = str(entry.get("kid") or "")
            kty = str(entry.get("kty") or "")
            if not kid:
                continue
            try:
                if kty == "RSA":
                    parsed[kid] = jwt_algorithms.RSAAlgorithm.from_jwk(entry)
                elif kty == "EC":
                    parsed[kid] = jwt_algorithms.ECAlgorithm.from_jwk(entry)
            except (ValueError, TypeError, KeyError):
                # 한 키가 깨져도 나머지는 쓸 수 있어야 한다.
                self._logger.warning(
                    "JWKS 항목을 해석할 수 없다", extra={"kid": kid}
                )
        if not parsed:
            raise errors.AuthenticationError(
                "인증 서버의 서명 키 목록이 비어 있거나 해석할 수 없다."
            )
        with self._lock:
            self._keys = parsed
            self._fetched_at = self._clock.now().timestamp()

    def _fetch(self) -> dict[str, typing.Any]:
        """JWKS 문서를 가져온다.

        요청 직전에 URL 을 다시 검증한다. 생성 시점에만 확인하면 DNS 응답이
        나중에 내부 주소로 바뀌는 리바인딩을 막을 수 없다.
        """
        validate_jwks_url(self._url)
        request = urllib.request.Request(  # noqa: S310 - 설정된 https URL
            self._url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - 설정된 https URL
                request, timeout=_JWKS_TIMEOUT_SECONDS
            ) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._logger.exception("JWKS 조회에 실패했다")
            raise errors.AuthenticationError(
                "인증 서버의 서명 키를 가져올 수 없다."
            ) from exc
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise errors.AuthenticationError(
                "인증 서버가 반환한 서명 키 형식이 올바르지 않다."
            )
        return decoded


class OidcVerifier:
    """계정별 설정으로 OIDC 액세스 토큰을 검증한다.

    발급자(`iss`)로 어느 계정의 토큰인지 판별하고, 그 계정에 등록된 설정만
    사용한다. 전역 설정을 두지 않는 이유는 두 곳에 같은 값이 있으면 어느
    쪽이 적용됐는지 추적하기 어렵고 한쪽만 고쳐 사고가 나기 때문이다.

    발급자가 등록되지 않았으면 거부한다. 미등록을 통과로 해석하면 아무 IdP
    가 발급한 토큰이나 받아들이게 된다.
    """

    def __init__(
        self,
        *,
        registry: repository.RegistryRepository,
        settings: config.Settings,
        logger: observability.Logger,
        clock_source: clock.Clock,
        config_cache: cache.TtlCache[typing.Any] | None = None,
        jwks_factory: typing.Callable[[str], JwksCache] | None = None,
    ) -> None:
        """검증기를 만든다.

        Args:
            registry: 계정 설정을 읽을 레지스트리 저장소.
            settings: 런타임 설정. 플랫폼 관리자 그룹을 읽는다.
            logger: 구조화 로거.
            clock_source: 시간 출처.
            config_cache: 발급자→설정 캐시. 요청마다 DynamoDB 를 두 번 읽지
                않기 위해 쓴다.
            jwks_factory: JWKS 캐시 생성기. 테스트가 네트워크를 타지 않는
                대역 객체를 넣는 데 쓴다.
        """
        self._registry = registry
        self._settings = settings
        self._logger = logger
        self._clock = clock_source
        self._config_cache: cache.TtlCache[typing.Any] = (
            config_cache or cache.TtlCache(time_source=clock_source)
        )
        self._jwks_factory = jwks_factory or self._default_jwks_factory
        self._jwks_lock = threading.Lock()
        self._jwks_by_url: dict[str, JwksCache] = {}

    def verify(self, token: str) -> VerifiedIdentity:
        """토큰을 검증하고 매핑된 주체를 반환한다.

        Args:
            token: `Authorization: Bearer` 로 받은 JWT.

        Returns:
            검증된 주체.

        Raises:
            AuthenticationError: 토큰 형식이 틀렸거나 서명·발급자·청중이
                유효하지 않은 경우, 또는 발급자가 등록되지 않은 경우.
            PermissionDeniedError: 계정 설정이 비활성인 경우.
        """
        issuer = self._unverified_issuer(token)
        account_config = self._config_for(issuer)
        if account_config.status is not domain.EntityStatus.ACTIVE:
            raise errors.PermissionDeniedError(
                "이 계정의 외부 인증이 비활성 상태다."
            )
        claims = self._decode(token, account_config)
        return self._map_identity(claims, account_config)

    # -- 내부 ---------------------------------------------------------------

    def _default_jwks_factory(self, url: str) -> JwksCache:
        """실제 네트워크를 쓰는 JWKS 캐시를 만든다."""
        return JwksCache(url, clock_source=self._clock, logger=self._logger)

    def _jwks_for(self, url: str) -> JwksCache:
        """URL 별 JWKS 캐시를 재사용한다."""
        with self._jwks_lock:
            existing = self._jwks_by_url.get(url)
            if existing is not None:
                return existing
            created = self._jwks_factory(url)
            self._jwks_by_url[url] = created
            return created

    @staticmethod
    def _unverified_issuer(token: str) -> str:
        """서명 검증 전에 발급자만 읽는다.

        어느 계정 설정으로 검증할지 알아내려면 발급자를 먼저 봐야 한다. 이
        값은 라우팅에만 쓰고, 실제 신뢰는 서명 검증 이후에 부여한다.
        """
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않다."
            ) from exc
        issuer = unverified.get("iss")
        if not isinstance(issuer, str) or not issuer:
            raise errors.AuthenticationError("자격증명이 유효하지 않다.")
        return issuer

    def _config_for(self, issuer: str) -> domain.AccountAuthConfig:
        """발급자로 계정 설정을 찾는다. 캐시를 거친다."""
        cached = self._config_cache.get_or_load(
            f"oidc:{issuer}", lambda: self._load_config(issuer)
        )
        if cached is None:
            raise errors.AuthenticationError("자격증명이 유효하지 않다.")
        return typing.cast("domain.AccountAuthConfig", cached)

    def _load_config(self, issuer: str) -> domain.AccountAuthConfig | None:
        """레지스트리에서 발급자에 해당하는 설정을 읽는다."""
        account_id = self._registry.find_account_by_issuer(issuer)
        if not account_id:
            self._logger.warning(
                "등록되지 않은 발급자의 토큰이다",
                extra={"issuer": issuer},
            )
            return None
        return self._registry.get_auth_config(account_id)

    def _decode(
        self, token: str, account_config: domain.AccountAuthConfig
    ) -> dict[str, typing.Any]:
        """서명과 표준 클레임을 검증한다."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않다."
            ) from exc

        algorithm = str(header.get("alg") or "")
        if algorithm not in _ALLOWED_ALGORITHMS:
            # HS* 와 none 을 여기서 끊는다. 허용하면 alg 바꿔치기가 통한다.
            self._logger.warning(
                "허용되지 않은 토큰 서명 알고리즘이다",
                extra={"algorithm": algorithm or "none"},
            )
            raise errors.AuthenticationError("자격증명이 유효하지 않다.")
        kid = str(header.get("kid") or "")
        if not kid:
            raise errors.AuthenticationError("자격증명이 유효하지 않다.")

        jwks = self._jwks_for(account_config.effective_jwks_url)
        key = jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=account_config.issuer,
                # 청중은 아래에서 직접 본다. Cognito 액세스 토큰은 aud 가
                # 없고 client_id 를 쓰기 때문에 라이브러리 검증만으로는 두
                # 형태를 모두 다룰 수 없다.
                options={
                    "verify_aud": False,
                    "require": ["exp", "iat", "iss", "sub"],
                },
            )
        except jwt.PyJWTError as exc:
            # 만료·서명 불일치·발급자 불일치를 구분해 알려주면 유효한 토큰을
            # 찾는 데 단서가 된다. 원인은 로그에만 남긴다.
            self._logger.warning(
                "토큰 검증에 실패했다",
                extra={"reason": type(exc).__name__},
            )
            raise errors.AuthenticationError(
                "자격증명이 유효하지 않다."
            ) from exc

        self._verify_audience(claims, account_config)
        return dict(claims)

    def _verify_audience(
        self,
        claims: dict[str, typing.Any],
        account_config: domain.AccountAuthConfig,
    ) -> None:
        """청중을 확인한다.

        설정이 비어 있으면 검사하지 않는다. 단일 클라이언트만 쓰는 환경에
        불필요한 설정을 강요하지 않기 위해서다. 여러 클라이언트를 쓰거나
        토큰이 다른 앱에서도 발급된다면 반드시 지정해야 한다.
        """
        expected = account_config.audience_list
        if not expected:
            return
        presented: set[str] = set()
        raw_aud = claims.get("aud")
        if isinstance(raw_aud, str):
            presented.add(raw_aud)
        elif isinstance(raw_aud, list):
            presented.update(str(item) for item in raw_aud)
        # Cognito 액세스 토큰 경로.
        client_id = claims.get("client_id")
        if isinstance(client_id, str):
            presented.add(client_id)
        if not presented & set(expected):
            self._logger.warning(
                "토큰의 대상 클라이언트가 허용 목록에 없다",
                extra={"account_id": account_config.account_id},
            )
            raise errors.AuthenticationError("자격증명이 유효하지 않다.")

    def _map_identity(
        self,
        claims: dict[str, typing.Any],
        account_config: domain.AccountAuthConfig,
    ) -> VerifiedIdentity:
        """클레임을 계정/팀/사용자와 관리 권한으로 매핑한다."""
        subject = str(claims.get("sub") or "")
        user_id = self._claim(claims, account_config.user_claim) or subject
        if not user_id:
            raise errors.PermissionDeniedError(
                "토큰에서 사용자를 결정할 수 없다."
            )
        team_id = self._claim(claims, account_config.team_claim)
        email = self._claim(claims, "email")
        name = self._claim(claims, "name") or email or user_id

        groups = self._groups(claims, account_config.groups_claim)
        group_set = set(groups)
        platform_groups = set(self._settings.oidc_platform_admin_group_list)
        account_groups = set(account_config.admin_group_list)
        return VerifiedIdentity(
            subject=subject,
            account_id=account_config.account_id,
            team_id=team_id,
            user_id=user_id,
            email=email,
            display_name=name,
            groups=groups,
            is_platform_admin=bool(
                platform_groups and group_set & platform_groups
            ),
            is_account_admin=bool(
                account_groups and group_set & account_groups
            ),
            config=account_config,
            claims=claims,
        )

    @staticmethod
    def _groups(
        claims: dict[str, typing.Any], claim_name: str
    ) -> tuple[str, ...]:
        """그룹 클레임을 튜플로 꺼낸다.

        공급자마다 형태가 다르다. Cognito 는 문자열 배열, 일부 IdP 는 공백
        구분 문자열을 쓴다.
        """
        if not claim_name:
            return ()
        value = claims.get(claim_name)
        if isinstance(value, list):
            return tuple(
                str(item).strip() for item in value if str(item).strip()
            )
        if isinstance(value, str):
            separator = "," if "," in value else None
            return tuple(
                item.strip() for item in value.split(separator) if item.strip()
            )
        return ()

    @staticmethod
    def _claim(claims: dict[str, typing.Any], name: str) -> str:
        """클레임을 문자열로 꺼낸다. 없으면 빈 문자열."""
        if not name:
            return ""
        value = claims.get(name)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, list) and value:
            first = value[0]
            return str(first).strip() if isinstance(first, str) else ""
        return ""
