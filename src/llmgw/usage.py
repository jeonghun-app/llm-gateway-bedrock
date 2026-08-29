"""사용량 기록 서비스.

요청 하나가 끝날 때(성공이든 실패든) 이 서비스가 호출된다. 하는 일은
네 가지다.

1. 토큰 수와 단가 표로 비용을 계산한다.
2. 원본 레코드를 쓰고 집계를 갱신한다. 두 작업은 하나의 트랜잭션이다.
   저장소 키는 서버가 요청마다 만드는 `usage_id` 다. Bedrock 을 호출한
   횟수만큼 비용이 실제로 발생하므로, 클라이언트가 `X-Request-Id` 를
   재사용해도 집계를 건너뛰지 않는다. 건너뛰면 사용량이 집계에 반영되지
   않아 월 예산 검사가 영원히 통과하고, 청구 배분에서도 빠진다.
   같은 트랜잭션이 네트워크 재전송으로 두 번 도착하는 경우는
   `ClientRequestToken` 이 막는다.
3. EMF 메트릭을 발행한다.
4. API 키의 마지막 사용 시각을 갱신한다(실패해도 무시).

기록 실패는 요청 실패로 이어지지 않는다. Bedrock 응답을 이미 받은
상황에서 집계 실패 때문에 클라이언트에게 5xx 를 주면 사용자는 비용을
지불하고도 결과를 받지 못한다. 대신 실패를 메트릭과 로그로 올리고
CloudWatch 알람으로 감지한다.
"""

from __future__ import annotations

import dataclasses
import datetime

import botocore.exceptions

from llmgw import clock
from llmgw import domain
from llmgw import observability
from llmgw import pricing
from llmgw import repository


@dataclasses.dataclass(frozen=True)
class RecordOutcome:
    """사용량 기록 결과.

    Attributes:
        record: 계산이 끝난 사용량 레코드.
        newly_recorded: 저장소에 새로 기록됐는지 여부. 서버가 만든
            `usage_id` 는 정상 흐름에서 충돌하지 않으므로 보통 `True` 다.
            `False` 는 같은 트랜잭션이 재전송된 경우다.
        persisted: 저장소 쓰기가 오류 없이 끝났는지 여부. `False` 면 집계에
            반영되지 않았다.
    """

    record: domain.UsageRecord
    newly_recorded: bool
    persisted: bool


class UsageRecorder:
    """사용량 계산과 기록을 담당한다."""

    def __init__(
        self,
        *,
        usage_store: repository.UsageStore,
        registry: repository.RegistryRepository,
        pricing_table: pricing.PricingTable,
        metrics: observability.MetricsEmitter,
        logger: observability.Logger,
        id_factory: clock.IdFactory,
        track_key_last_used: bool = True,
    ) -> None:
        """기록기를 만든다.

        Args:
            usage_store: 사용량 저장소.
            registry: API 키 갱신에 쓸 레지스트리 저장소.
            pricing_table: 단가 표.
            metrics: 메트릭 발행기.
            logger: 구조화 로거.
            id_factory: 사용량 레코드 ID 생성기.
            track_key_last_used: 키의 마지막 사용 시각을 갱신할지 여부.
        """
        self._usage_store = usage_store
        self._registry = registry
        self._pricing = pricing_table
        self._metrics = metrics
        self._logger = logger
        self._id_factory = id_factory
        self._track_key_last_used = track_key_last_used

    def build_record(
        self,
        *,
        principal: domain.Principal,
        request_id: str,
        started_at: datetime.datetime,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        status_code: int,
        error_code: str = "",
        streamed: bool = False,
    ) -> domain.UsageRecord:
        """비용을 계산해 사용량 레코드를 만든다.

        Args:
            principal: 인증된 호출 주체.
            request_id: 요청 상관관계 ID. 로그 추적에 쓰이며 저장소 키가
                아니다.
            started_at: 요청 시작 시각.
            model_id: 요청 모델 ID.
            input_tokens: 입력 토큰 수.
            output_tokens: 출력 토큰 수.
            latency_ms: 처리 시간(밀리초).
            status_code: 클라이언트에게 반환한 HTTP 상태 코드.
            error_code: 실패 시 도메인 에러 코드.
            streamed: 스트리밍 응답이었는지 여부.

        Returns:
            비용이 채워진 `UsageRecord`.
        """
        cost = self._pricing.calculate(model_id, input_tokens, output_tokens)
        if not cost.pricing_known and model_id:
            # 새 모델이 추가되면 단가 표 갱신 전까지 비용이 0으로 집계된다.
            # 조용히 과소 집계되는 것을 막기 위해 경고를 남긴다.
            self._logger.warning(
                "단가 표에 없는 모델이다. 비용을 0으로 기록한다",
                extra={"model_id": model_id},
            )
        return domain.UsageRecord(
            usage_id=self._id_factory.new_id(),
            request_id=request_id,
            timestamp=clock.to_iso(started_at),
            account_id=principal.account_id,
            team_id=principal.team_id,
            user_id=principal.user_id,
            key_id=principal.key_id,
            model_id=model_id,
            input_tokens=max(input_tokens, 0),
            output_tokens=max(output_tokens, 0),
            cost_usd=cost.cost_usd,
            latency_ms=max(latency_ms, 0),
            status_code=status_code,
            error_code=error_code,
            streamed=streamed,
            pricing_known=cost.pricing_known,
        )

    def persist(
        self, record: domain.UsageRecord, *, key_hash: str = ""
    ) -> RecordOutcome:
        """레코드를 저장하고 메트릭을 발행한다.

        Args:
            record: 저장할 사용량 레코드.
            key_hash: 마지막 사용 시각을 갱신할 API 키 해시. 빈 문자열이면
                갱신하지 않는다.

        Returns:
            기록 결과.
        """
        newly_recorded = False
        persisted = True
        try:
            newly_recorded = self._usage_store.record(record)
        except botocore.exceptions.ClientError:
            persisted = False
            self._logger.exception(
                "사용량 기록에 실패했다. 집계에 반영되지 않는다",
                extra={
                    "request_id": record.request_id,
                    "account_id": record.account_id,
                    "model_id": record.model_id,
                },
            )
            self._metrics.emit_usage_write_failure()

        if not newly_recorded and persisted:
            self._logger.info(
                "같은 트랜잭션이 재전송됐다. 집계를 중복 반영하지 않는다",
                extra={"usage_id": record.usage_id},
            )

        self._metrics.emit_request(
            model_id=record.model_id,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost_usd=float(record.cost_usd),
            latency_ms=record.latency_ms,
            succeeded=record.is_success,
        )

        if key_hash and self._track_key_last_used:
            self._touch_key(key_hash, record.timestamp)

        return RecordOutcome(
            record=record,
            newly_recorded=newly_recorded,
            persisted=persisted,
        )

    def _touch_key(self, key_hash: str, used_at: str) -> None:
        """키의 마지막 사용 시각을 갱신한다. 실패는 무시한다."""
        try:
            self._registry.touch_api_key(key_hash, used_at)
        except botocore.exceptions.ClientError as exc:
            # 관리 화면의 부가 정보라 실패해도 요청 처리에 영향이 없다.
            # 삭제된 키에 대한 조건 실패는 정상 흐름이므로 debug 로 남긴다.
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                self._logger.debug("삭제된 키의 사용 시각 갱신을 건너뛴다")
            else:
                self._logger.warning(
                    "키 사용 시각 갱신에 실패했다",
                    extra={"error_code": code},
                )
