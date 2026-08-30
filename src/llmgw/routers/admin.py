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
import pydantic

from llmgw import apikey
from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import oidc
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
        "rpm_limit": user.rpm_limit,
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
        "rpm_limit": api_key.rpm_limit,
        "expires_at": api_key.expires_at,
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


def _require_team(
    services: services_module.Services, account_id: str, team_id: str
) -> domain.Team:
    """팀을 조회하고 없으면 404 를 낸다."""
    team = services.registry.get_team(account_id, team_id)
    if team is None:
        raise errors.ResourceNotFoundError(f"팀을 찾을 수 없다: {team_id}")
    return team


def _require_user(
    services: services_module.Services, account_id: str, user_id: str
) -> domain.User:
    """사용자를 조회하고 없으면 404 를 낸다."""
    user = services.registry.get_user(account_id, user_id)
    if user is None:
        raise errors.ResourceNotFoundError(f"사용자를 찾을 수 없다: {user_id}")
    return user


def _apply_updates(
    payload: pydantic.BaseModel, field_map: dict[str, str]
) -> _JsonDict:
    """부분 수정 요청에서 실제로 실린 필드만 골라 변경 딕셔너리를 만든다.

    Pydantic 의 `model_fields_set` 을 보고, 요청 본문에 명시된 필드만
    반영한다. 이렇게 하면 "필드 미지정(기존 값 유지)" 과 "null 로 명시
    (값 초기화)" 를 구분할 수 있다.

    Args:
        payload: `model_fields_set` 을 가진 Pydantic 모델.
        field_map: {요청 필드명: 도메인 필드명} 매핑.

    Returns:
        `model_copy(update=...)` 에 넘길 변경 딕셔너리.
    """
    fields_set = payload.model_fields_set
    changes: _JsonDict = {}
    for request_field, domain_field in field_map.items():
        if request_field in fields_set:
            changes[domain_field] = getattr(payload, request_field)
    return changes


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


@router.patch("/accounts/{account_id}")
def update_account(
    account_id: str,
    payload: schemas.UpdateAccountRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정의 이름·예산을 수정한다.

    요청 본문에 실린 필드만 반영한다. `monthly_budget_usd` 를 `null` 로
    보내면 예산이 무제한으로 바뀐다.

    Args:
        account_id: 계정 ID.
        payload: 수정 요청.
        services: 인증된 서비스 컨테이너.

    Returns:
        수정된 계정.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
    """
    account = _require_account(services, account_id)
    changes = _apply_updates(
        payload,
        {"name": "name", "monthly_budget_usd": "monthly_budget_usd"},
    )
    updated = account.model_copy(update=changes)
    services.registry.put_account(updated, overwrite=True)
    services.logger.info("계정을 수정했다", extra={"account_id": account_id})
    return _account_payload(updated)


@router.delete("/accounts/{account_id}", status_code=http.HTTPStatus.NO_CONTENT)
def delete_account(
    account_id: str, services: services_module.AdminDep
) -> fastapi.Response:
    """계정을 삭제한다.

    하위 팀·사용자·키가 남아 있으면 409 로 거부한다.

    Args:
        account_id: 계정 ID.
        services: 인증된 서비스 컨테이너.

    Returns:
        본문 없는 204 응답.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
        ResourceConflictError: 하위 리소스가 남아 있는 경우.
    """
    services.registry.delete_account(account_id)
    services.logger.info("계정을 삭제했다", extra={"account_id": account_id})
    return fastapi.Response(status_code=http.HTTPStatus.NO_CONTENT)


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


@router.get("/accounts/{account_id}/teams/{team_id}")
def get_team(
    account_id: str, team_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """팀 하나를 반환한다.

    Raises:
        ResourceNotFoundError: 계정이나 팀이 없는 경우.
    """
    _require_account(services, account_id)
    return _team_payload(_require_team(services, account_id, team_id))


@router.patch("/accounts/{account_id}/teams/{team_id}")
def update_team(
    account_id: str,
    team_id: str,
    payload: schemas.UpdateTeamRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """팀의 이름·예산을 수정한다.

    Raises:
        ResourceNotFoundError: 계정이나 팀이 없는 경우.
    """
    _require_account(services, account_id)
    team = _require_team(services, account_id, team_id)
    changes = _apply_updates(
        payload,
        {"name": "name", "monthly_budget_usd": "monthly_budget_usd"},
    )
    updated = team.model_copy(update=changes)
    services.registry.put_team(updated, overwrite=True)
    services.logger.info(
        "팀을 수정했다", extra={"account_id": account_id, "team_id": team_id}
    )
    return _team_payload(updated)


@router.post("/accounts/{account_id}/teams/{team_id}/status")
def update_team_status(
    account_id: str,
    team_id: str,
    payload: schemas.UpdateStatusRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """팀을 활성/비활성 전환한다.

    비활성 팀에 속한 키는 인증에서 거부된다. 반영은 메타데이터 캐시
    TTL 만큼 지연될 수 있다.

    Raises:
        ResourceNotFoundError: 계정이나 팀이 없는 경우.
    """
    _require_account(services, account_id)
    team = _require_team(services, account_id, team_id)
    updated = team.model_copy(
        update={"status": domain.EntityStatus(payload.status)}
    )
    services.registry.put_team(updated, overwrite=True)
    services.logger.info(
        "팀 상태를 변경했다",
        extra={
            "account_id": account_id,
            "team_id": team_id,
            "status": payload.status,
        },
    )
    return _team_payload(updated)


@router.delete(
    "/accounts/{account_id}/teams/{team_id}",
    status_code=http.HTTPStatus.NO_CONTENT,
)
def delete_team(
    account_id: str, team_id: str, services: services_module.AdminDep
) -> fastapi.Response:
    """팀을 삭제한다.

    소속 사용자·키가 남아 있으면 409 로 거부한다.

    Raises:
        ResourceNotFoundError: 계정이나 팀이 없는 경우.
        ResourceConflictError: 소속 사용자·키가 남아 있는 경우.
    """
    _require_account(services, account_id)
    services.registry.delete_team(account_id, team_id)
    services.logger.info(
        "팀을 삭제했다", extra={"account_id": account_id, "team_id": team_id}
    )
    return fastapi.Response(status_code=http.HTTPStatus.NO_CONTENT)


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
        rpm_limit=payload.rpm_limit,
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


@router.get("/accounts/{account_id}/users/{user_id}")
def get_user(
    account_id: str, user_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """사용자 하나를 반환한다.

    Raises:
        ResourceNotFoundError: 계정이나 사용자가 없는 경우.
    """
    _require_account(services, account_id)
    return _user_payload(_require_user(services, account_id, user_id))


@router.patch("/accounts/{account_id}/users/{user_id}")
def update_user(
    account_id: str,
    user_id: str,
    payload: schemas.UpdateUserRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """사용자의 이름·메일·팀·예산을 수정한다.

    팀을 옮길 때 그 팀이 존재하는지 확인한다. 다만 이 사용자에게 이미
    발급된 키의 `team_id` 는 여기서 함께 바꾸지 않는다. 키의 소속을 옮기려면
    키를 새로 발급해야 한다. 사용자 축과 키 축 집계가 어긋나는 것을 막기
    위해서다.

    Raises:
        ResourceNotFoundError: 계정·사용자·지정한 팀이 없는 경우.
    """
    _require_account(services, account_id)
    user = _require_user(services, account_id, user_id)
    if "team_id" in payload.model_fields_set and payload.team_id:
        _require_team(services, account_id, payload.team_id)
    changes = _apply_updates(
        payload,
        {
            "name": "name",
            "email": "email",
            "team_id": "team_id",
            "monthly_budget_usd": "monthly_budget_usd",
            "rpm_limit": "rpm_limit",
        },
    )
    updated = user.model_copy(update=changes)
    services.registry.put_user(updated, overwrite=True)
    services.logger.info(
        "사용자를 수정했다",
        extra={"account_id": account_id, "user_id": user_id},
    )
    return _user_payload(updated)


@router.post("/accounts/{account_id}/users/{user_id}/status")
def update_user_status(
    account_id: str,
    user_id: str,
    payload: schemas.UpdateStatusRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """사용자를 활성/비활성 전환한다.

    비활성 사용자의 키는 인증에서 거부된다. 반영은 메타데이터 캐시
    TTL 만큼 지연될 수 있다.

    Raises:
        ResourceNotFoundError: 계정이나 사용자가 없는 경우.
    """
    _require_account(services, account_id)
    user = _require_user(services, account_id, user_id)
    updated = user.model_copy(
        update={"status": domain.EntityStatus(payload.status)}
    )
    services.registry.put_user(updated, overwrite=True)
    services.logger.info(
        "사용자 상태를 변경했다",
        extra={
            "account_id": account_id,
            "user_id": user_id,
            "status": payload.status,
        },
    )
    return _user_payload(updated)


@router.delete(
    "/accounts/{account_id}/users/{user_id}",
    status_code=http.HTTPStatus.NO_CONTENT,
)
def delete_user(
    account_id: str, user_id: str, services: services_module.AdminDep
) -> fastapi.Response:
    """사용자를 삭제한다.

    소유 키가 남아 있으면 409 로 거부한다.

    Raises:
        ResourceNotFoundError: 계정이나 사용자가 없는 경우.
        ResourceConflictError: 소유 키가 남아 있는 경우.
    """
    _require_account(services, account_id)
    services.registry.delete_user(account_id, user_id)
    services.logger.info(
        "사용자를 삭제했다",
        extra={"account_id": account_id, "user_id": user_id},
    )
    return fastapi.Response(status_code=http.HTTPStatus.NO_CONTENT)


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
        rpm_limit=payload.rpm_limit,
        expires_at=payload.expires_at,
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


def _require_api_key(
    services: services_module.Services, account_id: str, key_id: str
) -> domain.ApiKey:
    """키를 조회하고 없으면 404 를 낸다."""
    api_key = services.registry.get_api_key(account_id, key_id)
    if api_key is None:
        raise errors.ResourceNotFoundError(f"API 키를 찾을 수 없다: {key_id}")
    return api_key


@router.get("/accounts/{account_id}/keys/{key_id}")
def get_api_key(
    account_id: str, key_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """API 키 하나의 메타데이터를 반환한다. 평문 키는 포함되지 않는다.

    Raises:
        ResourceNotFoundError: 계정이나 키가 없는 경우.
    """
    _require_account(services, account_id)
    return _key_payload(_require_api_key(services, account_id, key_id))


@router.patch("/accounts/{account_id}/keys/{key_id}")
def update_api_key(
    account_id: str,
    key_id: str,
    payload: schemas.UpdateApiKeyRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """API 키의 이름·허용 모델·예산을 수정한다.

    소속(계정·팀·사용자)과 해시는 바꾸지 않는다.

    Raises:
        ResourceNotFoundError: 계정이나 키가 없는 경우.
    """
    _require_account(services, account_id)
    api_key = _require_api_key(services, account_id, key_id)
    changes = _apply_updates(
        payload,
        {
            "name": "name",
            "monthly_budget_usd": "monthly_budget_usd",
            "rpm_limit": "rpm_limit",
            "expires_at": "expires_at",
        },
    )
    if "allowed_models" in payload.model_fields_set:
        changes["allowed_models"] = tuple(payload.allowed_models or [])
    updated = api_key.model_copy(update=changes)
    services.registry.update_api_key(updated)
    services.logger.info(
        "API 키를 수정했다",
        extra={"account_id": account_id, "key_id": key_id},
    )
    return _key_payload(updated)


@router.post("/accounts/{account_id}/keys/{key_id}/status")
def update_api_key_status(
    account_id: str,
    key_id: str,
    payload: schemas.UpdateStatusRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """API 키를 활성/비활성 전환한다.

    비활성 키는 인증에서 즉시 거부된다. 삭제와 달리 되살릴 수 있어, 잠시
    막고 싶을 때 쓴다.

    Raises:
        ResourceNotFoundError: 계정이나 키가 없는 경우.
    """
    _require_account(services, account_id)
    api_key = _require_api_key(services, account_id, key_id)
    updated = api_key.model_copy(
        update={"status": domain.EntityStatus(payload.status)}
    )
    services.registry.update_api_key(updated)
    services.logger.info(
        "API 키 상태를 변경했다",
        extra={
            "account_id": account_id,
            "key_id": key_id,
            "status": payload.status,
        },
    )
    return _key_payload(updated)


@router.post("/accounts/{account_id}/keys/{key_id}/rotate")
def rotate_api_key(
    account_id: str, key_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """API 키를 재발급한다.

    같은 `key_id` 를 유지한 채 평문 키(그리고 해시·접두어)만 새로 만든다.
    예산·허용 모델·소속·이름은 그대로 이어받는다. 옛 평문 키는 즉시
    무효가 된다. 새 해시 생성과 옛 해시 삭제는 하나의 트랜잭션으로 처리해,
    중간 실패로 두 키가 동시에 유효해지는 상황을 막는다.

    이 엔드포인트는 HTTP 수준에서 멱등하지 않다. 응답을 못 받아 다시 호출하면
    또 다른 새 평문 키가 발급된다. 저장소가 호출마다 만드는 트랜잭션 토큰은
    boto3 의 한 SDK 호출 안에서 일어나는 내부 재시도만 보호한다.

    평문 키는 이 응답에서만 볼 수 있다.

    Raises:
        ResourceNotFoundError: 계정이나 키가 없는 경우.
    """
    _require_account(services, account_id)
    existing = _require_api_key(services, account_id, key_id)

    generated = apikey.generate_api_key(services.settings.env)
    rotated = existing.model_copy(
        update={
            "key_hash": generated.key_hash,
            "key_prefix": generated.key_prefix,
            "last_used_at": "",
        }
    )
    # 새 해시 생성과 옛 해시 삭제를 하나의 트랜잭션으로 묶는다. 별도 호출로
    # 나누면 두 번째가 실패했을 때 두 키가 모두 유효하고 같은 key_id 가 GSI
    # 에 중복으로 남는다. 저장소의 작업별 토큰은 SDK 내부 재시도를 보호할
    # 뿐이며, HTTP 재호출까지 멱등하게 만들지는 않는다.
    services.registry.rotate_api_key(existing.key_hash, rotated)
    services.logger.info(
        "API 키를 재발급했다",
        extra={"account_id": account_id, "key_id": key_id},
    )
    payload_out = _key_payload(rotated)
    payload_out["api_key"] = generated.plaintext
    payload_out["warning"] = (
        "평문 키는 이 응답에서만 확인할 수 있다. 옛 키는 즉시 무효다."
    )
    return payload_out


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


# ---------------------------------------------------------------------------
# 계정별 외부 인증(OIDC) 설정
# ---------------------------------------------------------------------------


def _auth_config_view(config: domain.AccountAuthConfig) -> _JsonDict:
    """인증 설정을 응답 형태로 바꾼다.

    비밀값은 없다. 발급자·청중·클레임 이름은 모두 공개 설정이고, 서명 키는
    IdP 가 공개하는 JWKS 에서 가져온다.

    Args:
        config: 인증 설정.

    Returns:
        JSON 직렬화 가능한 딕셔너리.
    """
    return {
        "account_id": config.account_id,
        "issuer": config.issuer,
        "jwks_url": config.jwks_url,
        "effective_jwks_url": config.effective_jwks_url,
        "audience": config.audience,
        "user_claim": config.user_claim,
        "team_claim": config.team_claim,
        "groups_claim": config.groups_claim,
        "admin_groups": config.admin_groups,
        "auto_provision": config.auto_provision,
        "provision_allowed_models": config.provision_allowed_models,
        "provision_budget_usd": (
            float(config.provision_budget_usd)
            if config.provision_budget_usd is not None
            else None
        ),
        "status": config.status.value,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


def _require_platform_admin(request: fastapi.Request) -> domain.AdminPrincipal:
    """플랫폼 관리자만 통과시킨다.

    가드레일 면제는 안전 통제를 제거하는 작업이다. 계정 관리자가 자기 계정의
    통제를 스스로 해제할 수 있으면 통제라고 부를 수 없다. 그래서 계정 범위
    관리자에게는 허용하지 않는다.

    Args:
        request: 현재 요청.

    Returns:
        플랫폼 범위 관리 주체.

    Raises:
        PermissionDeniedError: 계정 범위 관리자인 경우.
    """
    principal = services_module.get_admin_principal(request)
    if principal.scope is not domain.AdminScope.PLATFORM:
        raise errors.PermissionDeniedError(
            "가드레일 면제는 플랫폼 관리자만 바꿀 수 있다. 계정 관리자가"
            " 자기 계정의 안전 통제를 해제할 수 있으면 통제가 아니다."
        )
    return principal


def _guardrail_config_view(
    config: domain.AccountGuardrailConfig,
) -> _JsonDict:
    """가드레일 설정을 응답 형태로 만든다."""
    return {
        "account_id": config.account_id,
        "guardrail_id": config.guardrail_id,
        "guardrail_version": config.guardrail_version,
        "enabled": config.enabled,
        "updated_at": config.updated_at,
    }


@router.get("/accounts/{account_id}/guardrail")
def get_guardrail_config(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정의 가드레일 기준선을 조회한다.

    Args:
        account_id: 계정 ID.
        services: 서비스 컨테이너.

    Returns:
        설정. 없으면 `configured: false` 만 담아 반환한다.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
    """
    _require_account(services, account_id)
    config = services.registry.get_guardrail_config(account_id)
    if config is None:
        return {"account_id": account_id, "configured": False}
    view = _guardrail_config_view(config)
    view["configured"] = True
    return view


@router.put("/accounts/{account_id}/guardrail")
def put_guardrail_config(
    account_id: str,
    payload: schemas.PutGuardrailConfigRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정의 가드레일 기준선을 만들거나 갱신한다.

    저장 전에 AWS 에 그 가드레일이 실제로 있고 사용 가능한 상태인지 확인한다.
    없는 가드레일을 저장하면 이후 모든 요청이 `ValidationException` 으로
    실패한다. 그것을 배포 후에 알게 되는 것보다 여기서 막는 편이 낫다.

    Args:
        account_id: 계정 ID.
        payload: 설정 값.
        services: 서비스 컨테이너.

    Returns:
        저장된 설정.

    Raises:
        ResourceNotFoundError: 계정이 없거나 가드레일을 찾을 수 없는 경우.
    """
    _require_account(services, account_id)
    services.bedrock.verify_guardrail(
        payload.guardrail_id, payload.guardrail_version
    )
    config = domain.AccountGuardrailConfig(
        account_id=account_id,
        guardrail_id=payload.guardrail_id,
        guardrail_version=payload.guardrail_version,
        enabled=payload.enabled,
        updated_at=services.clock.now().isoformat(),
    )
    services.registry.put_guardrail_config(config)
    services.guardrails.invalidate(account_id)
    services.logger.info(
        "가드레일 기준선을 저장했다",
        extra={
            "account_id": account_id,
            "guardrail_id": payload.guardrail_id,
            "guardrail_version": payload.guardrail_version,
            "enabled": payload.enabled,
        },
    )
    view = _guardrail_config_view(config)
    view["configured"] = True
    return view


@router.delete(
    "/accounts/{account_id}/guardrail",
    status_code=http.HTTPStatus.NO_CONTENT,
)
def delete_guardrail_config(
    account_id: str,
    services: services_module.AdminDep,
    request: fastapi.Request,
) -> None:
    """계정의 가드레일 기준선을 삭제한다.

    삭제하면 그 계정 전체가 가드레일 없이 동작한다. 면제와 같은 효과를 계정
    단위로 내는 작업이므로 플랫폼 관리자만 할 수 있다.

    Args:
        account_id: 계정 ID.
        services: 서비스 컨테이너.
        request: 현재 요청. 권한 확인에 쓴다.
    """
    _require_platform_admin(request)
    _require_account(services, account_id)
    services.registry.delete_guardrail_config(account_id)
    services.guardrails.invalidate(account_id)
    services.logger.warning(
        "가드레일 기준선을 삭제했다. 이 계정은 가드레일 없이 동작한다",
        extra={"account_id": account_id},
    )


@router.put("/accounts/{account_id}/users/{user_id}/guardrail-exemption")
def put_user_guardrail_exemption(
    account_id: str,
    user_id: str,
    payload: schemas.PutGuardrailExemptionRequest,
    services: services_module.AdminDep,
    request: fastapi.Request,
) -> _JsonDict:
    """사용자의 가드레일 면제를 설정한다.

    플랫폼 관리자만 바꿀 수 있고 면제할 때는 사유가 필수다.

    Args:
        account_id: 계정 ID.
        user_id: 사용자 ID.
        payload: 면제 여부와 사유.
        services: 서비스 컨테이너.
        request: 현재 요청. 권한 확인에 쓴다.

    Returns:
        갱신된 면제 상태.

    Raises:
        ResourceNotFoundError: 사용자가 없는 경우.
    """
    admin = _require_platform_admin(request)
    user = services.registry.get_user(account_id, user_id)
    if user is None:
        raise errors.ResourceNotFoundError(f"사용자를 찾을 수 없다: {user_id}")
    services.registry.put_user(
        user.model_copy(
            update={
                "guardrail_exempt": payload.exempt,
                "guardrail_exempt_reason": (
                    payload.reason.strip() if payload.exempt else ""
                ),
            }
        ),
        overwrite=True,
    )
    services.logger.warning(
        "사용자 가드레일 면제를 변경했다",
        extra={
            "account_id": account_id,
            "user_id": user_id,
            "exempt": payload.exempt,
            "reason": payload.reason.strip(),
            "actor": admin.subject,
        },
    )
    return {
        "account_id": account_id,
        "user_id": user_id,
        "guardrail_exempt": payload.exempt,
        "guardrail_exempt_reason": (
            payload.reason.strip() if payload.exempt else ""
        ),
    }


@router.put("/accounts/{account_id}/teams/{team_id}/guardrail-exemption")
def put_team_guardrail_exemption(
    account_id: str,
    team_id: str,
    payload: schemas.PutGuardrailExemptionRequest,
    services: services_module.AdminDep,
    request: fastapi.Request,
) -> _JsonDict:
    """팀의 가드레일 면제를 설정한다.

    Args:
        account_id: 계정 ID.
        team_id: 팀 ID.
        payload: 면제 여부와 사유.
        services: 서비스 컨테이너.
        request: 현재 요청. 권한 확인에 쓴다.

    Returns:
        갱신된 면제 상태.

    Raises:
        ResourceNotFoundError: 팀이 없는 경우.
    """
    admin = _require_platform_admin(request)
    team = services.registry.get_team(account_id, team_id)
    if team is None:
        raise errors.ResourceNotFoundError(f"팀을 찾을 수 없다: {team_id}")
    services.registry.put_team(
        team.model_copy(
            update={
                "guardrail_exempt": payload.exempt,
                "guardrail_exempt_reason": (
                    payload.reason.strip() if payload.exempt else ""
                ),
            }
        ),
        overwrite=True,
    )
    services.logger.warning(
        "팀 가드레일 면제를 변경했다",
        extra={
            "account_id": account_id,
            "team_id": team_id,
            "exempt": payload.exempt,
            "reason": payload.reason.strip(),
            "actor": admin.subject,
        },
    )
    return {
        "account_id": account_id,
        "team_id": team_id,
        "guardrail_exempt": payload.exempt,
        "guardrail_exempt_reason": (
            payload.reason.strip() if payload.exempt else ""
        ),
    }


@router.get("/accounts/{account_id}/auth")
def get_auth_config(
    account_id: str, services: services_module.AdminDep
) -> _JsonDict:
    """계정의 외부 인증 설정을 조회한다.

    Args:
        account_id: 계정 ID.
        services: 서비스 컨테이너.

    Returns:
        인증 설정. 설정이 없으면 `configured: false` 만 담아 반환한다.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
    """
    _require_account(services, account_id)
    config = services.registry.get_auth_config(account_id)
    if config is None:
        return {"account_id": account_id, "configured": False}
    view = _auth_config_view(config)
    view["configured"] = True
    return view


@router.put("/accounts/{account_id}/auth")
def put_auth_config(
    account_id: str,
    payload: schemas.PutAuthConfigRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정의 외부 인증 설정을 만들거나 갱신한다.

    발급자는 계정 간에 겹칠 수 없다. 다른 계정이 쓰는 발급자를 등록하려 하면
    409 로 거부된다. 가로챌 수 있으면 그 계정 사용자로 위장할 수 있다.

    Args:
        account_id: 계정 ID.
        payload: 설정 값.
        services: 서비스 컨테이너.

    Returns:
        저장된 설정.

    Raises:
        ResourceNotFoundError: 계정이 없는 경우.
        ResourceConflictError: 발급자가 다른 계정에 이미 등록된 경우.
        InvalidRequestError: JWKS URL 이 https 가 아니거나 내부 주소인 경우.
    """
    _require_account(services, account_id)
    existing = services.registry.get_auth_config(account_id)
    now = clock.to_iso(services.clock.now())
    config = domain.AccountAuthConfig(
        account_id=account_id,
        issuer=payload.issuer,
        jwks_url=payload.jwks_url,
        audience=payload.audience,
        user_claim=payload.user_claim,
        team_claim=payload.team_claim,
        groups_claim=payload.groups_claim,
        admin_groups=payload.admin_groups,
        auto_provision=payload.auto_provision,
        provision_allowed_models=payload.provision_allowed_models,
        provision_budget_usd=payload.provision_budget_usd,
        status=existing.status if existing else domain.EntityStatus.ACTIVE,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    # 저장 전에 JWKS URL 을 검증한다. 잘못된 값을 저장해 두면 그 계정의 모든
    # 토큰 검증이 실패하고, 원인이 설정에 있다는 것을 알기 어렵다.
    oidc.validate_jwks_url(config.effective_jwks_url)
    services.registry.put_auth_config(config, overwrite=existing is not None)
    services.logger.info(
        "계정 외부 인증 설정을 저장했다",
        extra={"account_id": account_id, "issuer": config.issuer},
    )
    return _auth_config_view(config)


@router.post("/accounts/{account_id}/auth/status")
def set_auth_config_status(
    account_id: str,
    payload: schemas.UpdateStatusRequest,
    services: services_module.AdminDep,
) -> _JsonDict:
    """계정의 외부 인증을 즉시 켜거나 끈다.

    설정을 지우지 않고 차단할 수 있어야 한다. IdP 쪽 문제로 급히 막아야 할 때
    설정을 다시 입력하지 않고 되돌릴 수 있다.

    Args:
        account_id: 계정 ID.
        payload: 상태 값.
        services: 서비스 컨테이너.

    Returns:
        갱신된 설정.

    Raises:
        ResourceNotFoundError: 계정 또는 설정이 없는 경우.
    """
    _require_account(services, account_id)
    existing = services.registry.get_auth_config(account_id)
    if existing is None:
        raise errors.ResourceNotFoundError("인증 설정이 없다.")
    updated = existing.model_copy(
        update={
            "status": domain.EntityStatus(payload.status),
            "updated_at": clock.to_iso(services.clock.now()),
        }
    )
    services.registry.put_auth_config(updated, overwrite=True)
    services.logger.info(
        "계정 외부 인증 상태를 변경했다",
        extra={"account_id": account_id, "status": payload.status},
    )
    return _auth_config_view(updated)


@router.delete(
    "/accounts/{account_id}/auth", status_code=http.HTTPStatus.NO_CONTENT
)
def delete_auth_config(
    account_id: str, services: services_module.AdminDep
) -> None:
    """계정의 외부 인증 설정을 삭제한다.

    설정과 발급자 역인덱스가 함께 사라진다. 역인덱스만 남으면 다른 계정이 그
    발급자를 영구히 쓸 수 없게 된다.

    Args:
        account_id: 계정 ID.
        services: 서비스 컨테이너.

    Raises:
        ResourceNotFoundError: 계정 또는 설정이 없는 경우.
    """
    _require_account(services, account_id)
    services.registry.delete_auth_config(account_id)
    services.logger.info(
        "계정 외부 인증 설정을 삭제했다", extra={"account_id": account_id}
    )
