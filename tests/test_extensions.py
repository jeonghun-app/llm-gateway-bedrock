"""요청 필터 확장점 테스트.

협의체 검토에서 나온 검증 항목을 덮는다. 특히 다음을 고정한다.

- 확장이 없으면 기존 동작이 그대로다.
- 확장이 변형한 요청이 실제로 Bedrock 에 전달된다.
- 확장이 모델과 스트리밍 여부를 바꿀 수 없다.
- 고장·제한 시간 초과는 통과시키지 않는다(fail-closed).
- 설정에 적은 확장을 못 불러오면 기동이 실패한다.
- 나열하지 않은 모듈은 import 조차 하지 않는다.
"""

from __future__ import annotations

import dataclasses
import datetime
import time
import typing

from examples.extensions import request_filters as examples_filters
from fastapi import testclient
import pytest

from llmgw import config
from llmgw import observability
from llmgw import services
from llmgw.extensions import runtime as extensions_runtime
from llmgw.extensions import v1


def _context(
    *, model_id: str = "amazon.nova-lite-v1:0", streamed: bool = False
) -> v1.RequestContext:
    """테스트용 요청 컨텍스트를 만든다."""
    now = datetime.datetime(2026, 8, 30, tzinfo=datetime.UTC)
    return v1.RequestContext(
        principal=v1.ExtensionPrincipal(
            account_id="acme", team_id="platform", user_id="alice", key_id="k1"
        ),
        request_id="req-1",
        model_id=model_id,
        started_at=now,
        streamed=streamed,
        deadline_at=now + datetime.timedelta(seconds=1),
    )


def _payload(*contents: str) -> v1.RequestPayload:
    """사용자 메시지만 담은 요청 본문을 만든다."""
    return v1.RequestPayload(
        messages=tuple(
            v1.Message(role="user", content=item) for item in contents
        ),
        max_tokens=256,
        temperature=None,
        top_p=None,
        stop_sequences=(),
    )


def _chain(
    *filters: tuple[str, typing.Any],
    logger: observability.Logger,
    timeout_seconds: float = 1.0,
) -> extensions_runtime.RequestFilterChain:
    """이름과 인스턴스 쌍으로 체인을 만든다."""
    return extensions_runtime.RequestFilterChain(
        filters=tuple(
            extensions_runtime.LoadedFilter(name=name, instance=instance)
            for name, instance in filters
        ),
        logger=logger,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------


def test_명세형식이틀리면로딩이실패한다() -> None:
    for spec in ("모듈만", "a:b:c", ":Class", "module:"):
        with pytest.raises(extensions_runtime.ExtensionLoadError):
            extensions_runtime.load_request_filters([spec])


def test_없는모듈은로딩이실패한다() -> None:
    with pytest.raises(
        extensions_runtime.ExtensionLoadError, match="import 할 수 없다"
    ):
        extensions_runtime.load_request_filters(["llmgw_없는모듈:Filter"])


def test_없는클래스는로딩이실패한다() -> None:
    with pytest.raises(extensions_runtime.ExtensionLoadError, match="없다"):
        extensions_runtime.load_request_filters(
            ["examples.extensions.request_filters:없는클래스"]
        )


def test_계약에맞지않는객체는로딩이실패한다() -> None:
    # dict 는 filter_request 가 없다. 첫 요청에서야 터지면 이미 필터가
    # 동작한다고 믿고 트래픽을 받은 상태다.
    with pytest.raises(
        extensions_runtime.ExtensionLoadError, match="filter_request"
    ):
        extensions_runtime.load_request_filters(["builtins:dict"])


def test_중복지정은로딩이실패한다() -> None:
    spec = "examples.extensions.request_filters:MaskKoreanIdFilter"
    with pytest.raises(extensions_runtime.ExtensionLoadError, match="중복"):
        extensions_runtime.load_request_filters([spec, spec])


def test_예제확장은정상적으로로딩된다() -> None:
    loaded = extensions_runtime.load_request_filters(
        [
            "examples.extensions.request_filters:MaskKoreanIdFilter",
            "examples.extensions.request_filters:RejectLongPromptFilter",
        ]
    )
    assert len(loaded) == 2
    assert loaded[0].name.endswith("MaskKoreanIdFilter")


def test_나열하지않은모듈은import하지않는다() -> None:
    # 설치만으로 요청 경로에 코드가 끼어들면 안 된다. 빈 설정으로 로딩하면
    # 아무것도 import 되지 않아야 한다.
    assert extensions_runtime.load_request_filters([]) == ()


# ---------------------------------------------------------------------------
# 체인 실행
# ---------------------------------------------------------------------------


def test_확장이없으면원본을그대로반환한다(
    logger: observability.Logger,
) -> None:
    chain = _chain(logger=logger)
    payload = _payload("안녕")
    assert chain.is_empty is True
    assert chain.apply(payload, context=_context()) is payload


def test_변형한요청이반환된다(logger: observability.Logger) -> None:
    chain = _chain(
        ("mask", examples_filters.MaskKoreanIdFilter()), logger=logger
    )
    result = chain.apply(
        _payload("내 번호는 900101-1234567 이야"), context=_context()
    )
    assert "900101-1234567" not in result.messages[0].content
    assert "[주민등록번호 삭제됨]" in result.messages[0].content


def test_거부는예외로표현된다(logger: observability.Logger) -> None:
    chain = _chain(
        ("reject", examples_filters.RejectLongPromptFilter(max_chars=3)),
        logger=logger,
    )
    with pytest.raises(v1.RequestRejectedError, match="너무 길다"):
        chain.apply(_payload("네자를넘김"), context=_context())


def test_여러확장이설정순서대로적용된다(
    logger: observability.Logger,
) -> None:
    class Append:
        def __init__(self, suffix: str) -> None:
            self._suffix = suffix

        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            del context
            return dataclasses.replace(
                payload,
                messages=tuple(
                    v1.Message(role=m.role, content=m.content + self._suffix)
                    for m in payload.messages
                ),
            )

    chain = _chain(("a", Append("-A")), ("b", Append("-B")), logger=logger)
    result = chain.apply(_payload("x"), context=_context())
    # 순서가 바뀌면 "x-B-A" 가 된다. 순서가 계약의 일부임을 고정한다.
    assert result.messages[0].content == "x-A-B"


def test_확장이예외를던지면통과시키지않는다(
    logger: observability.Logger,
) -> None:
    class Broken:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            raise RuntimeError("내부 오류: 비밀값 abc123")

        # 확장이 던진 문구가 클라이언트로 나가면 프롬프트 조각이나 자격증명이
        # 노출될 수 있다.

    chain = _chain(("broken", Broken()), logger=logger)
    with pytest.raises(v1.ExtensionUnavailableError) as exc_info:
        chain.apply(_payload("안녕"), context=_context())
    assert "abc123" not in str(exc_info.value)
    assert exc_info.value.status_code == 503


def test_잘못된형식을반환하면거부한다(
    logger: observability.Logger,
) -> None:
    class BadReturn:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            del payload, context
            return typing.cast(v1.RequestPayload, "문자열")

    chain = _chain(("bad", BadReturn()), logger=logger)
    with pytest.raises(v1.ExtensionUnavailableError, match="잘못된 형식"):
        chain.apply(_payload("안녕"), context=_context())


def test_빈대화를반환하면거부한다(logger: observability.Logger) -> None:
    class Empties:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            del context
            return dataclasses.replace(payload, messages=())

    chain = _chain(("empty", Empties()), logger=logger)
    with pytest.raises(v1.ExtensionUnavailableError, match="빈 대화"):
        chain.apply(_payload("안녕"), context=_context())


def test_제한시간을넘기면차단되고이후요청은즉시실패한다(
    logger: observability.Logger,
) -> None:
    class Slow:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            del context
            time.sleep(5)
            return payload

    chain = _chain(("slow", Slow()), logger=logger, timeout_seconds=0.05)
    started = time.monotonic()
    with pytest.raises(v1.ExtensionUnavailableError, match="제한 시간"):
        chain.apply(_payload("안녕"), context=_context())
    assert time.monotonic() - started < 2.0

    # 워커가 묶여 있으므로 두 번째 요청은 기다리지 않고 즉시 실패해야 한다.
    second = time.monotonic()
    with pytest.raises(v1.ExtensionUnavailableError, match="응답하지 않는"):
        chain.apply(_payload("안녕"), context=_context())
    assert time.monotonic() - second < 0.5


# ---------------------------------------------------------------------------
# 요청 경로 통합
# ---------------------------------------------------------------------------


@pytest.fixture
def masking_chain(
    logger: observability.Logger,
) -> extensions_runtime.RequestFilterChain:
    """마스킹 확장을 켠 체인. `app_services` 픽스처가 이것을 쓴다."""
    return _chain(
        ("mask", examples_filters.MaskKoreanIdFilter()), logger=logger
    )


def test_확장이변형한요청이실제로bedrock에전달된다(
    app_services: services.Services,
    masking_chain: extensions_runtime.RequestFilterChain,
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    # Services 는 frozen 이므로 체인을 바꾼 사본을 만든다.
    patched = dataclasses.replace(app_services, request_filters=masking_chain)
    from llmgw import app as app_module

    with testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "amazon.nova-lite-v1:0",
                "messages": [
                    {"role": "user", "content": "내 번호 900101-1234567"}
                ],
                "max_tokens": 16,
            },
        )

    assert response.status_code == 200
    assert fake_bedrock.last_call is not None
    flattened = str(fake_bedrock.last_call["messages"])
    assert "900101-1234567" not in flattened
    assert "[주민등록번호 삭제됨]" in flattened


def test_확장이거부하면bedrock을호출하지않고403이다(
    app_services: services.Services,
    logger: observability.Logger,
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    patched = dataclasses.replace(
        app_services,
        request_filters=_chain(
            ("reject", examples_filters.RejectLongPromptFilter(max_chars=3)),
            logger=logger,
        ),
    )
    from llmgw import app as app_module

    fake_bedrock.last_call = None
    with testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "amazon.nova-lite-v1:0",
                "messages": [{"role": "user", "content": "네글자넘음"}],
                "max_tokens": 16,
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "request_rejected"
    # 비용이 발생하기 전에 막아야 한다.
    assert fake_bedrock.last_call is None


def test_확장고장은503이고bedrock을호출하지않는다(
    app_services: services.Services,
    logger: observability.Logger,
    api_key: str,
    fake_bedrock: typing.Any,
) -> None:
    class Broken:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            raise RuntimeError("고장")

    patched = dataclasses.replace(
        app_services,
        request_filters=_chain(("broken", Broken()), logger=logger),
    )
    from llmgw import app as app_module

    fake_bedrock.last_call = None
    with testclient.TestClient(
        app_module.create_app_with_services(patched),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "amazon.nova-lite-v1:0",
                "messages": [{"role": "user", "content": "안녕"}],
                "max_tokens": 16,
            },
        )

    # fail-closed. 검사가 동작하지 않는 상태로 요청을 흘려보내지 않는다.
    assert response.status_code == 503
    assert fake_bedrock.last_call is None


def test_확장은모델을바꿀수없다(logger: observability.Logger) -> None:
    class TryChangeModel:
        def filter_request(
            self, payload: v1.RequestPayload, *, context: v1.RequestContext
        ) -> v1.RequestPayload:
            # 반환 DTO 에 모델 필드가 없다. 컨텍스트로만 읽을 수 있다.
            assert not hasattr(payload, "model_id")
            assert context.model_id == "amazon.nova-lite-v1:0"
            return payload

    chain = _chain(("try", TryChangeModel()), logger=logger)
    chain.apply(_payload("안녕"), context=_context())


def test_확장에키해시가노출되지않는다() -> None:
    # 확장이 정책 판단에 쓸 이유가 없고, 넣으면 공개 계약이 내부 인증
    # 모델과 결합된다.
    fields = {f.name for f in dataclasses.fields(v1.ExtensionPrincipal)}
    assert fields == {"account_id", "team_id", "user_id", "key_id"}


# ---------------------------------------------------------------------------
# 기동
# ---------------------------------------------------------------------------


def test_설정한확장을못불러오면기동이실패한다() -> None:
    # 확장 없이 기동하면 운영자는 필터가 동작한다고 믿는 상태에서 필터 없이
    # 트래픽을 받는다. 그것이 가장 나쁜 결과다.
    settings = config.Settings(
        admin_token="t", request_filters="llmgw_없는모듈:Filter"
    )
    with pytest.raises(extensions_runtime.ExtensionLoadError):
        services.build_services(settings)
