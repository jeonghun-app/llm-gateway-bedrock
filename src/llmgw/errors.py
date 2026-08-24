"""게이트웨이 도메인 예외.

HTTP 상태 코드와 OpenAI 호환 에러 타입을 예외에 함께 담는다. 라우터는
`GatewayError` 하나만 처리하면 되고, 도메인 계층은 FastAPI 를 import 하지
않는다.

OpenAI 클라이언트는 에러 응답의 `error.type` 과 `error.code` 를 보고
재시도 여부를 판단하는 경우가 있어, 타입 문자열을 OpenAI 규약에 맞춘다.
"""

from __future__ import annotations

import http


class GatewayError(Exception):
    """게이트웨이가 클라이언트에게 반환하는 모든 오류의 기반 클래스.

    Attributes:
        status_code: HTTP 상태 코드.
        error_type: OpenAI 규약의 `error.type` 값.
        code: 기계가 판별할 수 있는 세부 코드.
        message: 사람이 읽는 설명. 시크릿과 PII를 넣지 않는다.
    """

    status_code: int = http.HTTPStatus.INTERNAL_SERVER_ERROR
    error_type: str = "server_error"
    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_payload(self) -> dict[str, dict[str, str]]:
        """OpenAI 호환 에러 본문을 만든다.

        Returns:
            `{"error": {...}}` 형태의 직렬화 가능한 딕셔너리.
        """
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
            }
        }


class InvalidRequestError(GatewayError):
    """요청 본문이나 파라미터가 스펙에 맞지 않는 경우."""

    status_code = http.HTTPStatus.BAD_REQUEST
    error_type = "invalid_request_error"
    code = "invalid_request"


class AuthenticationError(GatewayError):
    """API 키가 없거나 유효하지 않은 경우."""

    status_code = http.HTTPStatus.UNAUTHORIZED
    error_type = "invalid_request_error"
    code = "invalid_api_key"


class PermissionDeniedError(GatewayError):
    """키가 유효하지만 해당 리소스를 쓸 권한이 없는 경우.

    대표적으로 키의 허용 모델 목록에 없는 모델을 요청한 경우다.
    """

    status_code = http.HTTPStatus.FORBIDDEN
    error_type = "invalid_request_error"
    code = "model_not_allowed"


class BudgetExceededError(GatewayError):
    """월 예산 한도를 초과한 경우.

    OpenAI 는 쿼터 초과를 429 + `insufficient_quota` 로 표현한다. 클라이언트
    SDK 가 이 조합을 재시도하지 않도록 설계돼 있어 그대로 따른다.
    """

    status_code = http.HTTPStatus.TOO_MANY_REQUESTS
    error_type = "insufficient_quota"
    code = "budget_exceeded"


class ResourceNotFoundError(GatewayError):
    """관리 API에서 대상 리소스를 찾지 못한 경우."""

    status_code = http.HTTPStatus.NOT_FOUND
    error_type = "invalid_request_error"
    code = "not_found"


class ResourceConflictError(GatewayError):
    """이미 존재하는 식별자로 리소스를 만들려 한 경우."""

    status_code = http.HTTPStatus.CONFLICT
    error_type = "invalid_request_error"
    code = "already_exists"


class ModelNotFoundError(GatewayError):
    """Bedrock 이 해당 모델을 모르거나 계정에서 활성화되지 않은 경우."""

    status_code = http.HTTPStatus.NOT_FOUND
    error_type = "invalid_request_error"
    code = "model_not_found"


class UpstreamRateLimitError(GatewayError):
    """Bedrock 이 스로틀링한 경우.

    클라이언트가 백오프 후 재시도하도록 429 로 내린다.
    """

    status_code = http.HTTPStatus.TOO_MANY_REQUESTS
    error_type = "rate_limit_error"
    code = "upstream_throttled"


class UpstreamError(GatewayError):
    """Bedrock 호출이 위 분류에 해당하지 않는 이유로 실패한 경우."""

    status_code = http.HTTPStatus.BAD_GATEWAY
    error_type = "server_error"
    code = "upstream_error"


class StorageUnavailableError(GatewayError):
    """DynamoDB 접근이 실패한 경우.

    테이블이 없거나 IAM 권한이 부족한 상황이 대표적이다. 첫 배포에서 가장
    흔한 실패라, 일반 500 으로 뭉개지 않고 별도 코드로 구분해 운영자가
    원인을 바로 좁힐 수 있게 한다.
    """

    status_code = http.HTTPStatus.SERVICE_UNAVAILABLE
    error_type = "server_error"
    code = "storage_unavailable"


class AdminNotConfiguredError(GatewayError):
    """관리 토큰이 설정되지 않아 관리 API를 쓸 수 없는 경우.

    토큰이 비어 있을 때 인증을 통과시키면 관리 API가 인터넷에 무인증으로
    열린다. 그런 상태를 만들지 않기 위해 명시적으로 실패시킨다.
    """

    status_code = http.HTTPStatus.SERVICE_UNAVAILABLE
    error_type = "server_error"
    code = "admin_not_configured"
