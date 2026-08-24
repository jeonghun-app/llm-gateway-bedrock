"""헬스 체크 라우터.

`/healthz` 는 ALB 타깃 그룹이 30초마다 호출한다. 그래서 AWS API 를 전혀
호출하지 않는 얕은 체크로 둔다. 여기서 DynamoDB 를 확인하면 헬스 체크가
비용을 발생시키고, 일시적인 DynamoDB 지연이 태스크 교체로 번진다.

의존성까지 확인하는 깊은 체크는 `/readyz` 로 분리했다. 배포 직후 사람이
한 번 호출하거나 런북에서 장애를 진단할 때 쓴다.
"""

from __future__ import annotations

import http
import typing

import botocore.exceptions
import fastapi

import llmgw
from llmgw import services as services_module

router = fastapi.APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(services: services_module.ServicesDep) -> dict[str, str]:
    """프로세스가 요청을 받을 수 있는지 반환한다.

    Args:
        services: 서비스 컨테이너.

    Returns:
        상태와 버전, 환경 정보.
    """
    return {
        "status": "ok",
        "version": llmgw.__version__,
        "env": services.settings.env,
    }


@router.get("/readyz")
def readyz(
    services: services_module.ServicesDep,
    response: fastapi.Response,
) -> dict[str, typing.Any]:
    """DynamoDB 와 Bedrock 접근 가능 여부까지 확인한다.

    한쪽이라도 실패하면 503 을 반환하되, 어느 쪽이 실패했는지 본문에
    남긴다. 실패 원인 메시지는 예외 타입만 담아 자격증명이나 내부
    호스트명이 노출되지 않게 한다.

    Args:
        services: 서비스 컨테이너.
        response: 상태 코드를 설정할 응답 객체.

    Returns:
        의존성별 확인 결과.
    """
    checks: dict[str, str] = {}

    try:
        services.registry.get_account("__readyz_probe__")
        checks["dynamodb"] = "ok"
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.BotoCoreError,
    ) as exc:
        checks["dynamodb"] = f"failed: {type(exc).__name__}"
        services.logger.exception("readyz DynamoDB 확인 실패")

    model_ids = services.bedrock.list_model_ids()
    checks["bedrock"] = "ok" if model_ids else "failed: no models listed"

    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = http.HTTPStatus.SERVICE_UNAVAILABLE
    return {
        "ready": ready,
        "checks": checks,
        "model_count": len(model_ids),
    }
