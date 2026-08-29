"""셀프서비스 라우터.

개발자가 관리자에게 요청하지 않고 스스로 API 키를 받게 한다. 인증은 고객
IdP 가 발급한 액세스 토큰으로 하고, 계정·팀·사용자는 **토큰에서만** 결정한다.
본문으로 받으면 다른 사용자에게 키를 발급하는 경로가 열린다.

권한 상승이 불가능해야 한다는 점이 이 라우터의 핵심 제약이다.

- 키는 항상 토큰 주체 자신에게 귀속된다.
- 허용 모델은 계정이 정한 범위를 넘길 수 없다. 요청이 더 넓은 목록을 보내면
  교집합만 남는다.
- 예산은 요청으로 지정할 수 없다. 계정 설정값을 그대로 쓴다.
"""

from __future__ import annotations

import http
import typing

import fastapi

from llmgw import apikey
from llmgw import clock
from llmgw import domain
from llmgw import errors
from llmgw import pricing as pricing_module
from llmgw import schemas
from llmgw import services as services_module

router = fastapi.APIRouter(prefix="/auth", tags=["self-service"])

_JsonDict = dict[str, typing.Any]
_BEARER_PREFIX = "bearer "


def _identity(
    services: services_module.Services, authorization: str | None
) -> typing.Any:
    """`Authorization` 헤더의 OIDC 토큰을 검증해 주체를 만든다.

    Args:
        services: 서비스 컨테이너.
        authorization: `Bearer <jwt>` 헤더 값.

    Returns:
        검증된 주체.

    Raises:
        AuthenticationError: 헤더가 없거나 토큰이 유효하지 않은 경우.
    """
    if not authorization or not authorization.lower().startswith(
        _BEARER_PREFIX
    ):
        raise errors.AuthenticationError(
            "Authorization 헤더가 없다. 인증 서버에서 받은 액세스 토큰을"
            " 'Bearer <token>' 형식으로 보낸다."
        )
    token = authorization[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise errors.AuthenticationError("액세스 토큰이 비어 있다.")
    return services.oidc.verify(token)


@router.get("/me")
def whoami(
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> _JsonDict:
    """토큰이 어떤 계정·팀·사용자로 해석되는지 보여준다.

    클레임 매핑이 의도대로 됐는지 확인하는 용도다. 매핑이 틀리면 사용량이
    엉뚱한 축에 집계되므로, 키를 발급하기 전에 확인할 수 있어야 한다.

    Args:
        services: 서비스 컨테이너.
        authorization: `Bearer <jwt>` 헤더.

    Returns:
        해석된 주체 정보. 토큰 원문이나 서명은 포함하지 않는다.
    """
    identity = _identity(services, authorization)
    user = services.registry.get_user(identity.account_id, identity.user_id)
    return {
        "account_id": identity.account_id,
        "team_id": identity.team_id,
        "user_id": identity.user_id,
        "email": identity.email,
        "display_name": identity.display_name,
        "groups": list(identity.groups),
        "is_platform_admin": identity.is_platform_admin,
        "is_account_admin": identity.is_account_admin,
        "registered": user is not None,
        "auto_provision": identity.config.auto_provision,
        "allowed_models": list(identity.config.provision_model_list),
    }


@router.post("/keys", status_code=http.HTTPStatus.CREATED)
def issue_own_key(
    payload: schemas.SelfIssueKeyRequest,
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> _JsonDict:
    """토큰 주체 자신에게 API 키를 발급한다.

    평문 키는 이 응답에서만 볼 수 있다. 저장소에는 SHA-256 해시만 남는다.

    Args:
        payload: 키 이름과 원하는 허용 모델.
        services: 서비스 컨테이너.
        authorization: `Bearer <jwt>` 헤더.

    Returns:
        발급된 키 정보와 평문 키.

    Raises:
        AuthenticationError: 토큰이 유효하지 않은 경우.
        PermissionDeniedError: 사용자가 등록되지 않았고 자동 생성도 꺼져
            있는 경우.
    """
    identity = _identity(services, authorization)
    user = services.registry.get_user(identity.account_id, identity.user_id)
    if user is None:
        # 등록되지 않은 사람이 키를 만들 수 있으면 IdP 계정만으로 게이트웨이를
        # 쓰게 된다. 자동 생성 정책을 따르도록 인증기와 같은 규칙을 적용한다.
        raise errors.PermissionDeniedError(
            "사용자가 등록되지 않았다. 관리자가 사용자를 만들거나 계정의"
            " 자동 생성을 켠 뒤 다시 시도한다."
        )
    if user.status is not domain.EntityStatus.ACTIVE:
        raise errors.AuthenticationError("사용자가 비활성 상태다.")

    allowed = _narrow_models(
        requested=tuple(payload.allowed_models),
        permitted=identity.config.provision_model_list,
    )
    generated = apikey.generate_api_key(services.settings.env)
    api_key = domain.ApiKey(
        account_id=identity.account_id,
        key_id=services.id_factory.new_id(),
        key_hash=generated.key_hash,
        key_prefix=generated.key_prefix,
        user_id=identity.user_id,
        team_id=user.team_id or identity.team_id,
        name=payload.name,
        allowed_models=allowed,
        # 예산은 요청으로 지정할 수 없다. 스스로 한도를 올릴 수 있으면
        # 예산 통제가 의미를 잃는다.
        monthly_budget_usd=identity.config.provision_budget_usd,
        created_at=clock.to_iso(services.clock.now()),
    )
    services.registry.put_api_key(api_key)
    services.logger.info(
        "셀프서비스로 API 키를 발급했다",
        extra={
            "account_id": identity.account_id,
            "user_id": identity.user_id,
            "key_id": api_key.key_id,
        },
    )
    return {
        "key_id": api_key.key_id,
        "account_id": api_key.account_id,
        "user_id": api_key.user_id,
        "team_id": api_key.team_id,
        "name": api_key.name,
        "allowed_models": list(api_key.allowed_models),
        "monthly_budget_usd": (
            float(api_key.monthly_budget_usd)
            if api_key.monthly_budget_usd is not None
            else None
        ),
        "created_at": api_key.created_at,
        # 이 응답에서만 볼 수 있다.
        "api_key": generated.plaintext,
    }


@router.get("/keys")
def list_own_keys(
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> _JsonDict:
    """자기 키 목록을 반환한다. 평문은 포함하지 않는다.

    Args:
        services: 서비스 컨테이너.
        authorization: `Bearer <jwt>` 헤더.

    Returns:
        본인에게 귀속된 키 목록.
    """
    identity = _identity(services, authorization)
    keys = [
        key
        for key in services.registry.list_api_keys(identity.account_id)
        if key.user_id == identity.user_id
    ]
    return {
        "data": [
            {
                "key_id": key.key_id,
                "name": key.name,
                "key_prefix": key.key_prefix,
                "allowed_models": list(key.allowed_models),
                "status": key.status.value,
                "created_at": key.created_at,
                "last_used_at": key.last_used_at,
            }
            for key in keys
        ]
    }


@router.delete("/keys/{key_id}", status_code=http.HTTPStatus.NO_CONTENT)
def revoke_own_key(
    key_id: str,
    services: services_module.ServicesDep,
    authorization: typing.Annotated[
        str | None, fastapi.Header(alias="Authorization")
    ] = None,
) -> None:
    """자기 키를 폐기한다.

    남의 키를 지우지 못하게 소유자를 확인한다. 확인 없이 지우면 키 ID 만
    알아도 다른 사용자의 키를 무효화할 수 있다.

    Args:
        key_id: 폐기할 키 ID.
        services: 서비스 컨테이너.
        authorization: `Bearer <jwt>` 헤더.

    Raises:
        ResourceNotFoundError: 키가 없거나 본인 것이 아닌 경우.
    """
    identity = _identity(services, authorization)
    target = next(
        (
            key
            for key in services.registry.list_api_keys(identity.account_id)
            if key.key_id == key_id and key.user_id == identity.user_id
        ),
        None,
    )
    if target is None:
        # 남의 키가 존재한다는 사실도 알려주지 않는다.
        raise errors.ResourceNotFoundError(f"키를 찾을 수 없다: {key_id}")
    services.registry.delete_api_key(identity.account_id, key_id)
    services.logger.info(
        "셀프서비스로 API 키를 폐기했다",
        extra={
            "account_id": identity.account_id,
            "user_id": identity.user_id,
            "key_id": key_id,
        },
    )


def _narrow_models(
    *, requested: tuple[str, ...], permitted: tuple[str, ...]
) -> tuple[str, ...]:
    """요청 모델을 계정이 허용한 범위로 좁힌다.

    계정이 범위를 정하지 않았으면 요청을 그대로 쓴다. 정했다면 교집합만
    남긴다. 요청이 비어 있으면 계정 범위를 그대로 물려받는다.

    Args:
        requested: 요청한 모델 목록.
        permitted: 계정이 허용한 모델 목록.

    Returns:
        적용할 허용 모델 목록.
    """
    if not permitted:
        return requested
    if not requested:
        return permitted
    allowed_normalized = {
        pricing_module.normalize_model_id(item): item for item in permitted
    }
    narrowed = [
        allowed_normalized[pricing_module.normalize_model_id(item)]
        for item in requested
        if pricing_module.normalize_model_id(item) in allowed_normalized
    ]
    # 요청이 전부 범위 밖이면 계정 범위를 적용한다. 빈 목록은 "제한 없음" 을
    # 뜻하므로 그대로 두면 오히려 권한이 넓어진다.
    return tuple(narrowed) or permitted
