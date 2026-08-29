"""HTTP 요청/응답 스키마.

`/v1/*` 는 OpenAI Chat Completions 스펙과 호환되는 필드 이름을 쓴다.
기존 OpenAI SDK 의 `base_url` 만 바꿔서 붙일 수 있게 하는 것이 목적이다.

OpenAI 가 정의했지만 Bedrock Converse 에 대응이 없는 필드(`n`,
`presence_penalty`, `logit_bias` 등)는 받아들이되 무시한다. 클라이언트가
기본값으로 채워 보내는 경우가 많아 거부하면 호환성이 떨어진다. 다만
결과가 달라질 수 있는 `n > 1` 은 조용히 무시하지 않고 명시적으로 거부한다.
"""

from __future__ import annotations

import decimal
import typing

import pydantic

# Bedrock Converse 는 후보 응답을 하나만 반환한다.
_SUPPORTED_CHOICE_COUNT = 1


class ContentPart(pydantic.BaseModel):
    """멀티모달 메시지의 텍스트 조각.

    Attributes:
        type: 조각 종류. 현재는 `text` 만 처리한다.
        text: 텍스트 내용.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    type: str = "text"
    text: str = ""


class ChatMessage(pydantic.BaseModel):
    """대화 메시지 한 건.

    Attributes:
        role: `system`, `developer`, `user`, `assistant` 중 하나.
        content: 문자열 또는 텍스트 조각 배열.
        name: OpenAI 호환용 선택 필드. 변환에는 쓰지 않는다.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    role: str
    content: str | list[ContentPart] | None = None
    name: str | None = None

    def text(self) -> str:
        """메시지 본문을 평평한 문자열로 만든다.

        Returns:
            텍스트 조각을 개행으로 이어붙인 문자열. 내용이 없으면 빈 문자열.
        """
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(part.text for part in self.content if part.text)


class ChatCompletionRequest(pydantic.BaseModel):
    """`POST /v1/chat/completions` 요청 본문.

    Attributes:
        model: Bedrock 모델 ID 또는 추론 프로파일 ID.
        messages: 대화 이력.
        max_tokens: 최대 출력 토큰. OpenAI 의 구 필드명.
        max_completion_tokens: 최대 출력 토큰. OpenAI 의 신 필드명.
            둘 다 오면 이쪽을 우선한다.
        temperature: 샘플링 온도.
        top_p: 누적 확률 절단값.
        stop: 정지 문자열 또는 목록.
        stream: SSE 스트리밍 여부.
        n: 생성할 후보 수. 1만 지원한다.
        user: 호출자 식별 문자열. 사용량은 API 키로 귀속되므로 무시한다.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    model: str = pydantic.Field(min_length=1)
    messages: list[ChatMessage] = pydantic.Field(min_length=1)
    max_tokens: int | None = pydantic.Field(default=None, ge=1, le=200_000)
    max_completion_tokens: int | None = pydantic.Field(
        default=None, ge=1, le=200_000
    )
    temperature: float | None = pydantic.Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = pydantic.Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    stream: bool = False
    n: int = pydantic.Field(default=_SUPPORTED_CHOICE_COUNT, ge=1, le=1)
    user: str | None = None

    @property
    def effective_max_tokens(self) -> int | None:
        """적용할 최대 출력 토큰 수를 결정한다."""
        return self.max_completion_tokens or self.max_tokens

    @property
    def stop_sequences(self) -> list[str]:
        """정지 문자열을 목록으로 정규화한다."""
        if self.stop is None:
            return []
        if isinstance(self.stop, str):
            return [self.stop] if self.stop else []
        return [item for item in self.stop if item]


# ---------------------------------------------------------------------------
# 관리 API 스키마
# ---------------------------------------------------------------------------


class _AdminBase(pydantic.BaseModel):
    """관리 API 요청 공통 설정."""

    model_config = pydantic.ConfigDict(
        extra="forbid", str_strip_whitespace=True
    )


class CreateAccountRequest(_AdminBase):
    """계정 생성 요청.

    Attributes:
        account_id: 소문자 영숫자와 하이픈으로 된 식별자.
        name: 표시 이름.
        monthly_budget_usd: 월 예산. 생략하면 무제한.
    """

    account_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class CreateTeamRequest(_AdminBase):
    """팀 생성 요청.

    Attributes:
        team_id: 계정 범위에서 유일한 팀 식별자.
        name: 표시 이름.
        monthly_budget_usd: 월 예산. 생략하면 계정 예산만 적용된다.
    """

    team_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class CreateUserRequest(_AdminBase):
    """사용자 생성 요청.

    Attributes:
        user_id: 계정 범위에서 유일한 사용자 식별자.
        name: 표시 이름.
        email: 연락용 메일.
        team_id: 소속 팀 ID.
        monthly_budget_usd: 월 예산. 생략하면 상위 예산만 적용된다.
    """

    user_id: str = pydantic.Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    name: str = pydantic.Field(min_length=1, max_length=128)
    email: str = ""
    team_id: str = ""
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class CreateApiKeyRequest(_AdminBase):
    """API 키 발급 요청.

    Attributes:
        user_id: 키를 귀속시킬 사용자 ID. 반드시 존재해야 한다.
        name: 키 용도 메모.
        allowed_models: 허용 모델 목록. 비어 있으면 서버 기본 정책을 따른다.
        monthly_budget_usd: 키 월 예산.
    """

    user_id: str = pydantic.Field(min_length=1)
    name: str = ""
    allowed_models: list[str] = pydantic.Field(default_factory=list)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class UpdateStatusRequest(_AdminBase):
    """활성 상태 변경 요청.

    Attributes:
        status: `active` 또는 `disabled`.
    """

    status: typing.Literal["active", "disabled"]


class UpdateAccountRequest(_AdminBase):
    """계정 수정 요청.

    부분 수정이다. 요청 본문에 실린 필드만 반영한다. `monthly_budget_usd`
    를 `null` 로 명시하면 예산을 무제한으로 되돌린다. 필드를 아예 빼면 기존
    값을 유지한다. 두 경우를 구분하기 위해 `model_fields_set` 을 본다.

    문자열 필드는 `null` 을 허용하지 않는다. 도메인 모델에서 이름은 항상
    문자열이어야 하는데, `null` 을 그대로 저장하면 불변식이 깨지기 때문이다.
    값을 비우려면 예산처럼 `null` 을 쓰는 대신 빈 문자열을 명시해야 하는
    필드(이메일 등)만 그렇게 다룬다.

    Attributes:
        name: 표시 이름. 보내면 비어 있지 않아야 한다.
        monthly_budget_usd: 월 예산. `null` 이면 무제한.
    """

    name: str = pydantic.Field(default="", min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class UpdateTeamRequest(_AdminBase):
    """팀 수정 요청. 규칙은 `UpdateAccountRequest` 와 같다.

    Attributes:
        name: 표시 이름. 보내면 비어 있지 않아야 한다.
        monthly_budget_usd: 월 예산. `null` 이면 상위 예산만 적용.
    """

    name: str = pydantic.Field(default="", min_length=1, max_length=128)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class UpdateUserRequest(_AdminBase):
    """사용자 수정 요청. 규칙은 `UpdateAccountRequest` 와 같다.

    문자열 필드는 `null` 을 허용하지 않는다. 이메일과 팀은 빈 문자열로
    비울 수 있다(팀은 빈 문자열이면 팀 없음).

    Attributes:
        name: 표시 이름. 보내면 비어 있지 않아야 한다.
        email: 연락용 메일. 빈 문자열이면 지운다.
        team_id: 소속 팀 ID. 빈 문자열이면 팀 없음으로 만든다.
        monthly_budget_usd: 월 예산. `null` 이면 상위 예산만 적용.
    """

    name: str = pydantic.Field(default="", min_length=1, max_length=128)
    email: str = ""
    team_id: str = ""
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class UpdateApiKeyRequest(_AdminBase):
    """API 키 수정 요청. 규칙은 `UpdateAccountRequest` 와 같다.

    키의 소속(계정·팀·사용자)과 해시는 바꿀 수 없다. 소속을 옮기려면 새
    키를 발급한다. 문자열·목록 필드는 `null` 을 허용하지 않는다.

    Attributes:
        name: 키 용도 메모. 빈 문자열이면 지운다.
        allowed_models: 허용 모델 목록. 빈 목록이면 서버 기본 정책을 따른다.
        monthly_budget_usd: 키 월 예산. `null` 이면 상위 예산만 적용.
    """

    name: str = ""
    allowed_models: list[str] = pydantic.Field(default_factory=list)
    monthly_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class PutAuthConfigRequest(_AdminBase):
    """계정 외부 인증(OIDC) 설정 요청.

    고객이 이미 쓰는 인증 서버를 계정에 붙인다. 발급자는 계정 간에 겹칠 수
    없다. 발급자로 토큰이 어느 계정 것인지 판별하기 때문이다.

    Attributes:
        issuer: OIDC 발급자 URL. 토큰의 `iss` 와 정확히 일치해야 한다.
        jwks_url: JWKS 문서 URL. 생략하면 발급자에서 표준 경로를 만든다.
            https 여야 하고 내부 네트워크 주소는 거부된다.
        audience: 허용 클라이언트 ID. 쉼표로 구분한다. 생략하면 청중을
            검사하지 않는다.
        user_claim: 사용자 ID 로 쓸 클레임 이름.
        team_claim: 팀 ID 로 쓸 클레임 이름. 생략하면 팀 없이 동작한다.
        groups_claim: 그룹 목록 클레임 이름. Cognito 는 `cognito:groups` 다.
        admin_groups: 이 계정의 관리자로 인정할 그룹. 쉼표로 구분한다.
        auto_provision: 사용자가 없을 때 자동 생성할지 여부. 켜면 예산을
            반드시 지정해야 한다.
        provision_allowed_models: 자동 생성 사용자의 허용 모델. 쉼표 구분.
        provision_budget_usd: 자동 생성 사용자의 월 예산.
    """

    issuer: str = pydantic.Field(min_length=8, max_length=512)
    jwks_url: str = pydantic.Field(default="", max_length=512)
    audience: str = pydantic.Field(default="", max_length=512)
    user_claim: str = pydantic.Field(default="email", max_length=64)
    team_claim: str = pydantic.Field(default="", max_length=64)
    groups_claim: str = pydantic.Field(default="cognito:groups", max_length=64)
    admin_groups: str = pydantic.Field(default="", max_length=512)
    auto_provision: bool = False
    provision_allowed_models: str = pydantic.Field(default="", max_length=1024)
    provision_budget_usd: decimal.Decimal | None = pydantic.Field(
        default=None, ge=0
    )


class SelfIssueKeyRequest(_AdminBase):
    """셀프서비스 키 발급 요청.

    계정·사용자는 토큰에서 결정하므로 본문으로 받지 않는다. 받으면 다른
    사용자에게 키를 발급하는 경로가 열린다.

    Attributes:
        name: 키 표시 이름. 어디에 쓰는 키인지 구분하는 용도다.
        allowed_models: 이 키로 호출할 수 있는 모델. 생략하면 계정 설정의
            기본값을 따른다. 계정이 정한 범위를 넘길 수는 없다.
    """

    name: str = pydantic.Field(min_length=1, max_length=128)
    allowed_models: list[str] = pydantic.Field(default_factory=list)
