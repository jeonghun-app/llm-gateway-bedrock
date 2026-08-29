"""대시보드 다국어 회귀 테스트.

원문(한국어)을 키로 쓰는 방식이라 한국어 원문을 고치면 영어 매핑이 조용히
끊긴다. 이 테스트가 그 사고를 잡는다.

1. 화면에 쓰이는 모든 한국어 문자열이 사전에 있는가.
2. 언어를 바꾸면 정적 텍스트와 **동적으로 그려지는 표 헤더**까지 바뀌는가.
   표 헤더는 모듈 로드 시점에 한 번 평가되는 실수를 하기 쉬운 지점이다.
"""

from __future__ import annotations

import pathlib
import re
import typing

from playwright import sync_api
import pytest

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "src/llmgw/static"
_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")

pytestmark = pytest.mark.browser


def _dictionary_keys() -> set[str]:
    """i18n.js 의 영어 사전 키를 읽는다."""
    source = (_STATIC / "i18n.js").read_text(encoding="utf-8")
    start = source.index("const EN = {")
    end = source.index("\n  };", start)
    block = source[start:end]
    keys: set[str] = set()
    # 값이 여러 줄로 이어질 수 있어 줄 단위로 키만 찾는다. 인용된 키
    # (`'... ': '...'`)와 비인용 키(`요약: '...'`) 두 형태를 모두 다룬다.
    for line in block.splitlines():
        quoted = re.match(r"\s*'((?:[^'\\]|\\.)*)'\s*:", line)
        if quoted:
            keys.add(quoted.group(1))
            continue
        bare = re.match(
            r"\s*([^\s'\"][^:]*?)\s*:\s*$|\s*([^\s'\"][^:]*?)\s*:\s*'", line
        )
        if bare:
            key = bare.group(1) or bare.group(2)
            if key and _HANGUL.search(key):
                keys.add(key.strip())
    return keys


def _wrapped_strings() -> set[str]:
    """`t('...')` 로 감싼 한국어 문자열을 모은다."""
    found: set[str] = set()
    for name in ("app.js", "admin.js", "charts.js"):
        source = (_STATIC / name).read_text(encoding="utf-8")
        for match in re.finditer(r"t\(\s*'((?:[^'\\]|\\.)*)'", source):
            value = match.group(1)
            if _HANGUL.search(value):
                found.add(value)
        for match in re.finditer(r't\(\s*"((?:[^"\\]|\\.)*)"', source):
            value = match.group(1)
            if _HANGUL.search(value):
                found.add(value)
    return found


def _static_attributes() -> set[str]:
    """index.html 의 `data-i18n*` 값을 모은다."""
    source = (_STATIC / "index.html").read_text(encoding="utf-8")
    return {
        value
        for value in re.findall(
            r'data-i18n(?:-aria|-placeholder)?="([^"]+)"', source
        )
        if _HANGUL.search(value)
    }


def test_모든번역대상문자열이사전에있다() -> None:
    """사전에 없으면 영어 모드에서 한국어가 그대로 노출된다."""
    # Arrange
    keys = _dictionary_keys()

    # Act
    used = _wrapped_strings() | _static_attributes()
    missing = sorted(item for item in used if item not in keys)

    # Assert
    assert (
        not missing
    ), f"영어 번역이 없는 문자열 {len(missing)}개: {missing[:10]}"


def test_JS의한국어리터럴이모두t로감싸져있다() -> None:
    """감싸지 않으면 언어를 바꿔도 그 문자열만 한국어로 남는다."""
    # Arrange / Act
    unwrapped: list[str] = []
    for name in ("app.js", "admin.js", "charts.js"):
        source = (_STATIC / name).read_text(encoding="utf-8")
        lines = source.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(("*", "//", "/*")):
                continue
            for match in _LITERAL.finditer(line):
                value = match.group(1)
                if value is None:
                    value = match.group(2)
                if value is None or not _HANGUL.search(value):
                    continue
                # 같은 줄에서 `t('...')` 로 감싼 경우.
                prefix = line[max(0, match.start() - 2) : match.start()]
                if prefix.endswith("t("):
                    continue
                # 줄바꿈된 호출: 앞 줄이 `t(` 로 끝나는 경우.
                previous = lines[index - 1].rstrip() if index else ""
                if previous.endswith("t("):
                    continue
                # `return '팀';` 처럼 번역 키를 돌려주는 함수. 호출부에서
                # `t(scopeLabel(...))` 로 감싸므로 여기서 감쌀 필요가 없다.
                if stripped.startswith("return "):
                    continue
                unwrapped.append(f"{name}:{index + 1}: {value[:40]}")

    # Assert
    assert not unwrapped, (
        f"t() 로 감싸지 않은 한국어 문자열 {len(unwrapped)}개: "
        f"{unwrapped[:8]}"
    )


def test_언어를바꾸면정적텍스트와표헤더가함께바뀐다(
    ui: typing.Any,
) -> None:
    """표 헤더는 모듈 로드 시점에 한 번 평가되는 실수를 하기 쉬운 지점이다.

    상수로 두면 언어를 바꿔도 헤더만 한국어로 남는다.
    """
    page = ui.page

    # Arrange: 데이터를 불러와 표를 그린다.
    page.locator("#admin-token").fill("test-token")
    page.locator("#refresh-button").click()
    sync_api.expect(page.locator("#kpi-heading")).to_have_text("요약")

    korean_headers = page.locator("#detail-table thead th").all_inner_texts()

    # Act: 영어로 전환한다.
    page.locator('[data-lang="en"]').click()

    # Assert: 정적 텍스트와 동적 표 헤더가 모두 영어여야 한다.
    sync_api.expect(page.locator("#kpi-heading")).to_have_text("Summary")
    sync_api.expect(page.locator("#refresh-button")).to_have_text("Load")
    english_headers = page.locator("#detail-table thead th").all_inner_texts()
    assert english_headers != korean_headers, (
        "언어를 바꿨는데 표 헤더가 그대로다. 헤더 정의가 모듈 로드 시점에"
        f" 고정됐을 수 있다: {english_headers}"
    )
    assert not any(
        _HANGUL.search(item) for item in english_headers
    ), f"영어 모드인데 표 헤더에 한국어가 남아 있다: {english_headers}"

    # 캡션도 함께 바뀐다.
    caption = page.locator("#table-caption").inner_text()
    assert not _HANGUL.search(caption), f"캡션이 번역되지 않았다: {caption}"

    # Act: 한국어로 되돌린다.
    page.locator('[data-lang="ko"]').click()

    # Assert
    sync_api.expect(page.locator("#refresh-button")).to_have_text("조회")
    assert page.locator("#detail-table thead th").all_inner_texts() == (
        korean_headers
    )
    assert ui.page_errors == []


def test_영어모드에서미번역문자열이없다(ui: typing.Any) -> None:
    """모니터링·상세 6탭·관리 5탭을 모두 순회해 누락을 찾는다."""
    page = ui.page

    # Arrange
    page.locator('[data-lang="en"]').click()
    page.locator("#admin-token").fill("test-token")
    page.locator("#refresh-button").click()
    sync_api.expect(page.locator("#kpi-heading")).to_have_text("Summary")

    # Act: 모든 화면을 열어 동적 문자열까지 평가시킨다.
    for tab in (
        "tab-accounts",
        "tab-team",
        "tab-user",
        "tab-model",
        "tab-key",
        "tab-requests",
    ):
        page.locator(f"#{tab}").click()
    page.locator("#view-manage").click()
    for tab in (
        "mtab-accounts",
        "mtab-teams",
        "mtab-users",
        "mtab-keys",
        "mtab-auth",
    ):
        page.locator(f"#{tab}").click()
        page.wait_for_timeout(150)

    missing = typing.cast(
        "list[str]", page.evaluate("window.LlmgwI18n.missing()")
    )

    # Assert
    assert missing == [], f"영어 번역이 없는 문자열: {missing[:10]}"
    assert ui.page_errors == []
