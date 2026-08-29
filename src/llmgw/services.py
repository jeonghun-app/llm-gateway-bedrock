"""서비스 컨테이너와 FastAPI 의존성.

boto3 클라이언트와 도메인 서비스는 프로세스 시작 시 한 번만 만들어
`app.state` 에 담는다. 요청마다 클라이언트를 만들면 자격증명 해석과 TLS
핸드셰이크가 반복되어 지연이 크게 늘어난다.

라우터는 `ServicesDep` 를 통해서만 이 컨테이너를 얻는다. 테스트에서는
`build_services` 를 쓰지 않고 컨테이너를 직접 조립해 주입할 수 있다.
"""

from __future__ import annotations

import dataclasses
import typing

import fastapi

from llmgw import analytics as analytics_module
from llmgw import auth as auth_module
from llmgw import bedrock as bedrock_module
from llmgw import clock as clock_module
from llmgw import config
from llmgw import domain
from llmgw import errors
from llmgw import observability
from llmgw import oidc as oidc_module
from llmgw import pricing as pricing_module
from llmgw import repository
from llmgw import usage as usage_module

_ADMIN_TOKEN_HEADER = "X-Admin-Token"
_BEARER_PREFIX = "bearer "
# 공유 관리 토큰은 사람을 특정할 수 없다. 감사 로그에서 OIDC 주체와 구분되게
# 고정 라벨을 쓴다.
_SHARED_TOKEN_SUBJECT = "shared-admin-token"


@dataclasses.dataclass(frozen=True)
class Services:
    """애플리케이션 전역 의존성 묶음.

    Attributes:
        settings: 런타임 설정.
        logger: 구조화 로거.
        metrics: EMF 메트릭 발행기.
        registry: 레지스트리 저장소.
        usage_store: 사용량 저장소.
        pricing: 단가 표.
        authenticator: API 키 인증기.
        recorder: 사용량 기록기.
        analytics: 집계 조회 서비스.
        bedrock: Bedrock 어댑터.
        clock: 시각 제공자.
        id_factory: 식별자 생성기.
        oidc: 외부 인증 토큰 검증기.
    """

    settings: config.Settings
    logger: observability.Logger
    metrics: observability.MetricsEmitter
    registry: repository.RegistryRepository
    usage_store: repository.UsageStore
    pricing: pricing_module.PricingTable
    authenticator: auth_module.Authenticator
    recorder: usage_module.UsageRecorder
    analytics: analytics_module.AnalyticsService
    bedrock: bedrock_module.BedrockGateway
    clock: clock_module.Clock
    id_factory: clock_module.IdFactory
    oidc: oidc_module.OidcVerifier


def build_services(settings: config.Settings) -> Services:
    """설정으로부터 모든 의존성을 조립한다.

    Args:
        settings: 런타임 설정.

    Returns:
        조립된 서비스 컨테이너.

    Raises:
        FileNotFoundError: 단가 파일이 없는 경우.
        ValueError: 단가 파일 형식이 잘못된 경우. 잘못된 단가로 조용히
            0원 집계를 만드는 것보다 시작을 실패시키는 편이 안전하다.
    """
    logger = observability.create_logger(
        service_name=settings.service_name, level=settings.log_level
    )
    metrics = observability.MetricsEmitter(
        namespace=settings.metrics_namespace,
        environment=settings.env,
        logger=logger,
    )

    dynamodb = repository.create_dynamodb_resource(settings.aws_region)
    dynamodb_client = repository.create_dynamodb_client(settings.aws_region)
    registry = repository.RegistryRepository(
        dynamodb.Table(settings.registry_table),
        client=dynamodb_client,
    )
    usage_store = repository.UsageStore(
        usage_table=dynamodb.Table(settings.usage_table),
        agg_table=dynamodb.Table(settings.usage_agg_table),
        client=dynamodb_client,
        usage_ttl_days=settings.usage_ttl_days,
    )

    oidc_verifier = oidc_module.OidcVerifier(
        registry=registry,
        settings=settings,
        logger=logger,
        clock_source=clock_module.SYSTEM_CLOCK,
    )

    pricing_table = pricing_module.PricingTable.from_file(settings.pricing_file)
    control_client, runtime_client = bedrock_module.create_clients(
        region=settings.effective_bedrock_region,
        timeout_seconds=settings.request_timeout_seconds,
    )

    logger.info(
        "서비스 초기화 완료",
        extra={
            "env": settings.env,
            "aws_region": settings.aws_region,
            "bedrock_region": settings.effective_bedrock_region,
            "registry_table": settings.registry_table,
            "priced_models": len(pricing_table.known_model_ids()),
            "admin_api_enabled": bool(settings.admin_token),
        },
    )

    return Services(
        settings=settings,
        logger=logger,
        metrics=metrics,
        registry=registry,
        usage_store=usage_store,
        pricing=pricing_table,
        authenticator=auth_module.Authenticator(
            registry=registry,
            usage_store=usage_store,
            settings=settings,
            oidc_verifier=oidc_verifier,
            clock_source=clock_module.SYSTEM_CLOCK,
        ),
        recorder=usage_module.UsageRecorder(
            usage_store=usage_store,
            registry=registry,
            pricing_table=pricing_table,
            metrics=metrics,
            logger=logger,
            id_factory=clock_module.UUID_ID_FACTORY,
        ),
        analytics=analytics_module.AnalyticsService(
            usage_store=usage_store, registry=registry
        ),
        bedrock=bedrock_module.BedrockGateway(
            control_client=control_client,
            runtime_client=runtime_client,
            logger=logger,
        ),
        clock=clock_module.SYSTEM_CLOCK,
        id_factory=clock_module.UUID_ID_FACTORY,
        oidc=oidc_verifier,
    )


def get_services(request: fastapi.Request) -> Services:
    """요청에서 서비스 컨테이너를 꺼낸다.

    Args:
        request: 현재 요청.

    Returns:
        앱 상태에 저장된 서비스 컨테이너.

    Raises:
        GatewayError: 컨테이너가 초기화되지 않은 경우. 정상 기동 경로에서는
            발생하지 않는다.
    """
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise errors.GatewayError("서비스가 초기화되지 않았다.")
    return typing.cast("Services", services)


ServicesDep = typing.Annotated[Services, fastapi.Depends(get_services)]


def require_admin(
    request: fastapi.Request,
    services: ServicesDep,
    x_admin_token: typing.Annotated[
        str | None, fastapi.Header(alias=_ADMIN_TOKEN_HEADER)
    ] = None,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> Services:
    """관리 API 접근 권한을 확인하고 권한 범위를 강제한다.

    두 가지 자격증명을 받는다.

    - `X-Admin-Token`: 공유 관리 토큰. 항상 플랫폼 범위다. 부트스트랩과
      비상 접근(break-glass)용으로 남긴다.
    - `Authorization: Bearer <jwt>`: 고객 IdP 토큰. 토큰의 그룹이 계정
      설정의 `admin_groups` 에 있으면 그 계정만, 전역 설정의 플랫폼 관리자
      그룹에 있으면 모든 계정을 관리한다.

    범위 강제를 이 한 곳에서 하는 이유는 관리 엔드포인트가 23개인데 각
    엔드포인트에서 검사하면 새 엔드포인트가 검사를 빠뜨릴 수 있기 때문이다.
    경로의 `account_id` 를 읽어 확인하므로, 계정을 경로로 받는 모든
    엔드포인트가 자동으로 보호된다.

    계정 생성은 경로에 계정이 없고 본문으로 받으므로 플랫폼 범위만 허용한다.

    Args:
        request: 현재 요청. 경로 파라미터와 주체 저장에 쓴다.
        services: 서비스 컨테이너.
        x_admin_token: `X-Admin-Token` 헤더 값.
        authorization: `Bearer <jwt>` 헤더 값.

    Returns:
        검증을 통과한 서비스 컨테이너.

    Raises:
        AdminNotConfiguredError: 공유 토큰도 없고 OIDC 도 구성되지 않은 경우.
        AuthenticationError: 자격증명이 없거나 유효하지 않은 경우.
        PermissionDeniedError: 권한 범위를 벗어난 계정을 다루려는 경우.
    """
    principal = _resolve_admin_principal(
        services, x_admin_token=x_admin_token, authorization=authorization
    )
    _enforce_admin_scope(request, principal)
    # 감사에 쓰도록 주체를 요청에 남긴다.
    request.state.admin_principal = principal
    return services


def _resolve_admin_principal(
    services: Services,
    *,
    x_admin_token: str | None,
    authorization: str | None,
) -> domain.AdminPrincipal:
    """제시된 자격증명으로 관리 주체를 만든다.

    공유 토큰을 먼저 본다. 값이 왔다면 그것으로 판정하고 JWT 로 넘어가지
    않는다. 두 자격증명을 동시에 보내 더 넓은 권한을 얻는 경로를 만들지
    않기 위해서다.
    """
    if x_admin_token:
        auth_module.verify_admin_token(
            x_admin_token, services.settings.admin_token
        )
        return domain.AdminPrincipal(
            kind=domain.AdminAuthKind.SHARED_TOKEN,
            subject=_SHARED_TOKEN_SUBJECT,
            scope=domain.AdminScope.PLATFORM,
        )

    token = _bearer_token(authorization)
    if token:
        identity = services.oidc.verify(token)
        principal = identity.to_admin_principal()
        services.logger.info(
            "관리 API 인증 성공",
            extra={
                "admin_subject": principal.subject,
                "admin_scope": principal.scope.value,
                "admin_account_id": principal.account_id,
            },
        )
        return principal

    # 자격증명이 아예 없다. 관리 토큰이 설정돼 있지 않으면 그 사실을
    # 알려주는 편이 운영에 도움이 된다.
    if not services.settings.admin_token:
        raise errors.AdminNotConfiguredError(
            "관리 토큰이 설정되지 않아 관리 API를 사용할 수 없다."
        )
    raise errors.AuthenticationError("관리 토큰이 유효하지 않다.")


def _enforce_admin_scope(
    request: fastapi.Request, principal: domain.AdminPrincipal
) -> None:
    """경로의 계정을 다룰 권한이 있는지 확인한다."""
    account_id = request.path_params.get("account_id")
    if isinstance(account_id, str) and account_id:
        if not principal.can_manage(account_id):
            raise errors.PermissionDeniedError(
                f"이 계정을 관리할 권한이 없다: {account_id}"
            )
        return

    # 경로에 계정이 없는 요청. 계정 생성은 본문으로 계정을 받으므로 경로
    # 기반 검사가 통하지 않는다. 플랫폼 범위만 허용한다.
    if (
        request.method == "POST"
        and request.url.path.rstrip("/").endswith("/accounts")
        and principal.scope is not domain.AdminScope.PLATFORM
    ):
        raise errors.PermissionDeniedError(
            "계정 생성은 플랫폼 관리자만 할 수 있다."
        )


def _bearer_token(authorization: str | None) -> str:
    """`Authorization` 헤더에서 베어러 토큰을 꺼낸다. 없으면 빈 문자열."""
    if not authorization:
        return ""
    if not authorization.lower().startswith(_BEARER_PREFIX):
        return ""
    return authorization[len(_BEARER_PREFIX) :].strip()


def get_admin_principal(request: fastapi.Request) -> domain.AdminPrincipal:
    """현재 요청의 관리 주체를 반환한다.

    `require_admin` 이 먼저 실행돼야 한다. 목록 조회처럼 권한 범위에 따라
    결과를 좁혀야 하는 엔드포인트가 쓴다.

    Args:
        request: 현재 요청.

    Returns:
        해석된 관리 주체.

    Raises:
        GatewayError: 인증 의존성이 실행되지 않은 경우. 정상 경로에서는
            발생하지 않는다.
    """
    principal = getattr(request.state, "admin_principal", None)
    if principal is None:
        raise errors.GatewayError("관리 주체가 해석되지 않았다.")
    return typing.cast("domain.AdminPrincipal", principal)


AdminDep = typing.Annotated[Services, fastapi.Depends(require_admin)]
AdminPrincipalDep = typing.Annotated[
    domain.AdminPrincipal, fastapi.Depends(get_admin_principal)
]
