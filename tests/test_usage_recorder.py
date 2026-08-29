"""사용량 기록 서비스 테스트."""

from __future__ import annotations

import decimal
import typing

import botocore.exceptions
import pytest

import conftest
from llmgw import apikey
from llmgw import clock
from llmgw import domain
from llmgw import observability
from llmgw import pricing
from llmgw import repository
from llmgw import usage as usage_module

_PRINCIPAL = domain.Principal(
    account_id="acme",
    team_id="platform",
    user_id="alice",
    key_id="key-1",
)


def _build(
    recorder: usage_module.UsageRecorder, **overrides: typing.Any
) -> domain.UsageRecord:
    """기본값이 채워진 레코드를 만든다."""
    params: dict[str, typing.Any] = {
        "principal": _PRINCIPAL,
        "request_id": "req-1",
        "started_at": conftest.FIXED_NOW,
        "model_id": "amazon.nova-lite-v1:0",
        "input_tokens": 1000,
        "output_tokens": 500,
        "latency_ms": 120,
        "status_code": 200,
    }
    params.update(overrides)
    return recorder.build_record(**params)


def test_build_record_비용을계산해채운다(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange / Act
    record = _build(usage_recorder)

    # Assert
    # nova-lite 픽스처 단가: 입력 0.001, 출력 0.002 USD/1K
    # 1000/1000*0.001 + 500/1000*0.002 = 0.001 + 0.001 = 0.002
    assert record.cost_usd == decimal.Decimal(
        "0.0020000000"
    ), f"기대 0.002, 실제 {record.cost_usd}"
    assert record.pricing_known is True
    assert record.timestamp == "2026-08-23T12:00:00Z"


def test_build_record_단가없는모델_비용0과플래그(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange / Act
    record = _build(usage_recorder, model_id="brand.new-model")

    # Assert
    assert record.cost_usd == decimal.Decimal("0")
    assert record.pricing_known is False


def test_build_record_음수값을0으로보정한다(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange / Act
    record = _build(
        usage_recorder, input_tokens=-5, output_tokens=-1, latency_ms=-9
    )

    # Assert
    assert (record.input_tokens, record.output_tokens, record.latency_ms) == (
        0,
        0,
        0,
    )


def test_build_record_실패요청도레코드를만든다(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange / Act
    record = _build(
        usage_recorder,
        status_code=429,
        error_code="budget_exceeded",
        input_tokens=0,
        output_tokens=0,
    )

    # Assert
    assert record.is_success is False
    assert record.error_code == "budget_exceeded"


def test_persist_신규기록_newly_recorded는True(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange
    record = _build(usage_recorder)

    # Act
    outcome = usage_recorder.persist(record)

    # Assert
    assert outcome.newly_recorded is True
    assert outcome.persisted is True


def test_persist_중복기록_newly_recorded는False(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange
    record = _build(usage_recorder)

    # Act
    usage_recorder.persist(record)
    outcome = usage_recorder.persist(record)

    # Assert
    assert outcome.newly_recorded is False
    assert (
        outcome.persisted is True
    ), "중복은 오류가 아니므로 persisted 는 True 여야 한다"


def test_persist_중복기록시집계가증가하지않는다(
    usage_recorder: usage_module.UsageRecorder,
    usage_store: repository.UsageStore,
) -> None:
    # Arrange
    record = _build(usage_recorder)

    # Act
    usage_recorder.persist(record)
    usage_recorder.persist(record)
    usage_recorder.persist(record)

    # Assert
    total = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert total.requests == 1, f"기대 1, 실제 {total.requests}"
    assert total.cost_usd == decimal.Decimal("0.0020000000")


def test_persist_저장실패시persisted는False이고예외를던지지않는다(
    registry: repository.RegistryRepository,
    pricing_table: pricing.PricingTable,
    metrics: observability.MetricsEmitter,
    logger: observability.Logger,
) -> None:
    """Bedrock 응답을 이미 받은 뒤라면 집계 실패로 5xx 를 내면 안 된다."""

    # Arrange
    class FailingStore:
        """항상 실패하는 저장소 대역."""

        def record(self, usage: domain.UsageRecord) -> bool:
            """언제나 DynamoDB 오류를 던진다."""
            del usage
            raise botocore.exceptions.ClientError(
                {
                    "Error": {
                        "Code": "ProvisionedThroughputExceededException",
                        "Message": "throttled",
                    }
                },
                "TransactWriteItems",
            )

    recorder = usage_module.UsageRecorder(
        usage_store=typing.cast("repository.UsageStore", FailingStore()),
        registry=registry,
        pricing_table=pricing_table,
        metrics=metrics,
        logger=logger,
        id_factory=clock.UUID_ID_FACTORY,
    )
    record = _build(recorder)

    # Act
    outcome = recorder.persist(record)

    # Assert
    assert outcome.persisted is False
    assert outcome.newly_recorded is False


def test_persist_키사용시각을갱신한다(
    usage_recorder: usage_module.UsageRecorder,
    registry: repository.RegistryRepository,
) -> None:
    # Arrange
    generated = apikey.generate_api_key("test")
    registry.put_api_key(
        domain.ApiKey(
            key_id="key-1",
            key_hash=generated.key_hash,
            key_prefix=generated.key_prefix,
            account_id="acme",
            team_id="platform",
            user_id="alice",
        )
    )
    record = _build(usage_recorder)

    # Act
    usage_recorder.persist(record, key_hash=generated.key_hash)

    # Assert
    loaded = registry.get_api_key_by_hash(generated.key_hash)
    assert loaded is not None
    assert loaded.last_used_at == "2026-08-23T12:00:00Z"


def test_persist_삭제된키의사용시각갱신실패를무시한다(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange
    record = _build(usage_recorder)

    # Act
    outcome = usage_recorder.persist(record, key_hash="a" * 64)

    # Assert
    assert (
        outcome.persisted is True
    ), "부가 정보 갱신 실패가 기록 성공을 뒤집으면 안 된다"


def test_persist_키해시가없으면갱신을시도하지않는다(
    usage_recorder: usage_module.UsageRecorder,
) -> None:
    # Arrange
    record = _build(usage_recorder)

    # Act
    outcome = usage_recorder.persist(record, key_hash="")

    # Assert
    assert outcome.persisted is True


@pytest.mark.parametrize(
    ("status_code", "expected_success"),
    [(200, 1), (201, 1), (299, 1), (400, 0), (429, 0), (500, 0)],
)
def test_persist_상태코드에따라성공실패축이나뉜다(
    usage_recorder: usage_module.UsageRecorder,
    usage_store: repository.UsageStore,
    status_code: int,
    expected_success: int,
) -> None:
    # Arrange
    record = _build(
        usage_recorder,
        request_id=f"req-{status_code}",
        status_code=status_code,
    )

    # Act
    usage_recorder.persist(record)

    # Assert
    total = usage_store.query_totals(
        "acme", domain.Granularity.DAY, "2026-08-23"
    )["TOTAL"]
    assert total.success_requests == expected_success
    assert total.error_requests == 1 - expected_success
