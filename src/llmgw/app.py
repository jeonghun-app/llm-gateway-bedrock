"""FastAPI 애플리케이션 팩토리.

미들웨어에서 요청마다 상관관계 ID 를 만들어 컨텍스트에 넣고, 응답 헤더와
액세스 로그에 같은 값을 남긴다. 클라이언트가 `X-Request-Id` 를 보내면 그
값을 그대로 쓴다. 사용량 기록의 멱등성 키와 같은 값이라 로그에서 특정
요청의 집계 반영 여부를 추적할 수 있다.

예외는 세 갈래로 처리한다.

- `GatewayError`: 도메인이 의도한 실패. 상태 코드와 OpenAI 호환 본문을
  예외가 들고 있다.
- `RequestValidationError`: 스키마 검증 실패. 400 + 필드별 사유.
- 그 외: 예상하지 못한 오류. 500 + 일반화된 메시지. 내부 예외 메시지를
  클라이언트에 노출하지 않는다.
"""

from __future__ import annotations

import http
import pathlib
import time
import typing

import botocore.exceptions
import fastapi
from fastapi import exceptions as fastapi_exceptions
from fastapi import responses
from fastapi import staticfiles
from starlette import exceptions as starlette_exceptions

import llmgw
from llmgw import config
from llmgw import errors
from llmgw import observability
from llmgw import services as services_module
from llmgw.routers import admin as admin_router
from llmgw.routers import analytics as analytics_router
from llmgw.routers import auth_self as auth_self_router
from llmgw.routers import health as health_router
from llmgw.routers import openai_compat as openai_router

_REQUEST_ID_HEADER = "X-Request-Id"
_STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

# 액세스 로그를 남기지 않는 경로. ALB 헬스 체크가 30초마다 호출해
# 로그 대부분을 차지하고 CloudWatch Logs 비용만 늘린다.
_QUIET_PATHS = frozenset({"/healthz"})

# 대시보드 정적 애셋 경로. 배포 후에도 브라우저가 옛 CSS/JS 를 쓰지 않도록
# 재검증을 강제한다.
_UI_PATH_PREFIX = "/ui"

_DESCRIPTION = """
Amazon Bedrock 앞단의 OpenAI 호환 게이트웨이.

- `POST /v1/chat/completions` — OpenAI Chat Completions 호환 (스트리밍 지원)
- `GET /v1/models` — 호출 가능한 모델 목록
- `/admin/*` — 계정·팀·사용자·API 키 관리 (`X-Admin-Token` 필요)
- `/analytics/*` — 계정·팀·사용자·모델별 사용량 집계 (`X-Admin-Token` 필요)
- `/ui` — 모니터링 대시보드
""".strip()


def create_app(settings: config.Settings | None = None) -> fastapi.FastAPI:
    """FastAPI 앱을 만든다.

    Args:
        settings: 사용할 설정. 생략하면 환경변수에서 읽는다.

    Returns:
        구성된 FastAPI 앱.
    """
    resolved = settings or config.get_settings()
    services = services_module.build_services(resolved)
    return create_app_with_services(services)


def create_app_with_services(
    services: services_module.Services,
) -> fastapi.FastAPI:
    """이미 조립된 서비스 컨테이너로 앱을 만든다.

    테스트는 moto 로 만든 저장소를 담은 컨테이너를 직접 넘긴다.

    Args:
        services: 서비스 컨테이너.

    Returns:
        구성된 FastAPI 앱.
    """
    app = fastapi.FastAPI(
        title="LLM Gateway",
        description=_DESCRIPTION,
        version=llmgw.__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.services = services

    _register_middleware(app, services)
    _register_exception_handlers(app, services)

    app.include_router(health_router.router)
    app.include_router(openai_router.router)
    app.include_router(admin_router.router)
    app.include_router(analytics_router.router)
    app.include_router(auth_self_router.router)

    if _STATIC_DIR.is_dir():
        app.mount(
            "/ui",
            staticfiles.StaticFiles(directory=_STATIC_DIR, html=True),
            name="ui",
        )

    @app.get("/", include_in_schema=False)
    def root() -> responses.RedirectResponse:
        """루트 접근을 대시보드로 보낸다."""
        return responses.RedirectResponse(url="/ui/")

    return app


def _register_middleware(
    app: fastapi.FastAPI, services: services_module.Services
) -> None:
    """상관관계 ID 와 액세스 로그 미들웨어를 등록한다."""

    @app.middleware("http")
    async def correlate_and_log(
        request: fastapi.Request,
        call_next: typing.Callable[
            [fastapi.Request], typing.Awaitable[fastapi.Response]
        ],
    ) -> fastapi.Response:
        """요청 ID 를 컨텍스트에 심고 처리 시간을 로그로 남긴다."""
        incoming = (request.headers.get(_REQUEST_ID_HEADER) or "").strip()
        request_id = incoming or services.id_factory.new_id()
        token = observability.set_correlation_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            response.headers[_REQUEST_ID_HEADER] = request_id
            # 대시보드 애셋은 항상 재검증하게 만든다. Cache-Control 이 없으면
            # 브라우저가 휴리스틱 캐싱으로 옛 CSS/JS 를 계속 쓰고, 배포해도
            # 화면이 바뀌지 않는다. `no-cache` 는 캐시 금지가 아니라 "쓰기
            # 전에 물어봐라" 라서, 내용이 그대로면 ETag 로 304 가 돌아가
            # 전송량은 늘지 않는다.
            if request.url.path.startswith(_UI_PATH_PREFIX):
                response.headers["Cache-Control"] = "no-cache"
            # 로깅은 컨텍스트를 되돌리기 전에 해야 한다. reset 을 먼저
            # 실행하면 이 줄에 correlation_id 가 붙지 않아, status_code 와
            # duration_ms 를 가진 유일한 로그를 요청 ID 로 조회할 수 없다.
            if request.url.path not in _QUIET_PATHS:
                services.logger.info(
                    "요청 처리 완료",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": elapsed_ms,
                    },
                )
        finally:
            observability.reset_correlation_id(token)

        return response


def _register_exception_handlers(
    app: fastapi.FastAPI, services: services_module.Services
) -> None:
    """예외 핸들러를 등록한다."""

    @app.exception_handler(errors.GatewayError)
    async def handle_gateway_error(
        request: fastapi.Request, exc: Exception
    ) -> responses.JSONResponse:
        """도메인 예외를 OpenAI 호환 에러 본문으로 변환한다."""
        del request
        gateway_error = typing.cast("errors.GatewayError", exc)
        return responses.JSONResponse(
            status_code=gateway_error.status_code,
            content=gateway_error.to_payload(),
        )

    @app.exception_handler(fastapi_exceptions.RequestValidationError)
    async def handle_validation_error(
        request: fastapi.Request, exc: Exception
    ) -> responses.Response:
        """검증 실패를 OpenAI 호환 형태로 바꿔 준다.

        FastAPI 기본 응답은 `detail` 배열이라 OpenAI 클라이언트가 파싱하지
        못한다. 필드 경로와 사유를 사람이 읽을 수 있는 한 문장으로 합친다.
        """
        del request
        validation_error = typing.cast(
            "fastapi_exceptions.RequestValidationError", exc
        )
        problems = [
            f"{'.'.join(str(part) for part in item.get('loc', []))}:"
            f" {item.get('msg', '')}"
            for item in validation_error.errors()
        ]
        message = "요청 본문이 올바르지 않다. " + "; ".join(problems)
        return responses.JSONResponse(
            status_code=http.HTTPStatus.BAD_REQUEST,
            content=errors.InvalidRequestError(message).to_payload(),
        )

    # Starlette 의 기반 클래스에 등록해야 라우팅 단계에서 나오는 404·405 도
    # 잡힌다. FastAPI 의 HTTPException 은 이 클래스의 서브클래스라 함께
    # 처리된다. FastAPI 쪽에만 등록하면 없는 경로 응답이 기본 `detail`
    # 형식으로 나가 OpenAI 클라이언트가 파싱하지 못한다.
    @app.exception_handler(starlette_exceptions.HTTPException)
    async def handle_http_exception(
        request: fastapi.Request, exc: Exception
    ) -> responses.Response:
        """HTTP 예외를 OpenAI 호환 본문 형식으로 맞춘다."""
        http_error = typing.cast("starlette_exceptions.HTTPException", exc)
        if http_error.status_code == http.HTTPStatus.NOT_FOUND:
            wrapped: errors.GatewayError = errors.ResourceNotFoundError(
                str(http_error.detail)
            )
        else:
            wrapped = errors.GatewayError(str(http_error.detail))
            wrapped.status_code = http_error.status_code
        response = responses.JSONResponse(
            status_code=http_error.status_code,
            content=wrapped.to_payload(),
        )
        if http_error.headers:
            response.headers.update(http_error.headers)
        del request
        return response

    @app.exception_handler(botocore.exceptions.ClientError)
    @app.exception_handler(botocore.exceptions.BotoCoreError)
    async def handle_storage_error(
        request: fastapi.Request, exc: Exception
    ) -> responses.Response:
        """DynamoDB 오류를 503 으로 바꾼다.

        테이블 미생성과 IAM 권한 부족이 첫 배포에서 가장 흔한 실패다. 일반
        500 으로 내려보내면 운영자가 애플리케이션 버그와 구분하지 못한다.
        AWS 에러 코드는 응답에 포함한다. 코드 자체에는 계정 정보나 내부
        호스트명이 들어가지 않고, 원인 파악에 바로 쓰인다. 반면 AWS 원문
        메시지는 로그에만 남긴다.

        `ClientError` 는 `BotoCoreError` 의 서브클래스가 아니라 둘 다 따로
        등록해야 한다.
        """
        aws_code = "Unknown"
        if isinstance(exc, botocore.exceptions.ClientError):
            aws_code = str(
                exc.response.get("Error", {}).get("Code") or "Unknown"
            )
        else:
            aws_code = type(exc).__name__

        services.logger.exception(
            "저장소 접근에 실패했다",
            extra={
                "method": request.method,
                "path": request.url.path,
                "aws_error_code": aws_code,
            },
        )
        message = (
            f"저장소에 접근할 수 없다 (AWS 코드: {aws_code})."
            " DynamoDB 테이블 존재 여부와 태스크 역할 권한을 확인한다."
        )
        return responses.JSONResponse(
            status_code=http.HTTPStatus.SERVICE_UNAVAILABLE,
            content=errors.StorageUnavailableError(message).to_payload(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(
        request: fastapi.Request, exc: Exception
    ) -> responses.Response:
        """예상하지 못한 예외를 500 으로 감싼다.

        예외 메시지에 내부 구조나 자격증명 조각이 들어갈 수 있어 클라이언트
        에게는 일반화된 문구만 준다. 원인은 스택트레이스와 함께 로그로 남긴다.
        """
        services.logger.exception(
            "처리되지 않은 예외",
            extra={
                "method": request.method,
                "path": request.url.path,
                "exception_type": type(exc).__name__,
            },
        )
        return responses.JSONResponse(
            status_code=http.HTTPStatus.INTERNAL_SERVER_ERROR,
            content=errors.GatewayError(
                "게이트웨이 내부 오류가 발생했다. 요청 ID 로 로그를 확인한다."
            ).to_payload(),
        )
