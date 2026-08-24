"""관리 API 라우터.

계정 → 팀 → 사용자 → API 키를 만들고 조회한다. 모든 엔드포인트가
`X-Admin-Token` 헤더를 요구한다.

키 발급 응답은 평문 키를 딱 한 번 돌려준다. 이후에는 어디에서도 조회할 수
없다. 저장소에 해시만 남기기 때문이다.
"""

from __future__ import annotations

import http
import typing

import fastapi

from llmgw import apikey
from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import schemas
from llmgw import services as services_module

router = fastapi.APIRouter(prefix="/admin", tags=["admin"])

_JsonDict = dict[str, typing.Any]


def _account_payload(account: domain.Account) -> _JsonDict:
    """계정을 API 응답용 딕셔너리로 만든다."""
    return {
        "account_id": account.account_id,
        "name": account.name,
        "monthly_budget_usd": (
            float(account.monthly_budget_usd)
            if account.monthly_budget_usd is not None
            else None
        ),
        "status": account.status.value,
        "created_at": account.created_at,
    }


def _team_payload(team: domain.Team) -> _JsonDict:
    """팀을 API 응답용 딕셔너리로 만든다."""
    return {
        "account_id": team.account_id,
        "team_id": team.team_id,
        "name": team.name,
        "monthly_budget_usd": (
            float(team.monthly_budget_usd)
            if team.monthly_budget_usd is not None
            else None
        ),
        "status": team.status.value,
        "created_at": team.created_at,
    }


def _user_payload(user: domain.User) -> _JsonDict:
    """사용자를 API 응답용 딕셔너리로 만든다."""
    return {
        "account_id": user.account_id,
        "user_id": user.user_id,
        "name": user.name,
        "email": user.email,
        "team_id": user.team_id,
        "monthly_budget_usd": (
            float(user.monthly_budget_usd)
            if user.monthly_budget_usd is not None
            else None
        ),
        "status": user.status.value,
        "created_at": user.created_at,
    }


def _key_payload(api_key: domain.ApiKey) -> _JsonDict:
    """API 키 메타데이터를 응답용 딕셔너리로 만든다.

    `key_hash` 는 응답에 넣지 않는다. 해시만으로 인증이 되지는 않지만,
    관리 화면에 노출할 이유가 없는 값이다.
    """
    return {
        "key_id": api_key.key_id,
        "key_prefix": api_key.key_prefix,
        "account_id": api_key.account_id,
        "team_id": api_key.team_id,
        "user_id": api_key.user_id,
        "name": api_key.name,
        "allowed_models": list(api_key.allowed_models),
        "monthly_budget_usd": (
            float(api_key.monthly_budget_usd)
            if api_key.monthly_budget_usd is not None
            else None
        ),
        "status": api_key.status.value,
        "created_at": api_key.created_at,
        "last_used_at": api_key.last_used_at,
    }


def _require_account(
    services: services_module.Services, account_id: str
) -> domain.Account:
    """계정을 조회하고 없으면 404 를 낸다."""
    account = services.registry.get_account(account_id)
    if account is None:
        raise errors.ResourceNotFoundError(f"계정을 찾을 수 없다: {account_id}")
    return account


# -- 계정 -------------------------------------------------------------------


@router.post("/accounts", status_code=http.HTTPStatus.CREATED)
def create_account(
    payload: schemas.CreateAccountRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정을 만든다.

    Args:
        payload: 생성 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        생성된 계정.

    Raises:
        ResourceConflictError: 같은 ID 의 계정이 이미 있는 경우.
    """
    account = domain.Account(
        account_id=payload.account_id,
        name=payload.name,
        monthly_budget_usd=payload.monthly_budget_usd,
        created_at=clock.to_iso(services.clock.now()),
    )
    services.registry.put_account(account)
    services.logger.info(
        "계정을 생성했다", extra={"account_id": account.account_id}
    )
    return _account_payload(account)


@router.get("/accounts")
def list_accounts(services: services_module.AdminDep) -> _JsonDict:
    """모든 계정을 반환한다.

    Args:
        services: 인증된 서비스 컨테이너.

    Returns:
        계정 목록.
    """
    accounts = services.registry.list_accounts()
    return {"data": [_account_payload(item) for item in accounts]}


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정 하나를 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        계정 정보.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
    """
    return _account_payload(_require_account(services, account_id))


@router.post("/accounts/{account_id}/status")
def update_account_status(
    account_id: str,
    payload: schemas.UpdateStatusRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정을 활성/비활성 전환한다.

    비활성 계정의 API 키는 모두 즉시 거부된다. 다만 인증 경로의 메타데이터
    캐시 TTL(기본 30초)만큼 반영이 지연될 수 있다.

    Args:
        account_id: 계정 ID.
        payload: 변경 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        변경된 계정.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
    """
    account = _require_account(services, account_id)
    updated = account.model_copy(
        update={"status": domain.EntityStatus(payload.status)}
    )
    services.registry.put_account(updated, overwrite=True)
    services.logger.info(
        "계정 상태를 변경했다",
        extra={"account_id": account_id, "status": payload.status},
    )
    return _account_payload(updated)


# -- 팀 ---------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/teams", status_code=http.HTTPStatus.CREATED
)
def create_team(
    account_id: str,
    payload: schemas.CreateTeamRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """팀을 만든다.

    Args:
        account_id: 계정 ID.
        payload: 생성 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        생성된 팀.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
        ResourceConflictError: 같은 팀 ID 가 이미 있는 경우.
    """
    _require_account(services, account_id)
    team = domain.Team(
        account_id=account_id,
        team_id=payload.team_id,
        name=payload.name,
        monthly_budget_usd=payload.monthly_budget_usd,
        created_at=clock.to_iso(services.clock.now()),
    )
    services.registry.put_team(team)
    return _team_payload(team)


@router.get("/accounts/{account_id}/teams")
def list_teams(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정의 팀 목록을 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        팀 목록.
    """
    teams = services.registry.list_teams(account_id)
    return {"data": [_team_payload(item) for item in teams]}


# -- 사용자 -----------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/users", status_code=http.HTTPStatus.CREATED
)
def create_user(
    account_id: str,
    payload: schemas.CreateUserRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """사용자를 만든다.

    Args:
        account_id: 계정 ID.
        payload: 생성 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        생성된 사용자.

    Raises:
        ResourceNotFoundError: 계정이나 지정한 팀이 없는 경우.
        ResourceConflictError: 같은 사용자 ID 가 이미 있는 경우.
    """
    _require_account(services, account_id)
    if payload.team_id and (
        services.registry.get_team(account_id, payload.team_id) is None
    ):
        raise errors.ResourceNotFoundError(
            f"팀을 찾을 수 없다: {payload.team_id}"
        )
    user = domain.User(
        account_id=account_id,
        user_id=payload.user_id,
        name=payload.name,
        email=payload.email,
        team_id=payload.team_id,
        monthly_budget_usd=payload.monthly_budget_usd,
        created_at=clock.to_iso(services.clock.now()),
    )
    services.registry.put_user(user)
    return _user_payload(user)


@router.get("/accounts/{account_id}/users")
def list_users(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정의 사용자 목록을 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        사용자 목록.
    """
    users = services.registry.list_users(account_id)
    return {"data": [_user_payload(item) for item in users]}


# -- API 키 -----------------------------------------------------------------


@router.post("/accounts/{account_id}/keys", status_code=http.HTTPStatus.CREATED)
def create_api_key(
    account_id: str,
    payload: schemas.CreateApiKeyRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """API 키를 발급한다.

    팀 ID 는 요청에서 받지 않고 사용자 레코드에서 가져온다. 사용자와 키의
    팀이 어긋나면 집계가 사용자 축과 팀 축에서 서로 다른 값을 내기 때문이다.

    Args:
        account_id: 계정 ID.
        payload: 발급 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        키 메타데이터와 평문 키(`api_key`). 평문은 이 응답에서만 볼 수 있다.

    Raises:
        ResourceNotFoundError: 계정이나 사용자가 없는 경우.
    """
    _require_account(services, account_id)
    user = services.registry.get_user(account_id, payload.user_id)
    if user is None:
        raise errors.ResourceNotFoundError(
            f"사용자를 찾을 수 없다: {payload.user_id}"
        )

    generated = apikey.generate_api_key(services.settings.env)
    api_key = domain.ApiKey(
        key_id=services.id_factory.new_id()[:12],
        key_hash=generated.key_hash,
        key_prefix=generated.key_prefix,
        account_id=account_id,
        team_id=user.team_id,
        user_id=user.user_id,
        name=payload.name,
        allowed_models=tuple(payload.allowed_models),
        monthly_budget_usd=payload.monthly_budget_usd,
        created_at=clock.to_iso(services.clock.now()),
    )
    services.registry.put_api_key(api_key)
    services.logger.info(
        "API 키를 발급했다",
        extra={
            "account_id": account_id,
            "user_id": user.user_id,
            "key_id": api_key.key_id,
        },
    )
    payload_out = _key_payload(api_key)
    payload_out["api_key"] = generated.plaintext
    payload_out["warning"] = (
        "평문 키는 이 응답에서만 확인할 수 있다. 안전한 곳에 보관한다."
    )
    return payload_out


@router.get("/accounts/{account_id}/keys")
def list_api_keys(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정의 API 키 목록을 반환한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        키 목록. 평문 키는 포함되지 않는다.
    """
    keys = services.registry.list_api_keys(account_id)
    return {"data": [_key_payload(item) for item in keys]}


@router.delete(
    "/accounts/{account_id}/keys/{key_id}",
    status_code=http.HTTPStatus.NO_CONTENT,
)
def delete_api_key(
    account_id: str, key_id: str, services: services_module.AdminDep
) -> fastapi.Response:
    """API 키를 삭제한다.

    Args:
        account_id: 계정 ID.
        key_id: 삭제할 키 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        본문 없는 204 응답.

    Raises:
        ResourceNotFoundError: 해당 계정에 그 키가 없는 경우.
    """
    services.registry.delete_api_key(account_id, key_id)
    services.logger.info(
        "API 키를 삭제했다",
        extra={"account_id": account_id, "key_id": key_id},
    )
    return fastapi.Response(status_code=http.HTTPStatus.NO_CONTENT)


# -- 모델 -------------------------------------------------------------------


@router.get("/models")
def list_bedrock_models(services: services_module.AdminDep) -> _JsonDict:
    """Bedrock 에서 호출 가능한 모델 목록을 반환한다.

    키 발급 화면의 허용 모델 선택에 쓴다. 단가 표에 있는지 여부를 함께
    내려 보내 비용이 0으로 집계될 모델을 관리자가 알아볼 수 있게 한다.

    Args:
        services: 인증된 서비스 컨테이너.

    Returns:
        모델 ID 와 단가 인지 여부 목록.
    """
    model_ids = services.bedrock.list_model_ids()
    return {
        "data": [
            {
                "model_id": model_id,
                "pricing_known": services.pricing.get(model_id) is not None,
            }
            for model_id in model_ids
        ]
    }
