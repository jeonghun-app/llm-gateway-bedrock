"""환경변수 기반 애플리케이션 설정.

모든 설정은 `LLMGW_` 접두어를 가진 환경변수로 주입한다. 리전만은 ECS가
자동으로 넣어주는 `AWS_REGION` 을 함께 받아들인다.

시크릿(관리 토큰)은 이미지나 코드에 넣지 않는다. ECS 태스크 정의의
`secrets` 필드로 Secrets Manager 값을 주입받아 `LLMGW_ADMIN_TOKEN` 으로
전달된다.
"""

from __future__ import annotations

import functools
import pathlib
import typing

import pydantic
import pydantic_settings

_PACKAGE_DIR = pathlib.Path(__file__).resolve().parent

# usage 원본 레코드 보존 기간의 기본값. 집계 테이블은 만료시키지 않으므로
# 대시보드 수치는 이 기간이 지나도 유지된다.
_DEFAULT_USAGE_TTL_DAYS = 90


class Settings(pydantic_settings.BaseSettings):
    """게이트웨이 런타임 설정.

    Attributes:
        env: 배포 환경 식별자(`dev`/`stg`/`prod`). 리소스 이름과 API 키
            접두어에 쓰인다.
        aws_region: DynamoDB 등 컨트롤 플레인 호출에 사용할 리전.
        bedrock_region: Bedrock 호출 리전. 비어 있으면 `aws_region` 을 쓴다.
            모델 가용성이 리전마다 달라 분리해 두었다.
        registry_table: 계정·팀·사용자·API 키 테이블 이름.
        usage_table: 요청 단위 원본 사용량 테이블 이름.
        usage_agg_table: 사전 집계 카운터 테이블 이름.
        admin_token: 관리 API와 대시보드 인증에 쓰는 토큰. 빈 값이면 관리
            API가 전부 503을 반환한다. 실수로 인증 없이 열리는 것을 막기
            위해 기본값을 빈 문자열로 두었다.
        default_allowed_models: API 키에 모델 허용 목록이 없을 때 적용할
            기본 허용 목록. 쉼표로 구분한다. 빈 값이면 모든 모델을 허용한다.
        usage_ttl_days: usage 원본 레코드 TTL(일).
        pricing_file: 모델 단가 표 JSON 경로.
        log_level: 로그 레벨.
        service_name: 구조화 로그의 `service` 필드 값.
        metrics_namespace: CloudWatch EMF 메트릭 네임스페이스.
        request_timeout_seconds: Bedrock 호출 읽기 타임아웃.
        request_filters: 활성화할 요청 필터 확장. `module:Class` 명세를 쉼표로
            구분한다. 적은 순서가 적용 순서다. 비면 확장을 쓰지 않는다.
        extension_timeout_seconds: 확장 하나의 제한 시간(초).
    """

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="LLMGW_",
        env_file=None,
        extra="ignore",
        frozen=True,
    )

    env: str = "dev"

    aws_region: str = pydantic.Field(
        default="us-east-1",
        validation_alias=pydantic.AliasChoices(
            "LLMGW_AWS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"
        ),
    )
    bedrock_region: str = ""

    registry_table: str = "llmgw-dev-registry"
    usage_table: str = "llmgw-dev-usage"
    usage_agg_table: str = "llmgw-dev-usage-agg"

    admin_token: str = ""

    default_allowed_models: str = ""

    usage_ttl_days: int = pydantic.Field(
        default=_DEFAULT_USAGE_TTL_DAYS, ge=1, le=3650
    )
    pricing_file: str = str(_PACKAGE_DIR / "pricing.json")

    log_level: str = "INFO"
    service_name: str = "llmgw"
    metrics_namespace: str = "LLMGateway"

    request_timeout_seconds: int = pydantic.Field(default=300, ge=1, le=900)

    # -- 외부 인증(OIDC) ---------------------------------------------------
    # 발급자·청중·클레임 매핑은 계정별 설정(`AccountAuthConfig`)에만 둔다.
    # 전역과 계정별 두 곳에 같은 설정이 있으면 어느 쪽이 적용됐는지 추적하기
    # 어렵고, 한쪽만 고쳐 사고가 난다.
    #
    # 여기 있는 값은 계정 경계를 넘는 것뿐이다.
    #
    # 이 그룹을 가진 토큰은 모든 계정을 관리할 수 있다(공유 관리 토큰과
    # 동등). 비어 있으면 플랫폼 관리자를 토큰으로 부여할 수 없고, 계정별
    # `admin_groups` 로 자기 계정만 관리하게 된다.
    oidc_platform_admin_groups: str = ""

    # 단가 표에 없는 모델을 어떻게 다루는지.
    #
    # AWS Price List API 에도 없는 신규 모델이 존재하므로 단가를 자동으로
    # 채울 수 없다. 추측한 값을 넣으면 틀린 청구가 되므로, 대신 운영자가
    # 정책을 고르게 한다.
    #
    #   allow  : 통과시키고 비용 0으로 기록한다(기존 동작, 기본값).
    #            비용 귀속이 부정확해지지만 새 모델을 즉시 쓸 수 있다.
    #   reject : 요청을 거부한다. 비용 귀속을 보장하지만 단가를 등록하기
    #            전까지 그 모델을 쓸 수 없다.
    #   hide   : 통과시키되 `/v1/models` 에서 감춘다. 클라이언트가 실수로
    #            고르는 것을 막으면서 명시적 사용은 허용한다.
    unpriced_model_policy: typing.Literal["allow", "reject", "hide"] = "allow"

    # 활성화할 요청 필터 확장. `module:Class` 를 쉼표로 구분한다.
    #
    # 확장은 게이트웨이 프로세스 안에서 신뢰된 코드로 돈다. 프롬프트를 읽고
    # 태스크 역할 자격증명에 접근할 수 있다. 설치만으로 활성화되면 의존성을
    # 하나 추가한 것이 그 권한을 준 것이 되므로, 여기 명시한 것만 import
    # 하고 실행한다. 적은 순서가 적용 순서이며 순서가 결과를 바꾼다.
    request_filters: str = ""

    # 확장 하나의 제한 시간. Bedrock 읽기 타임아웃(기본 300초)보다 훨씬 짧게
    # 잡는다. 필터는 지역 검사나 짧은 정책 조회여야 하고, 여기서 오래
    # 기다리면 게이트웨이의 워커가 묶인다.
    extension_timeout_seconds: float = pydantic.Field(
        default=1.0, gt=0.0, le=30.0
    )

    @property
    def oidc_platform_admin_group_list(self) -> tuple[str, ...]:
        """플랫폼 관리자로 인정할 그룹 목록.

        Returns:
            공백이 제거된 튜플. 설정이 비어 있으면 빈 튜플.
        """
        raw = self.oidc_platform_admin_groups.strip()
        if not raw:
            return ()
        return tuple(item.strip() for item in raw.split(",") if item.strip())

    @property
    def effective_bedrock_region(self) -> str:
        """Bedrock 호출에 실제로 사용할 리전을 반환한다."""
        return self.bedrock_region or self.aws_region

    @property
    def default_allowed_model_list(self) -> tuple[str, ...]:
        """쉼표 구분 문자열을 모델 ID 튜플로 변환한다.

        환경변수로 리스트를 넘길 때 JSON 파싱을 요구하지 않기 위해
        문자열로 받고 여기서 분해한다.

        Returns:
            공백이 제거된 모델 ID 튜플. 설정이 비어 있으면 빈 튜플.
        """
        raw = self.default_allowed_models.strip()
        if not raw:
            return ()
        return tuple(item.strip() for item in raw.split(",") if item.strip())

    @property
    def request_filter_list(self) -> tuple[str, ...]:
        """활성 요청 필터 명세를 순서대로 반환한다.

        Returns:
            `module:Class` 문자열 튜플. 설정이 비어 있으면 빈 튜플.
        """
        raw = self.request_filters.strip()
        if not raw:
            return ()
        return tuple(item.strip() for item in raw.split(",") if item.strip())


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 설정을 반환한다.

    환경변수는 프로세스 수명 동안 바뀌지 않으므로 캐시한다. 테스트에서
    환경변수를 바꿀 때는 `get_settings.cache_clear()` 를 호출한다.

    Returns:
        검증된 `Settings` 인스턴스.
    """
    return Settings()
