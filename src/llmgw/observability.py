"""구조화 로깅과 CloudWatch 메트릭.

로그는 AWS Lambda Powertools 의 `Logger` 를 쓴다. Lambda 전용 데코레이터는
쓰지 않지만 JSON 포매터가 ECS 에서도 그대로 동작한다. 상관관계 ID 는
`contextvars` 에 담고 포매터에서 꺼내 모든 로그 줄에 자동으로 붙인다.
Powertools 의 `append_keys` 를 쓰지 않은 이유는 그 상태가 로거 인스턴스에
공유되어 동시 요청 사이에 값이 섞일 수 있기 때문이다. `contextvars` 는
asyncio 태스크 경계를 따라 정확히 전파된다.

메트릭은 EMF(Embedded Metric Format) 로 stdout 에 쓴다. CloudWatch Logs 가
EMF 구조를 자동으로 메트릭으로 추출하므로 `PutMetricData` 를 직접 호출하지
않는다. API 스로틀링과 추가 IAM 권한이 필요 없다는 것이 이점이다.

메트릭 차원에 계정 ID 를 넣지 않는다. 차원 조합마다 별도 커스텀 메트릭으로
과금되어 테넌트 수에 비례해 비용이 늘어난다. 계정·팀·사용자별 수치는
DynamoDB 집계 테이블에서 보고, CloudWatch 는 서비스 전체와 모델별 건강
상태만 본다.
"""

from __future__ import annotations

import contextvars
import sys
import typing

import aws_lambda_powertools
from aws_lambda_powertools import metrics as pt_metrics
from aws_lambda_powertools.logging import formatter as pt_formatter

# 요청 단위 상관관계 ID. 미들웨어가 채우고 포매터가 읽는다.
_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llmgw_correlation_id", default=""
)

Logger = aws_lambda_powertools.Logger

_MetricUnit = pt_metrics.MetricUnit


def set_correlation_id(value: str) -> contextvars.Token[str]:
    """현재 컨텍스트의 상관관계 ID 를 설정한다.

    Args:
        value: 요청 ID.

    Returns:
        복원에 사용할 컨텍스트 토큰.
    """
    return _CORRELATION_ID.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    """상관관계 ID 를 이전 값으로 복원한다.

    Args:
        token: `set_correlation_id` 가 반환한 토큰.
    """
    _CORRELATION_ID.reset(token)


def get_correlation_id() -> str:
    """현재 컨텍스트의 상관관계 ID 를 반환한다. 없으면 빈 문자열."""
    return _CORRELATION_ID.get()


class CorrelationIdFormatter(pt_formatter.LambdaPowertoolsFormatter):
    """모든 로그 줄에 `correlation_id` 를 붙이는 포매터."""

    def serialize(self, log: typing.Any) -> str:
        """직렬화 직전에 상관관계 ID 를 주입한다.

        Args:
            log: Powertools 가 만든 로그 딕셔너리.

        Returns:
            JSON 문자열.
        """
        correlation_id = get_correlation_id()
        if correlation_id:
            log["correlation_id"] = correlation_id
        return super().serialize(log)


def create_logger(*, service_name: str, level: str) -> Logger:
    """JSON 구조화 로거를 만든다.

    Args:
        service_name: 로그의 `service` 필드 값.
        level: 로그 레벨 문자열(`DEBUG`/`INFO`/...).

    Returns:
        구성된 Powertools `Logger`.
    """
    return Logger(
        service=service_name,
        level=level.upper(),
        logger_formatter=CorrelationIdFormatter(use_rfc3339=True),
        stream=sys.stdout,
    )


class MetricsEmitter:
    """요청 단위 EMF 메트릭 발행기."""

    def __init__(
        self,
        *,
        namespace: str,
        environment: str,
        logger: Logger | None = None,
    ) -> None:
        """발행기를 만든다.

        Args:
            namespace: CloudWatch 커스텀 메트릭 네임스페이스.
            environment: `Environment` 차원 값(`dev`/`stg`/`prod`).
            logger: 발행 실패를 남길 로거.
        """
        self._namespace = namespace
        self._environment = environment
        self._logger = logger

    def emit_request(
        self,
        *,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        succeeded: bool,
    ) -> None:
        """한 요청의 처리 결과를 메트릭으로 남긴다.

        `EphemeralMetrics` 를 요청마다 새로 만든다. 전역 `Metrics` 는
        플러시 전까지 상태를 누적하는데, 동시 요청이 섞이면 차원이 뒤엉킨다.

        Args:
            model_id: 요청 모델 ID.
            input_tokens: 입력 토큰 수.
            output_tokens: 출력 토큰 수.
            cost_usd: 계산된 비용.
            latency_ms: 처리 시간(밀리초).
            succeeded: 성공 여부.
        """
        emitter = pt_metrics.EphemeralMetrics(namespace=self._namespace)
        emitter.add_dimension(name="Environment", value=self._environment)
        emitter.add_dimension(name="Model", value=model_id or "unknown")
        emitter.add_metric(name="Requests", unit=_MetricUnit.Count, value=1)
        emitter.add_metric(
            name="Errors",
            unit=_MetricUnit.Count,
            value=0 if succeeded else 1,
        )
        emitter.add_metric(
            name="InputTokens", unit=_MetricUnit.Count, value=input_tokens
        )
        emitter.add_metric(
            name="OutputTokens", unit=_MetricUnit.Count, value=output_tokens
        )
        emitter.add_metric(
            name="CostUsd", unit=_MetricUnit.NoUnit, value=cost_usd
        )
        emitter.add_metric(
            name="LatencyMs",
            unit=_MetricUnit.Milliseconds,
            value=latency_ms,
        )
        self._flush(emitter)

    def emit_unpriced_request(self, model_id: str) -> None:
        """단가 표에 없는 모델로 처리된 요청을 메트릭으로 올린다.

        이 값이 0 이 아니면 비용 집계가 실제보다 낮고, 그만큼 월 예산이
        늦게 걸린다. 로그만 남기면 아무도 보지 않으므로 알람을 걸 수 있는
        메트릭으로 만든다.

        Args:
            model_id: 단가를 찾지 못한 모델 ID.
        """
        emitter = pt_metrics.EphemeralMetrics(namespace=self._namespace)
        emitter.add_dimension(name="Environment", value=self._environment)
        emitter.add_dimension(name="Model", value=model_id)
        emitter.add_metric(
            name="UnpricedRequests", unit=_MetricUnit.Count, value=1
        )
        self._flush(emitter)

    def emit_usage_write_failure(self) -> None:
        """사용량 기록 최종 실패를 메트릭으로 남긴다.

        동기 HTTP API 라 실패 이벤트를 넣을 큐가 없어 DLQ 를 걸 수 없다.
        대신 이 메트릭에 CloudWatch 알람을 붙여 집계 유실을 감지한다.
        """
        emitter = pt_metrics.EphemeralMetrics(namespace=self._namespace)
        emitter.add_dimension(name="Environment", value=self._environment)
        emitter.add_metric(
            name="UsageWriteFailures", unit=_MetricUnit.Count, value=1
        )
        self._flush(emitter)

    def _flush(self, emitter: pt_metrics.EphemeralMetrics) -> None:
        """메트릭을 stdout 으로 내보낸다.

        메트릭 발행 실패가 API 응답을 깨뜨려서는 안 되므로 검증 오류를
        삼킨다. 대신 삼킨 사실을 로그로 남겨 조용한 실패를 만들지 않는다.

        Args:
            emitter: 플러시할 EMF 발행기.
        """
        try:
            emitter.flush_metrics()
        except pt_metrics.SchemaValidationError:
            if self._logger is not None:
                self._logger.exception("EMF 메트릭 검증에 실패했다")
