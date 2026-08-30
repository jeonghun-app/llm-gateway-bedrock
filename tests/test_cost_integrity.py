"""비용 귀속의 정합성 테스트.

단가를 모르는 모델은 비용이 0 으로 집계된다. 예산을 쓰지 않는 주체에게는
보고 정확도 문제지만, 예산을 쓰는 주체에게는 **설정한 상한이 조용히
무효가 되는** 통제 우회다. 그 구분을 고정한다.

멀티모달 조각을 조용히 버리는 문제도 함께 다룬다. 둘 다 "게이트웨이가
실제로 하는 일을 정직하게 말하는가" 에 관한 테스트다.
"""

from __future__ import annotations

import decimal
import typing

from fastapi import testclient

from llmgw import config
from llmgw import domain
from llmgw import services

_UNPRICED_MODEL = "meta.llama3-8b-instruct-v1:0"


def _chat(
    client: testclient.TestClient,
    api_key: str,
    *,
    model: str = "amazon.nova-lite-v1:0",
    messages: list[dict[str, typing.Any]] | None = None,
) -> typing.Any:
    """채팅 완성을 호출한다."""
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": messages or [{"role": "user", "content": "안녕"}],
            "max_tokens": 16,
        },
    )


# ---------------------------------------------------------------------------
# 단가 미등록 + 예산
# ---------------------------------------------------------------------------


def test_단가없는모델은단가표에실제로없다(
    app_services: services.Services,
) -> None:
    """테스트가 쓰는 모델이 정말 단가 미등록인지 먼저 고정한다.

    나중에 단가가 추가되면 이 테스트가 먼저 실패해, 아래 테스트들이 조용히
    무의미해지는 것을 막는다.
    """
    assert app_services.pricing.get(_UNPRICED_MODEL) is None


def test_예산없는키는단가없는모델을쓸수있다(
    client: testclient.TestClient, api_key: str
) -> None:
    # 예산을 쓰지 않으면 비용 0 집계는 보고 정확도 문제일 뿐이다. 모델을
    # 막을 이유가 없다.
    response = _chat(client, api_key, model=_UNPRICED_MODEL)
    assert response.status_code == 200


def test_예산있는키는단가없는모델이거부된다(
    app_services: services.Services,
    registry: typing.Any,
    api_key: str,
) -> None:
    from llmgw import app as app_module

    # 사용자에게 예산을 걸면 단가 없는 모델은 예산을 무효화한다.
    user = registry.get_user("acme", "alice")
    assert user is not None
    registry.put_user(
        user.model_copy(update={"monthly_budget_usd": decimal.Decimal("10")}),
        overwrite=True,
    )

    with testclient.TestClient(
        app_module.create_app_with_services(app_services),
        raise_server_exceptions=False,
    ) as client:
        response = _chat(client, api_key, model=_UNPRICED_MODEL)
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "invalid_request"
        assert "예산" in body["error"]["message"]

        # 단가가 있는 모델은 그대로 통과해야 한다.
        assert _chat(client, api_key).status_code == 200


def test_reject정책은예산이없어도거부한다(
    app_services: services.Services, api_key: str
) -> None:
    from llmgw import app as app_module

    strict = app_services.settings.model_copy(
        update={"unpriced_model_policy": "reject"}
    )
    patched = _with_settings(app_services, strict)
    with testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    ) as client:
        assert _chat(client, api_key, model=_UNPRICED_MODEL).status_code == 400


def _with_settings(
    base: services.Services, settings: config.Settings
) -> services.Services:
    """설정만 교체한 서비스 컨테이너를 만든다."""
    import dataclasses

    return dataclasses.replace(base, settings=settings)


def test_예산보유판정은네계층중하나라도있으면참이다() -> None:
    principal = domain.Principal(
        account_id="a",
        team_id="t",
        user_id="u",
        key_id="k",
        key_hash="h",
        allowed_models=(),
    )
    assert principal.has_monetary_budget is False
    for field in (
        "account_budget_usd",
        "team_budget_usd",
        "user_budget_usd",
        "key_budget_usd",
    ):
        with_budget = principal.model_copy(update={field: decimal.Decimal("1")})
        assert with_budget.has_monetary_budget is True, field


# ---------------------------------------------------------------------------
# 멀티모달 조용한 유실
# ---------------------------------------------------------------------------


def test_이미지조각은거부되고bedrock을호출하지않는다(
    client: testclient.TestClient, api_key: str, fake_bedrock: typing.Any
) -> None:
    fake_bedrock.last_call = None
    response = _chat(
        client,
        api_key,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "이 그림을 설명해줘"},
                    {"type": "image_url", "image_url": {"url": "http://x/y"}},
                ],
            }
        ],
    )
    assert response.status_code == 400
    assert "image_url" in response.json()["error"]["message"]
    # 형식 오류는 비용이 발생하기 전에 걸러야 한다.
    assert fake_bedrock.last_call is None


def test_텍스트조각만있으면통과한다(
    client: testclient.TestClient, api_key: str
) -> None:
    response = _chat(
        client,
        api_key,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "첫째"},
                    {"type": "text", "text": "둘째"},
                ],
            }
        ],
    )
    assert response.status_code == 200
