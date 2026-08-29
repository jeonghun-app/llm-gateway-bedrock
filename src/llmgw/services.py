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
from llmgw import errors
from llmgw import observability
from llmgw import pricing as pricing_module
from llmgw import repository
from llmgw import usage as usage_module

_ADMIN_TOKEN_HEADER = "X-Admin-Token"


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
    services: ServicesDep,
    x_admin_token: typing.Annotated[
        str | None, fastapi.Header(alias=_ADMIN_TOKEN_HEADER)
    ] = None,
) -> Services:
    """관리 API 접근 권한을 확인한다.

    Args:
        services: 서비스 컨테이너.
        x_admin_token: `X-Admin-Token` 헤더 값.

    Returns:
        검증을 통과한 서비스 컨테이너.

    Raises:
        AdminNotConfiguredError: 서버에 관리 토큰이 설정되지 않은 경우.
        AuthenticationError: 토큰이 없거나 일치하지 않는 경우.
    """
    auth_module.verify_admin_token(x_admin_token, services.settings.admin_token)
    return services


AdminDep = typing.Annotated[Services, fastapi.Depends(require_admin)]
