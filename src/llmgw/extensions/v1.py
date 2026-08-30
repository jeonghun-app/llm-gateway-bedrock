"""확장점 v1 공개 계약.

이 모듈의 이름과 형태는 **공개 API** 다. 하위 호환을 지켜야 한다. 의미나
호출 순서를 바꾸려면 `v2` 모듈을 새로 만들고 전환 기간 동안 둘을 함께
지원한다.

## 왜 별도 DTO 인가

게이트웨이 내부의 `schemas.ChatCompletionRequest` 를 그대로 넘기지 않는다.
세 가지 이유다.

1. 내부 스키마는 `extra="allow"` 이고 frozen 이 아니다. 확장이 제자리에서
   수정하면 필터 실행 순서에 따라 결과가 달라지고, 원본이 무엇이었는지
   알 수 없어 감사가 불가능해진다.
2. 내부 스키마를 공개하면 내부 리팩터링이 곧 확장 파괴가 된다.
3. 확장이 바꿔서는 안 되는 것(모델, 스트리밍 여부)을 타입 차원에서 막을 수
   있다. 그 둘은 컨텍스트에만 있어 반환값으로 바꿀 수 없다.

## 확장이 바꿀 수 없는 것

- `model_id`: 모델 허용 목록과 단가 정책을 이미 통과한 값이다. 확장이 바꾸면
  권한 검사를 우회하게 된다.
- `streamed`: 응답 형식은 클라이언트와의 계약이다.
- 토큰 수와 비용: Bedrock 이 실제로 청구한 값을 기록한다. 확장이 프롬프트를
  늘리면 그만큼 입력 토큰과 비용이 늘고, 그것이 정확한 기록이다.
"""

from __future__ import annotations

import dataclasses
import datetime
import http
import typing

from llmgw import errors

__all__ = [
    "ExtensionPrincipal",
    "RequestContext",
    "Message",
    "RequestPayload",
    "RequestFilter",
    "RequestRejectedError",
    "ExtensionUnavailableError",
]


class RequestRejectedError(errors.GatewayError):
    """확장이 정책에 따라 요청을 거부했다.

    확장이 "이 요청은 통과시키지 않는다" 고 판단한 정상 결과다. 확장 자체의
    고장과 구분해야 하므로 별도 예외를 둔다.
    """

    status_code = http.HTTPStatus.FORBIDDEN
    code = "request_rejected"


class ExtensionUnavailableError(errors.GatewayError):
    """확장이 고장났거나 제한 시간을 넘겼다.

    503 인 이유는 요청 자체에는 문제가 없고 게이트웨이 측 구성 요소가
    응답하지 못한 상황이기 때문이다. 4xx 로 돌려주면 클라이언트가 요청을
    고치려 시도하게 된다.
    """

    status_code = http.HTTPStatus.SERVICE_UNAVAILABLE
    code = "extension_unavailable"


@dataclasses.dataclass(frozen=True, slots=True)
class ExtensionPrincipal:
    """요청 주체의 식별자만 담은 사영(projection).

    키 해시와 예산·허용 모델은 넣지 않는다. 확장이 정책 판단에 쓸 이유가
    없고, 넣으면 공개 계약이 내부 인증 모델과 결합된다.

    Attributes:
        account_id: 계정 ID.
        team_id: 팀 ID. 팀이 없으면 빈 문자열.
        user_id: 사용자 ID.
        key_id: API 키 ID. OIDC 로 인증했으면 빈 문자열.
    """

    account_id: str
    team_id: str
    user_id: str
    key_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class RequestContext:
    """확장에 넘기는 요청 컨텍스트.

    `model_id` 와 `streamed` 가 여기 있고 반환 payload 에는 없다. 확장이
    바꿀 수 없게 하려는 배치다.

    Attributes:
        principal: 요청 주체 식별자.
        request_id: 상관관계 ID. 로그와 사용량 레코드에 같은 값이 남는다.
            **멱등 키가 아니다.** 클라이언트가 같은 값으로 재시도하면 호출
            횟수만큼 별도로 집계된다.
        model_id: 요청 모델 ID. 확장이 바꿀 수 없다.
        started_at: 요청 수신 시각(UTC).
        streamed: 스트리밍 요청 여부. 확장이 바꿀 수 없다.
        deadline_at: 이 확장 호출의 제한 시각. 확장이 네트워크 호출을 한다면
            이 시각을 넘기지 않도록 자체 타임아웃을 걸어야 한다. 게이트웨이는
            이 시각이 지나면 결과를 버리지만, 실행 중인 코드를 강제로 멈출
            수는 없다.
    """

    principal: ExtensionPrincipal
    request_id: str
    model_id: str
    started_at: datetime.datetime
    streamed: bool
    deadline_at: datetime.datetime


@dataclasses.dataclass(frozen=True, slots=True)
class Message:
    """대화 메시지 한 건.

    `content` 가 문자열인 이유는 현재 계약이 텍스트 Converse 범위이기
    때문이다. 멀티모달을 지원하게 되면 v1 을 확장하는 대신 v2 가 필요하다.

    Attributes:
        role: `system`, `developer`, `user`, `assistant` 중 하나.
        content: 메시지 본문. 조각 배열은 평평한 문자열로 합쳐서 넘긴다.
    """

    role: str
    content: str


@dataclasses.dataclass(frozen=True, slots=True)
class RequestPayload:
    """확장이 검사하고 변형할 수 있는 요청 본문.

    모든 필드가 불변이다. 확장은 이 객체를 수정하지 않고 새 객체를 반환한다.

    Attributes:
        messages: 대화 이력. 비어 있으면 게이트웨이가 거부한다.
        max_tokens: 최대 출력 토큰. `None` 이면 모델 기본값.
        temperature: 샘플링 온도.
        top_p: 누적 확률 절단값.
        stop_sequences: 정지 문자열.
    """

    messages: tuple[Message, ...]
    max_tokens: int | None
    temperature: float | None
    top_p: float | None
    stop_sequences: tuple[str, ...]


class RequestFilter(typing.Protocol):
    """Bedrock 호출 전에 요청을 검사하거나 변형하는 확장점.

    동기 함수다. 이 게이트웨이의 요청 핸들러가 모두 동기이고(boto3 가 동기
    라이브러리다) Starlette 이 threadpool 에서 실행한다. 확장을 위해 요청
    경로를 async 로 바꿀 이유가 없다.

    호출 시점은 인증·레이트리밋·단가정책·모델권한·예산 검사를 모두 통과한
    뒤, Bedrock 요청으로 변환하기 전이다. 따라서 확장은 이미 인증되고
    권한이 확인된 요청만 본다.

    여러 확장을 설정하면 **설정에 적은 순서대로** 직렬 적용된다. 뒤의 확장은
    앞의 확장이 반환한 값을 본다. 순서가 결과를 바꿀 수 있으므로 순서가
    계약의 일부다.
    """

    def filter_request(
        self, payload: RequestPayload, *, context: RequestContext
    ) -> RequestPayload:
        """요청을 검사하고, 통과시킬 요청 본문을 반환한다.

        Args:
            payload: 현재 요청 본문. 수정하지 말고 새 객체를 반환한다.
            context: 요청 주체와 제한 시각.

        Returns:
            Bedrock 에 보낼 요청 본문. 변형하지 않으려면 받은 `payload` 를
            그대로 반환한다. `None` 을 반환해서는 안 된다. "통과" 와
            "본문 제거" 를 구분할 수 없어지기 때문이다.

        Raises:
            RequestRejectedError: 정책에 따라 요청을 거부할 때. 게이트웨이가
                403 으로 응답하고 Bedrock 을 호출하지 않는다.
        """
        ...
