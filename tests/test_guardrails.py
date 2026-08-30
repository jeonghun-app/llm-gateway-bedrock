"""가드레일 정책 테스트.

이 기능은 안전 통제를 다루므로 "동작한다" 만으로는 부족하다. 통제가 **꺼지는
경로**가 의도한 것뿐인지를 고정하는 것이 이 테스트의 목적이다.

실측(sandbox 가드레일)으로 확인한 사실을 코드가 지키는지도 함께 본다.

- 스트리밍은 `sync` 로 강제한다. `async` 는 차단 대상을 먼저 보낸다.
- `trace` 는 끈다. 켜면 응답에 차단하려던 원문이 들어온다.
- 버전은 숫자만 받는다. `DRAFT` 는 런타임에서 동작하므로 막지 않으면 조용히
  바뀌는 정책을 강제한다.
"""

from __future__ import annotations

import typing

from fastapi import testclient
import pydantic
import pytest

from llmgw import domain
from llmgw import guardrails
from llmgw import repository
from llmgw import services

_GUARDRAIL_ID = "gr-abc123"
_VERSION = "2"


def _put_config(
    client: testclient.TestClient,
    headers: dict[str, str],
    *,
    enabled: bool = True,
    account: str = "acme",
) -> typing.Any:
    """계정 가드레일 기준선을 설정한다."""
    return client.put(
        f"/admin/accounts/{account}/guardrail",
        headers=headers,
        json={
            "guardrail_id": _GUARDRAIL_ID,
            "guardrail_version": _VERSION,
            "enabled": enabled,
        },
    )


def _records(app_services: services.Services) -> list[dict[str, typing.Any]]:
    """고정 시계가 가리키는 날짜의 사용량 레코드를 시간 역순으로 읽는다."""
    day = app_services.clock.now().date().isoformat()
    return app_services.usage_store.list_records("acme", day)


def _chat(
    client: testclient.TestClient, api_key: str, *, stream: bool = False
) -> typing.Any:
    """채팅 완성을 호출한다."""
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "amazon.nova-lite-v1:0",
            "messages": [{"role": "user", "content": "안녕"}],
            "max_tokens": 16,
            "stream": stream,
        },
    )


# ---------------------------------------------------------------------------
# 정책 해석
# ---------------------------------------------------------------------------


def _principal(
    *, account: str = "acme", team: str = "platform", user: str = "alice"
) -> domain.Principal:
    """테스트용 요청 주체."""
    return domain.Principal(
        account_id=account,
        team_id=team,
        user_id=user,
        key_id="k1",
        key_hash="h1",
        allowed_models=(),
    )


def test_기준선이없으면가드레일을붙이지않는다(
    guardrail_resolver: guardrails.GuardrailResolver,
) -> None:
    decision = guardrail_resolver.resolve(_principal())
    assert decision.applied is False
    assert decision.exempt_scope == ""


def test_기준선이있으면가드레일을붙인다(
    guardrail_resolver: guardrails.GuardrailResolver,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    registry.put_guardrail_config(
        domain.AccountGuardrailConfig(
            account_id="acme",
            guardrail_id=_GUARDRAIL_ID,
            guardrail_version=_VERSION,
        )
    )
    decision = guardrail_resolver.resolve(_principal())
    assert decision.applied is True
    assert decision.guardrail_id == _GUARDRAIL_ID
    assert decision.guardrail_version == _VERSION


def test_기준선을끄면붙이지않는다(
    guardrail_resolver: guardrails.GuardrailResolver,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    registry.put_guardrail_config(
        domain.AccountGuardrailConfig(
            account_id="acme",
            guardrail_id=_GUARDRAIL_ID,
            guardrail_version=_VERSION,
            enabled=False,
        )
    )
    assert guardrail_resolver.resolve(_principal()).applied is False


def test_사용자면제가팀면제보다먼저적용된다(
    guardrail_resolver: guardrails.GuardrailResolver,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    registry.put_guardrail_config(
        domain.AccountGuardrailConfig(
            account_id="acme",
            guardrail_id=_GUARDRAIL_ID,
            guardrail_version=_VERSION,
        )
    )
    user = registry.get_user("acme", "alice")
    assert user is not None
    registry.put_user(
        user.model_copy(
            update={
                "guardrail_exempt": True,
                "guardrail_exempt_reason": "레드팀 평가",
            }
        ),
        overwrite=True,
    )
    decision = guardrail_resolver.resolve(_principal())
    assert decision.applied is False
    assert decision.exempt_scope == "user"


def test_팀면제는소속사용자에게적용된다(
    guardrail_resolver: guardrails.GuardrailResolver,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    registry.put_guardrail_config(
        domain.AccountGuardrailConfig(
            account_id="acme",
            guardrail_id=_GUARDRAIL_ID,
            guardrail_version=_VERSION,
        )
    )
    team = registry.get_team("acme", "platform")
    assert team is not None
    registry.put_team(
        team.model_copy(
            update={
                "guardrail_exempt": True,
                "guardrail_exempt_reason": "내부 평가 전용 팀",
            }
        ),
        overwrite=True,
    )
    decision = guardrail_resolver.resolve(_principal())
    assert decision.applied is False
    assert decision.exempt_scope == "team"


# ---------------------------------------------------------------------------
# Bedrock 파라미터 — 실측 결과를 코드가 지키는가
# ---------------------------------------------------------------------------


def test_스트리밍은sync를강제한다() -> None:
    from llmgw import bedrock

    decision = domain.GuardrailDecision(
        guardrail_id=_GUARDRAIL_ID, guardrail_version=_VERSION
    )
    params = bedrock.BedrockGateway._build_params(
        model_id="m",
        messages=[],
        system=[],
        inference_config={},
        guardrail=decision,
        streaming=True,
    )
    config = params["guardrailConfig"]
    # async 는 차단 대상 텍스트를 클라이언트에 먼저 보낸다. 실측에서 확인했다.
    assert config["streamProcessingMode"] == "sync"


def test_비스트리밍에는스트림모드를넣지않는다() -> None:
    from llmgw import bedrock

    params = bedrock.BedrockGateway._build_params(
        model_id="m",
        messages=[],
        system=[],
        inference_config={},
        guardrail=domain.GuardrailDecision(
            guardrail_id=_GUARDRAIL_ID, guardrail_version=_VERSION
        ),
        streaming=False,
    )
    assert "streamProcessingMode" not in params["guardrailConfig"]


def test_trace는항상꺼진다() -> None:
    from llmgw import bedrock

    for streaming in (False, True):
        params = bedrock.BedrockGateway._build_params(
            model_id="m",
            messages=[],
            system=[],
            inference_config={},
            guardrail=domain.GuardrailDecision(
                guardrail_id=_GUARDRAIL_ID, guardrail_version=_VERSION
            ),
            streaming=streaming,
        )
        # trace 를 켜면 응답에 modelOutput(차단하려던 원문)이 들어온다.
        assert params["guardrailConfig"]["trace"] == "disabled"


def test_미적용판정이면guardrailConfig가없다() -> None:
    from llmgw import bedrock

    for decision in (None, domain.GuardrailDecision()):
        params = bedrock.BedrockGateway._build_params(
            model_id="m",
            messages=[],
            system=[],
            inference_config={},
            guardrail=decision,
            streaming=False,
        )
        assert "guardrailConfig" not in params


def test_draft버전은도메인에서거부된다() -> None:
    # 광범위한 Exception 을 잡으면 다른 이유로 실패해도 통과한다.
    with pytest.raises(pydantic.ValidationError):
        domain.AccountGuardrailConfig(
            account_id="acme",
            guardrail_id=_GUARDRAIL_ID,
            guardrail_version="DRAFT",
        )


# ---------------------------------------------------------------------------
# 요청 경로 통합
# ---------------------------------------------------------------------------


def test_기준선설정후요청에가드레일이붙는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    assert _put_config(client, admin_headers).status_code == 200
    assert _chat(client, api_key).status_code == 200
    assert fake_bedrock.last_call is not None
    config = fake_bedrock.last_call.get("guardrail")
    assert config is not None and config.applied is True
    assert config.guardrail_id == _GUARDRAIL_ID


def test_기준선이없으면요청에가드레일이없다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    assert _chat(client, api_key).status_code == 200
    assert fake_bedrock.last_call is not None
    config = fake_bedrock.last_call.get("guardrail")
    assert config is None or config.applied is False


# ---------------------------------------------------------------------------
# 관리 API 권한 — 통제가 꺼지는 경로를 제한하는가
# ---------------------------------------------------------------------------


def test_없는가드레일은저장되지않는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    fake_bedrock: typing.Any,
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    fake_bedrock.unknown_guardrails.add(_GUARDRAIL_ID)
    response = _put_config(client, admin_headers)
    assert response.status_code == 404
    # 저장되지 않아야 한다. 저장되면 이후 모든 요청이 실패한다.
    assert registry.get_guardrail_config("acme") is None


def test_저장전에가드레일존재를확인한다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    fake_bedrock: typing.Any,
    api_key: str,
) -> None:
    _put_config(client, admin_headers)
    assert (_GUARDRAIL_ID, _VERSION) in fake_bedrock.verified_guardrails


def test_draft버전은api에서거부된다(
    client: testclient.TestClient, admin_headers: dict[str, str], api_key: str
) -> None:
    response = client.put(
        "/admin/accounts/acme/guardrail",
        headers=admin_headers,
        json={
            "guardrail_id": _GUARDRAIL_ID,
            "guardrail_version": "DRAFT",
            "enabled": True,
        },
    )
    # 앱의 예외 핸들러가 검증 오류를 400 invalid_request 로 바꾼다.
    assert response.status_code == 400


def test_면제에는사유가필요하다(
    client: testclient.TestClient, admin_headers: dict[str, str], api_key: str
) -> None:
    response = client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": True},
    )
    assert response.status_code == 400


def test_면제해제에는사유가필요없다(
    client: testclient.TestClient, admin_headers: dict[str, str], api_key: str
) -> None:
    response = client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": False},
    )
    assert response.status_code == 200
    assert response.json()["guardrail_exempt"] is False


def test_면제하면사유가저장된다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    response = client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": True, "reason": "레드팀 평가 2026-Q3"},
    )
    assert response.status_code == 200
    user = registry.get_user("acme", "alice")
    assert user is not None
    assert user.guardrail_exempt is True
    assert user.guardrail_exempt_reason == "레드팀 평가 2026-Q3"


def test_면제를해제하면사유도지워진다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    registry: repository.RegistryRepository,
    api_key: str,
) -> None:
    client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": True, "reason": "임시"},
    )
    client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": False},
    )
    user = registry.get_user("acme", "alice")
    assert user is not None
    assert user.guardrail_exempt is False
    assert user.guardrail_exempt_reason == ""


def test_없는사용자면제는404다(
    client: testclient.TestClient, admin_headers: dict[str, str], api_key: str
) -> None:
    response = client.put(
        "/admin/accounts/acme/users/없는사람/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": True, "reason": "x"},
    )
    assert response.status_code == 404


def test_기준선삭제후가드레일이붙지않는다(
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    _put_config(client, admin_headers)
    assert (
        client.delete(
            "/admin/accounts/acme/guardrail", headers=admin_headers
        ).status_code
        == 204
    )
    _chat(client, api_key)
    assert fake_bedrock.last_call is not None
    config = fake_bedrock.last_call.get("guardrail")
    assert config is None or config.applied is False


def test_조회는설정여부를알려준다(
    client: testclient.TestClient, admin_headers: dict[str, str], api_key: str
) -> None:
    before = client.get("/admin/accounts/acme/guardrail", headers=admin_headers)
    assert before.status_code == 200
    assert before.json()["configured"] is False

    _put_config(client, admin_headers)
    after = client.get("/admin/accounts/acme/guardrail", headers=admin_headers)
    assert after.json()["configured"] is True
    assert after.json()["guardrail_id"] == _GUARDRAIL_ID


# ---------------------------------------------------------------------------
# 관측 — 개입을 사용량에 남기는가
# ---------------------------------------------------------------------------


def test_개입하면사용량에남는다(
    app_services: services.Services,
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    _put_config(client, admin_headers)
    # Bedrock 이 개입을 알리는 상황을 재현한다.
    fake_bedrock.stop_reason = "guardrail_intervened"
    response = _chat(client, api_key)

    # 개입은 오류가 아니다. Bedrock 이 200 과 차단 메시지를 준다.
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "content_filter"

    records = _records(app_services)
    assert records, "사용량 레코드가 없다"
    assert records[0]["guardrail_applied"] is True
    assert records[0]["guardrail_intervened"] is True


def test_적용했지만개입하지않으면구분된다(
    app_services: services.Services,
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    api_key: str,
) -> None:
    _put_config(client, admin_headers)
    assert _chat(client, api_key).status_code == 200
    latest = _records(app_services)[0]
    # "적용했는데 개입 안 함" 과 "적용조차 안 함" 은 다른 상태다.
    assert latest["guardrail_applied"] is True
    assert latest["guardrail_intervened"] is False


def test_면제하면적용안함으로기록된다(
    app_services: services.Services,
    client: testclient.TestClient,
    admin_headers: dict[str, str],
    api_key: str,
) -> None:
    _put_config(client, admin_headers)
    client.put(
        "/admin/accounts/acme/users/alice/guardrail-exemption",
        headers=admin_headers,
        json={"exempt": True, "reason": "평가"},
    )
    assert _chat(client, api_key).status_code == 200
    latest = _records(app_services)[0]
    assert latest["guardrail_applied"] is False
    assert latest["guardrail_exempt_scope"] == "user"
