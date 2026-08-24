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


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 설정을 반환한다.

    환경변수는 프로세스 수명 동안 바뀌지 않으므로 캐시한다. 테스트에서
    환경변수를 바꿀 때는 `get_settings.cache_clear()` 를 호출한다.

    Returns:
        검증된 `Settings` 인스턴스.
    """
    return Settings()
