"""헬스 체크와 예외 처리 테스트."""

from __future__ import annotations

import dataclasses
import typing

import botocore.exceptions
from fastapi import testclient

import conftest
from llmgw import app as app_module
from llmgw import services as services_module


class _BrokenRegistry:
    """DynamoDB 접근이 실패하는 레지스트리 대역."""

    def get_account(self, account_id: str) -> None:
        """항상 접근 거부를 던진다."""
        del account_id
        raise botocore.exceptions.ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "denied",
                }
            },
            "GetItem",
        )


class _ExplodingBedrock(conftest.FakeBedrock):
    """모델 목록이 비어 있는 Bedrock 대역."""

    def list_model_ids(self) -> tuple[str, ...]:
        """빈 목록을 반환한다."""
        return ()


def _client_with(
    app_services: services_module.Services, **replacements: typing.Any
) -> testclient.TestClient:
    """일부 의존성을 바꿔 끼운 테스트 클라이언트를 만든다."""
    patched = dataclasses.replace(app_services, **replacements)
    return testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    )


def test_readyz_정상이면200(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"] == {"dynamodb": "ok", "bedrock": "ok"}
    assert body["model_count"] == 3


def test_readyz_DynamoDB실패시503(
    app_services: services_module.Services,
) -> None:
    # Arrange
    client = _client_with(
        app_services,
        registry=typing.cast("typing.Any", _BrokenRegistry()),
    )

    # Act
    with client:
        response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["dynamodb"].startswith("failed: ClientError")


def test_readyz_모델목록이비면503(
    app_services: services_module.Services,
) -> None:
    """Bedrock 모델 액세스가 꺼져 있으면 준비되지 않은 상태다."""
    # Arrange
    client = _client_with(
        app_services,
        bedrock=typing.cast("typing.Any", _ExplodingBedrock()),
    )

    # Act
    with client:
        response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    assert response.json()["checks"]["bedrock"].startswith("failed")


def test_readyz_실패메시지에내부정보를노출하지않는다(
    app_services: services_module.Services,
) -> None:
    # Arrange
    client = _client_with(
        app_services,
        registry=typing.cast("typing.Any", _BrokenRegistry()),
    )

    # Act
    with client:
        body = client.get("/readyz").json()

    # Assert
    assert (
        "denied" not in body["checks"]["dynamodb"]
    ), "AWS 원문 메시지를 그대로 노출하면 안 된다"


def test_없는경로_404이고OpenAI형식본문(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/does-not-exist")

    # Assert
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["type"] == "invalid_request_error"


def test_허용되지않은메서드_405이고OpenAI형식본문(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.delete("/healthz")

    # Assert
    assert response.status_code == 405
    assert "error" in response.json()


def test_예상하지못한예외_500이고내부메시지를숨긴다(
    app_services: services_module.Services,
) -> None:
    """예외 메시지에 내부 구조가 섞여 나가면 안 된다."""

    # Arrange
    class _Boom:
        def get_account(self, account_id: str) -> None:
            del account_id
            raise RuntimeError("internal-secret-detail")

    client = _client_with(
        app_services, registry=typing.cast("typing.Any", _Boom())
    )

    # Act
    with client:
        response = client.get(
            "/admin/accounts/acme",
            headers={"X-Admin-Token": app_services.settings.admin_token},
        )

    # Assert
    assert response.status_code == 500
    body = response.json()
    assert "internal-secret-detail" not in body["error"]["message"]
    assert body["error"]["type"] == "server_error"


def test_openapi_스펙이생성된다(
    client: testclient.TestClient,
) -> None:
    """스펙 생성이 깨지면 /docs 도 함께 깨진다."""
    # Arrange / Act
    response = client.get("/openapi.json")

    # Assert
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/v1/chat/completions" in paths
    assert "/analytics/dashboard" in paths
    assert "/admin/accounts" in paths


def test_대시보드정적파일이서빙된다(
    client: testclient.TestClient,
) -> None:
    # Arrange / Act
    response = client.get("/ui/")

    # Assert
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_DynamoDB테이블없음_503이고AWS코드를알려준다(
    app_services: services_module.Services,
) -> None:
    """첫 배포에서 가장 흔한 실패라 원인을 바로 좁힐 수 있어야 한다."""

    # Arrange
    class _MissingTable:
        def list_accounts(self) -> None:
            raise botocore.exceptions.ClientError(
                {
                    "Error": {
                        "Code": "ResourceNotFoundException",
                        "Message": "Requested resource not found",
                    }
                },
                "Query",
            )

    client = _client_with(
        app_services, registry=typing.cast("typing.Any", _MissingTable())
    )

    # Act
    with client:
        response = client.get(
            "/admin/accounts",
            headers={"X-Admin-Token": app_services.settings.admin_token},
        )

    # Assert
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "storage_unavailable"
    assert "ResourceNotFoundException" in body["error"]["message"]
    assert (
        "Requested resource not found" not in body["error"]["message"]
    ), "AWS 원문 메시지는 로그에만 남겨야 한다"


def test_DynamoDB연결오류도503으로변환된다(
    app_services: services_module.Services,
) -> None:
    # Arrange
    class _Unreachable:
        def list_accounts(self) -> None:
            raise botocore.exceptions.EndpointConnectionError(
                endpoint_url="https://dynamodb.us-east-1.amazonaws.com"
            )

    client = _client_with(
        app_services, registry=typing.cast("typing.Any", _Unreachable())
    )

    # Act
    with client:
        response = client.get(
            "/admin/accounts",
            headers={"X-Admin-Token": app_services.settings.admin_token},
        )

    # Assert
    assert response.status_code == 503
    assert "EndpointConnectionError" in response.json()["error"]["message"]
