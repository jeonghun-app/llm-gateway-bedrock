"""Bedrock 어댑터 테스트.

비스트리밍 호출은 `botocore.stub.Stubber` 로 검증한다. Stubber 는 요청
파라미터를 실제 서비스 모델과 대조하므로, 우리가 만드는 Converse 인자가
API 스펙에 맞는지까지 확인된다.

스트리밍은 `EventStream` 객체를 Stubber 로 만들 수 없어 대역 클라이언트를
쓴다. 이 경우 검증 대상은 이벤트 파싱 로직이다.
"""

from __future__ import annotations

import typing

import boto3
import botocore.exceptions
import botocore.stub
import pytest

from llmgw import bedrock
from llmgw import cache
from llmgw import errors
from llmgw import observability

_CONVERSE_RESPONSE = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "안녕하세요"}],
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 12, "outputTokens": 5, "totalTokens": 17},
    "metrics": {"latencyMs": 321},
}


class _FakeRuntime:
    """converse_stream 이벤트를 그대로 돌려주는 대역."""

    def __init__(self, events: list[dict[str, typing.Any]]) -> None:
        self._events = events
        self.captured_params: dict[str, typing.Any] = {}

    def converse_stream(self, **params: typing.Any) -> dict[str, typing.Any]:
        """이벤트 목록을 스트림처럼 반환한다."""
        self.captured_params = params
        return {"stream": iter(self._events)}


class _FailingControl:
    """모델 목록 조회가 실패하는 대역."""

    def list_foundation_models(
        self, **kwargs: typing.Any
    ) -> dict[str, typing.Any]:
        """항상 접근 거부를 던진다."""
        del kwargs
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "ListFoundationModels",
        )

    def get_paginator(self, name: str) -> typing.Any:
        """항상 실패하는 페이지네이터를 반환한다."""
        del name
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "ListInferenceProfiles",
        )


class _StaticControl:
    """고정된 모델 목록을 반환하는 대역."""

    def __init__(self) -> None:
        self.call_count = 0

    def list_foundation_models(
        self, **kwargs: typing.Any
    ) -> dict[str, typing.Any]:
        """온디맨드 텍스트 모델 2건을 반환한다."""
        del kwargs
        self.call_count += 1
        return {
            "modelSummaries": [
                {"modelId": "amazon.nova-lite-v1:0"},
                {"modelId": "amazon.nova-pro-v1:0"},
            ]
        }

    def get_paginator(self, name: str) -> typing.Any:
        """추론 프로파일 1건을 반환하는 페이지네이터를 만든다."""
        del name

        class _Paginator:
            def paginate(self) -> typing.Iterator[dict[str, typing.Any]]:
                yield {
                    "inferenceProfileSummaries": [
                        {
                            "inferenceProfileId": "us.anthropic.claude-3"
                            "-haiku-20240307-v1:0",
                            "status": "ACTIVE",
                        },
                        {
                            "inferenceProfileId": "us.retired-model",
                            "status": "INACTIVE",
                        },
                    ]
                }

        return _Paginator()


@pytest.fixture
def gateway_logger() -> observability.Logger:
    """조용한 로거를 제공한다."""
    return observability.create_logger(
        service_name="llmgw-test", level="CRITICAL"
    )


def _gateway(
    *,
    control: typing.Any,
    runtime: typing.Any,
    logger: observability.Logger,
) -> bedrock.BedrockGateway:
    """캐시를 끈 게이트웨이를 만든다."""
    return bedrock.BedrockGateway(
        control_client=control,
        runtime_client=runtime,
        logger=logger,
        model_cache=cache.TtlCache(ttl_seconds=0),
    )


# -- converse ---------------------------------------------------------------


def test_converse_정상응답을파싱한다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    stubber.add_response(
        "converse",
        _CONVERSE_RESPONSE,
        expected_params={
            "modelId": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": [{"text": "안녕"}]}],
            "system": [{"text": "규칙"}],
            "inferenceConfig": {"maxTokens": 64},
        },
    )
    subject = _gateway(
        control=_StaticControl(), runtime=client, logger=gateway_logger
    )

    # Act
    with stubber:
        result = subject.converse(
            model_id="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "안녕"}]}],
            system=[{"text": "규칙"}],
            inference_config={"maxTokens": 64},
        )

    # Assert
    assert result.text == "안녕하세요"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 12
    assert result.output_tokens == 5


def test_converse_빈system과빈설정은파라미터에서제외된다(
    gateway_logger: observability.Logger,
) -> None:
    """일부 모델은 빈 system/inferenceConfig 를 거부한다."""
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    stubber.add_response(
        "converse",
        _CONVERSE_RESPONSE,
        expected_params={
            "modelId": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        },
    )
    subject = _gateway(
        control=_StaticControl(), runtime=client, logger=gateway_logger
    )

    # Act
    with stubber:
        subject.converse(
            model_id="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )

    # Assert
    stubber.assert_no_pending_responses()


def test_converse_텍스트블록이없으면빈문자열(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    stubber.add_response(
        "converse",
        {
            "output": {"message": {"role": "assistant", "content": []}},
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 1,
                "outputTokens": 0,
                "totalTokens": 1,
            },
            "metrics": {"latencyMs": 10},
        },
    )
    subject = _gateway(
        control=_StaticControl(), runtime=client, logger=gateway_logger
    )

    # Act
    with stubber:
        result = subject.converse(
            model_id="m",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )

    # Assert
    assert result.text == ""
    assert result.output_tokens == 0


@pytest.mark.parametrize(
    ("service_code", "expected_error"),
    [
        ("ValidationException", errors.InvalidRequestError),
        ("ResourceNotFoundException", errors.ModelNotFoundError),
        ("AccessDeniedException", errors.PermissionDeniedError),
        ("ThrottlingException", errors.UpstreamRateLimitError),
        ("ServiceQuotaExceededException", errors.UpstreamRateLimitError),
        ("ModelNotReadyException", errors.UpstreamRateLimitError),
        ("ModelTimeoutException", errors.UpstreamError),
        ("InternalServerException", errors.UpstreamError),
        ("ServiceUnavailableException", errors.UpstreamError),
        ("완전히새로운코드", errors.UpstreamError),
    ],
)
def test_converse_에러코드를도메인예외로변환한다(
    gateway_logger: observability.Logger,
    service_code: str,
    expected_error: type[errors.GatewayError],
) -> None:
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    stubber.add_client_error(
        "converse",
        service_error_code=service_code,
        service_message="테스트 오류",
    )
    subject = _gateway(
        control=_StaticControl(), runtime=client, logger=gateway_logger
    )

    # Act / Assert
    with stubber, pytest.raises(expected_error):
        subject.converse(
            model_id="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )


def test_converse_모델액세스거부시안내메시지를포함한다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    stubber = botocore.stub.Stubber(client)
    stubber.add_client_error(
        "converse",
        service_error_code="AccessDeniedException",
        service_message="denied",
    )
    subject = _gateway(
        control=_StaticControl(), runtime=client, logger=gateway_logger
    )

    # Act / Assert
    with (
        stubber,
        pytest.raises(errors.PermissionDeniedError, match="모델 액세스"),
    ):
        subject.converse(
            model_id="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )


def test_converse_SDK레벨오류도UpstreamError로변환한다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    class _ConnectionFailingRuntime:
        def converse(self, **params: typing.Any) -> dict[str, typing.Any]:
            del params
            raise botocore.exceptions.ConnectTimeoutError(
                endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"
            )

    subject = _gateway(
        control=_StaticControl(),
        runtime=_ConnectionFailingRuntime(),
        logger=gateway_logger,
    )

    # Act / Assert
    with pytest.raises(errors.UpstreamError, match="ConnectTimeoutError"):
        subject.converse(
            model_id="m",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )


# -- converse_stream --------------------------------------------------------


def test_converse_stream_텍스트증분과사용량을순서대로내보낸다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    runtime = _FakeRuntime(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "안"}}},
            {"contentBlockDelta": {"delta": {"text": "녕"}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}},
            {
                "metadata": {
                    "usage": {"inputTokens": 7, "outputTokens": 2},
                    "metrics": {"latencyMs": 50},
                }
            },
        ]
    )
    subject = _gateway(
        control=_StaticControl(), runtime=runtime, logger=gateway_logger
    )

    # Act
    deltas = list(
        subject.converse_stream(
            model_id="amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )
    )

    # Assert
    texts = [delta.text for delta in deltas if delta.text]
    assert texts == ["안", "녕"]
    stop_deltas = [delta for delta in deltas if delta.stop_reason]
    assert stop_deltas[0].stop_reason == "end_turn"
    final = deltas[-1]
    assert final.is_final is True
    assert (final.input_tokens, final.output_tokens) == (7, 2)


def test_converse_stream_빈텍스트증분은버린다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    runtime = _FakeRuntime(
        [
            {"contentBlockDelta": {"delta": {"text": ""}}},
            {"contentBlockDelta": {"delta": {}}},
            {"contentBlockDelta": {"delta": {"text": "실제"}}},
        ]
    )
    subject = _gateway(
        control=_StaticControl(), runtime=runtime, logger=gateway_logger
    )

    # Act
    deltas = list(
        subject.converse_stream(
            model_id="m",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inference_config={},
        )
    )

    # Assert
    assert [delta.text for delta in deltas] == ["실제"]


def test_converse_stream_오류이벤트를UpstreamError로올린다(
    gateway_logger: observability.Logger,
) -> None:
    """스트림 중간 오류를 무시하면 응답이 조용히 잘린다."""
    # Arrange
    runtime = _FakeRuntime(
        [
            {"contentBlockDelta": {"delta": {"text": "부분"}}},
            {"internalServerException": {"message": "boom"}},
        ]
    )
    subject = _gateway(
        control=_StaticControl(), runtime=runtime, logger=gateway_logger
    )

    # Act / Assert
    generator = subject.converse_stream(
        model_id="m",
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system=[],
        inference_config={},
    )
    assert next(generator).text == "부분"
    with pytest.raises(errors.UpstreamError, match="스트림 오류"):
        next(generator)


def test_converse_stream_파라미터를그대로전달한다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    runtime = _FakeRuntime([{"messageStop": {"stopReason": "end_turn"}}])
    subject = _gateway(
        control=_StaticControl(), runtime=runtime, logger=gateway_logger
    )

    # Act
    list(
        subject.converse_stream(
            model_id="amazon.nova-pro-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[{"text": "s"}],
            inference_config={"temperature": 0.1},
        )
    )

    # Assert
    assert runtime.captured_params == {
        "modelId": "amazon.nova-pro-v1:0",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "system": [{"text": "s"}],
        "inferenceConfig": {"temperature": 0.1},
    }


# -- list_model_ids ---------------------------------------------------------


def test_list_model_ids_기반모델과활성프로파일을합친다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    subject = _gateway(
        control=_StaticControl(),
        runtime=_FakeRuntime([]),
        logger=gateway_logger,
    )

    # Act
    model_ids = subject.list_model_ids()

    # Assert
    assert model_ids == (
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0",
    )
    assert (
        "us.retired-model" not in model_ids
    ), "비활성 프로파일은 노출하지 않아야 한다"


def test_list_model_ids_조회실패시빈튜플을반환한다(
    gateway_logger: observability.Logger,
) -> None:
    """모델 목록 조회 실패가 게이트웨이 전체를 죽이면 안 된다."""
    # Arrange
    subject = _gateway(
        control=_FailingControl(),
        runtime=_FakeRuntime([]),
        logger=gateway_logger,
    )

    # Act
    model_ids = subject.list_model_ids()

    # Assert
    assert model_ids == ()


def test_list_model_ids_캐시가컨트롤플레인호출을줄인다(
    gateway_logger: observability.Logger,
) -> None:
    # Arrange
    control = _StaticControl()
    subject = bedrock.BedrockGateway(
        control_client=control,
        runtime_client=_FakeRuntime([]),
        logger=gateway_logger,
        model_cache=cache.TtlCache(ttl_seconds=300),
    )

    # Act
    subject.list_model_ids()
    subject.list_model_ids()

    # Assert
    assert (
        control.call_count == 1
    ), f"컨트롤 플레인 호출이 {control.call_count}회 발생했다"
