"""공용 픽스처.

유닛 테스트는 실제 AWS 를 호출하지 않는다. DynamoDB 는 `moto` 로 모킹하고,
Bedrock 은 `botocore.stub.Stubber` 또는 테스트용 대역으로 대체한다.

`moto` 가 실수로 실제 자격증명을 집어 진짜 계정에 쓰는 것을 막기 위해,
모듈 로드 시점에 더미 자격증명과 리전을 환경변수로 못박는다.
"""

from __future__ import annotations

import datetime
import decimal
import os
import typing

# moto 를 import 하기 전에 설정해야 효과가 있다.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3  # noqa: E402
from fastapi import testclient  # noqa: E402
import moto  # noqa: E402
import pytest  # noqa: E402

from llmgw import analytics  # noqa: E402
from llmgw import apikey  # noqa: E402
from llmgw import app  # noqa: E402
from llmgw import auth  # noqa: E402
from llmgw import bedrock  # noqa: E402
from llmgw import config  # noqa: E402
from llmgw import domain  # noqa: E402
from llmgw import observability  # noqa: E402
from llmgw import pricing  # noqa: E402
from llmgw import repository  # noqa: E402
from llmgw import services  # noqa: E402
from llmgw import usage  # noqa: E402

TEST_REGION = "us-east-1"
REGISTRY_TABLE = "llmgw-test-registry"
USAGE_TABLE = "llmgw-test-usage"
USAGE_AGG_TABLE = "llmgw-test-usage-agg"

FIXED_NOW = datetime.datetime(2026, 8, 23, 12, 0, 0, tzinfo=datetime.UTC)


class FrozenClock:
    """테스트에서 시간을 고정하는 시계.

    `sleep` 없이 시간 경과를 시뮬레이션하기 위해 `advance` 를 제공한다.
    """

    def __init__(self, start: datetime.datetime = FIXED_NOW) -> None:
        """시계를 만든다.

        Args:
            start: 초기 시각.
        """
        self._now = start

    def now(self) -> datetime.datetime:
        """현재 고정된 시각을 반환한다."""
        return self._now

    def advance(self, seconds: float) -> None:
        """시각을 앞으로 이동한다.

        Args:
            seconds: 이동할 초.
        """
        self._now = self._now + datetime.timedelta(seconds=seconds)


class SequenceIdFactory:
    """예측 가능한 식별자를 만드는 팩토리."""

    def __init__(self, prefix: str = "id") -> None:
        """팩토리를 만든다.

        Args:
            prefix: 생성될 ID 의 접두어.
        """
        self._prefix = prefix
        self._counter = 0

    def new_id(self) -> str:
        """`<prefix>-<순번>` 형태의 ID 를 반환한다."""
        self._counter += 1
        return f"{self._prefix}-{self._counter}"


def create_tables(dynamodb: typing.Any) -> None:
    """테스트용 DynamoDB 테이블 3개를 만든다.

    스키마는 `infra/app.yaml` 의 정의와 일치해야 한다. 어긋나면 유닛
    테스트는 통과하지만 배포 환경에서 실패한다.

    Args:
        dynamodb: moto 로 모킹된 DynamoDB 리소스.
    """
    dynamodb.create_table(
        TableName=REGISTRY_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    dynamodb.create_table(
        TableName=USAGE_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
        LocalSecondaryIndexes=[
            {
                "IndexName": "lsi_ts",
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "ts", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    dynamodb.create_table(
        TableName=USAGE_AGG_TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    )


@pytest.fixture
def dynamodb() -> typing.Iterator[typing.Any]:
    """moto 로 모킹된 DynamoDB 리소스를 제공한다."""
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name=TEST_REGION)
        create_tables(resource)
        yield resource


@pytest.fixture
def settings() -> config.Settings:
    """테스트용 설정을 만든다."""
    return config.Settings(
        env="test",
        aws_region=TEST_REGION,
        registry_table=REGISTRY_TABLE,
        usage_table=USAGE_TABLE,
        usage_agg_table=USAGE_AGG_TABLE,
        admin_token="test-admin-token",
        usage_ttl_days=30,
    )


@pytest.fixture
def registry(dynamodb: typing.Any) -> repository.RegistryRepository:
    """레지스트리 저장소를 제공한다.

    키 재발급 트랜잭션을 위해 저수준 클라이언트도 함께 넘긴다. moto
    컨텍스트 안에서 만들어야 하므로 `dynamodb` 픽스처에 의존한다.
    """
    client = boto3.client("dynamodb", region_name=TEST_REGION)
    return repository.RegistryRepository(
        dynamodb.Table(REGISTRY_TABLE), client=client
    )


@pytest.fixture
def dynamodb_client(dynamodb: typing.Any) -> typing.Any:
    """저수준 DynamoDB 클라이언트를 제공한다.

    `dynamodb` 픽스처에 의존해 moto 컨텍스트 안에서 만들어진다.
    """
    del dynamodb  # moto 컨텍스트 활성화 목적의 의존성이다.
    return boto3.client("dynamodb", region_name=TEST_REGION)


@pytest.fixture
def usage_store(
    dynamodb: typing.Any, dynamodb_client: typing.Any
) -> repository.UsageStore:
    """사용량 저장소를 제공한다."""
    return repository.UsageStore(
        usage_table=dynamodb.Table(USAGE_TABLE),
        agg_table=dynamodb.Table(USAGE_AGG_TABLE),
        client=dynamodb_client,
        usage_ttl_days=30,
    )


@pytest.fixture
def pricing_table() -> pricing.PricingTable:
    """단가 표를 제공한다.

    실제 `pricing.json` 대신 계산 검증이 쉬운 값을 쓴다. 실제 파일 로딩은
    `test_pricing.py` 에서 별도로 검증한다.
    """
    return pricing.PricingTable(
        {
            "amazon.nova-lite-v1:0": pricing.ModelPrice(
                model_id="amazon.nova-lite-v1:0",
                input_per_1k_usd=decimal.Decimal("0.001"),
                output_per_1k_usd=decimal.Decimal("0.002"),
            ),
            "anthropic.claude-3-haiku-20240307-v1:0": pricing.ModelPrice(
                model_id="anthropic.claude-3-haiku-20240307-v1:0",
                input_per_1k_usd=decimal.Decimal("0.01"),
                output_per_1k_usd=decimal.Decimal("0.02"),
            ),
        }
    )


@pytest.fixture
def logger() -> observability.Logger:
    """테스트용 로거를 제공한다."""
    return observability.create_logger(
        service_name="llmgw-test", level="WARNING"
    )


@pytest.fixture
def metrics(logger: observability.Logger) -> observability.MetricsEmitter:
    """테스트용 메트릭 발행기를 제공한다."""
    return observability.MetricsEmitter(
        namespace="LLMGatewayTest", environment="test", logger=logger
    )


@pytest.fixture
def usage_recorder(
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
    pricing_table: pricing.PricingTable,
    metrics: observability.MetricsEmitter,
    logger: observability.Logger,
) -> usage.UsageRecorder:
    """사용량 기록기를 제공한다."""
    return usage.UsageRecorder(
        usage_store=usage_store,
        registry=registry,
        pricing_table=pricing_table,
        metrics=metrics,
        logger=logger,
    )


@pytest.fixture
def authenticator(
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
    settings: config.Settings,
) -> auth.Authenticator:
    """인증기를 제공한다. 캐시는 비활성화해 상태 누출을 막는다."""
    from llmgw import cache

    return auth.Authenticator(
        registry=registry,
        usage_store=usage_store,
        settings=settings,
        metadata_cache=cache.TtlCache(ttl_seconds=0),
    )


@pytest.fixture
def analytics_service(
    usage_store: repository.UsageStore,
    registry: repository.RegistryRepository,
) -> analytics.AnalyticsService:
    """집계 조회 서비스를 제공한다."""
    return analytics.AnalyticsService(
        usage_store=usage_store, registry=registry
    )


def make_usage_record(
    *,
    request_id: str = "req-1",
    account_id: str = "acme",
    team_id: str = "platform",
    user_id: str = "alice",
    key_id: str = "key-1",
    model_id: str = "amazon.nova-lite-v1:0",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cost_usd: str = "0.002",
    latency_ms: int = 120,
    status_code: int = 200,
    error_code: str = "",
    timestamp: str = "2026-08-23T12:00:00Z",
) -> domain.UsageRecord:
    """테스트용 사용량 레코드를 만든다.

    Args:
        request_id: 요청 ID.
        account_id: 계정 ID.
        team_id: 팀 ID.
        user_id: 사용자 ID.
        key_id: 키 ID.
        model_id: 모델 ID.
        input_tokens: 입력 토큰 수.
        output_tokens: 출력 토큰 수.
        cost_usd: 비용 문자열.
        latency_ms: 지연.
        status_code: HTTP 상태 코드.
        error_code: 에러 코드.
        timestamp: ISO-8601 시각.

    Returns:
        구성된 `UsageRecord`.
    """
    return domain.UsageRecord(
        request_id=request_id,
        timestamp=timestamp,
        account_id=account_id,
        team_id=team_id,
        user_id=user_id,
        key_id=key_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=decimal.Decimal(cost_usd),
        latency_ms=latency_ms,
        status_code=status_code,
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# HTTP 계층 픽스처
# ---------------------------------------------------------------------------

# 대역 Bedrock 이 돌려주는 고정 응답. 토큰 수를 12/5 로 잡아 비용 계산
# 결과가 테스트에서 눈으로 검증 가능한 값이 되게 했다.
FAKE_INPUT_TOKENS = 12
FAKE_OUTPUT_TOKENS = 5
FAKE_RESPONSE_TEXT = "안녕하세요"
FAKE_MODEL_IDS = (
    "amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0",
    "us.anthropic.claude-3-haiku-20240307-v1:0",
)


class FakeBedrock:
    """Bedrock 어댑터 대역.

    `raise_on_converse` / `raise_on_stream` 에 예외를 넣으면 해당 경로가
    실패한다. 실패 경로 테스트에서 실제 AWS 오류를 재현하기 위한 것이다.
    """

    def __init__(self) -> None:
        """대역을 만든다."""
        self.last_call: dict[str, typing.Any] | None = None
        self.raise_on_converse: Exception | None = None
        self.raise_on_stream: Exception | None = None
        self.model_ids: tuple[str, ...] = FAKE_MODEL_IDS

    def list_model_ids(self) -> tuple[str, ...]:
        """고정된 모델 목록을 반환한다."""
        return self.model_ids

    def converse(self, **params: typing.Any) -> bedrock.ConverseResult:
        """고정 응답을 반환하거나 설정된 예외를 던진다."""
        self.last_call = params
        if self.raise_on_converse is not None:
            raise self.raise_on_converse
        return bedrock.ConverseResult(
            text=FAKE_RESPONSE_TEXT,
            stop_reason="end_turn",
            input_tokens=FAKE_INPUT_TOKENS,
            output_tokens=FAKE_OUTPUT_TOKENS,
        )

    def converse_stream(
        self, **params: typing.Any
    ) -> typing.Iterator[bedrock.StreamDelta]:
        """텍스트를 한 글자씩 흘려보낸다."""
        self.last_call = params
        if self.raise_on_stream is not None:
            raise self.raise_on_stream
        for character in FAKE_RESPONSE_TEXT:
            yield bedrock.StreamDelta(text=character)
        yield bedrock.StreamDelta(stop_reason="end_turn")
        yield bedrock.StreamDelta(
            input_tokens=FAKE_INPUT_TOKENS,
            output_tokens=FAKE_OUTPUT_TOKENS,
            is_final=True,
        )


def seed_account_tree(
    registry_repo: repository.RegistryRepository,
    *,
    account_id: str = "acme",
    team_id: str = "platform",
    user_id: str = "alice",
) -> None:
    """계정·팀·사용자를 심는다. 이미 있으면 무시한다.

    Args:
        registry_repo: 레지스트리 저장소.
        account_id: 계정 ID.
        team_id: 팀 ID.
        user_id: 사용자 ID.
    """
    registry_repo.put_account(
        domain.Account(account_id=account_id, name="Acme Inc."),
        overwrite=True,
    )
    registry_repo.put_team(
        domain.Team(account_id=account_id, team_id=team_id, name="플랫폼팀"),
        overwrite=True,
    )
    registry_repo.put_user(
        domain.User(
            account_id=account_id,
            user_id=user_id,
            name="앨리스",
            email="alice@example.com",
            team_id=team_id,
        ),
        overwrite=True,
    )


def seed_api_key(
    registry_repo: repository.RegistryRepository,
    *,
    key_id: str = "key-1",
    account_id: str = "acme",
    team_id: str = "platform",
    user_id: str = "alice",
    allowed_models: tuple[str, ...] = (),
    monthly_budget_usd: decimal.Decimal | None = None,
    status: domain.EntityStatus = domain.EntityStatus.ACTIVE,
) -> str:
    """계정 트리와 API 키를 심고 평문 키를 반환한다.

    Args:
        registry_repo: 레지스트리 저장소.
        key_id: 키 ID.
        account_id: 계정 ID.
        team_id: 팀 ID.
        user_id: 사용자 ID.
        allowed_models: 허용 모델 목록.
        monthly_budget_usd: 키 월 예산.
        status: 키 활성 상태.

    Returns:
        평문 API 키.
    """
    seed_account_tree(
        registry_repo,
        account_id=account_id,
        team_id=team_id,
        user_id=user_id,
    )
    generated = apikey.generate_api_key("test")
    registry_repo.put_api_key(
        domain.ApiKey(
            key_id=key_id,
            key_hash=generated.key_hash,
            key_prefix=generated.key_prefix,
            account_id=account_id,
            team_id=team_id,
            user_id=user_id,
            name=f"테스트 키 {key_id}",
            allowed_models=allowed_models,
            monthly_budget_usd=monthly_budget_usd,
            status=status,
        )
    )
    return generated.plaintext


@pytest.fixture
def fake_bedrock() -> FakeBedrock:
    """Bedrock 대역을 제공한다."""
    return FakeBedrock()


@pytest.fixture
def frozen_clock() -> FrozenClock:
    """고정 시계를 제공한다."""
    return FrozenClock()


@pytest.fixture
def app_services(
    settings: config.Settings,
    registry: repository.RegistryRepository,
    usage_store: repository.UsageStore,
    pricing_table: pricing.PricingTable,
    logger: observability.Logger,
    metrics: observability.MetricsEmitter,
    authenticator: auth.Authenticator,
    analytics_service: analytics.AnalyticsService,
    usage_recorder: usage.UsageRecorder,
    fake_bedrock: FakeBedrock,
    frozen_clock: FrozenClock,
) -> services.Services:
    """실제 서비스 컨테이너를 조립한다. Bedrock 과 시계만 대역이다."""
    return services.Services(
        settings=settings,
        logger=logger,
        metrics=metrics,
        registry=registry,
        usage_store=usage_store,
        pricing=pricing_table,
        authenticator=authenticator,
        recorder=usage_recorder,
        analytics=analytics_service,
        bedrock=typing.cast("bedrock.BedrockGateway", fake_bedrock),
        clock=frozen_clock,
        id_factory=SequenceIdFactory("req"),
    )


@pytest.fixture
def client(
    app_services: services.Services,
) -> typing.Iterator[testclient.TestClient]:
    """앱 전체를 거치는 테스트 클라이언트를 제공한다.

    `raise_server_exceptions=False` 로 두어야 예외 핸들러가 만든 500 응답을
    검증할 수 있다. 기본값이면 예외가 테스트로 그대로 전파된다.
    """
    application = app.create_app_with_services(app_services)
    with testclient.TestClient(
        application, raise_server_exceptions=False
    ) as test_client:
        yield test_client


@pytest.fixture
def api_key(registry: repository.RegistryRepository) -> str:
    """기본 계정 트리와 제한 없는 API 키를 심고 평문 키를 반환한다."""
    return seed_api_key(registry)


@pytest.fixture
def admin_headers(settings: config.Settings) -> dict[str, str]:
    """관리 API 인증 헤더를 제공한다."""
    return {"X-Admin-Token": settings.admin_token}
