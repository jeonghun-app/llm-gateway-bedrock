#!/usr/bin/env python3
"""릴리스 아티팩트의 버전이 서로 일치하는지 검사한다.

## 왜 필요한가

v1.13.1 을 릴리스했는데 `infra/app.yaml` 의 `ImageUri` 기본값이 여전히
v1.11.0 이었다. 즉 콘솔에서 파라미터를 비우고 설치한 사람은 두 버전 전
이미지를 받았다. 코드 버전만 올리고 문서와 템플릿을 잊는 것은 사람이 반복해서
놓치는 종류의 실수라 검사로 막는다.

## 검사 대상

- `src/llmgw/__init__.py` 의 `__version__`
- `pyproject.toml` 의 `version`
- `CHANGELOG.md` 최상단 릴리스 항목
- `infra/app.yaml` 의 `ImageUri` 기본값과 예시
- `README.md` / `README.en.md` 의 이미지 태그 예시

종료 코드 0 이면 모두 일치한다.
"""

from __future__ import annotations

import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 이미지 태그는 여러 파일에 예시로 흩어져 있다. 모두 같은 버전을 가리켜야
# 한다. 다이제스트 고정 예시(@sha256:)는 버전 문자열이 없으므로 제외한다.
_IMAGE_TAG = re.compile(r"llm-gateway-bedrock:v(\d+\.\d+\.\d+)")


def _fail(message: str) -> None:
    """오류를 출력하고 즉시 종료한다."""
    print(f"[불일치] {message}", file=sys.stderr)
    sys.exit(1)


def _read(relative: str) -> str:
    """저장소 기준 상대 경로의 파일을 읽는다."""
    return (_ROOT / relative).read_text(encoding="utf-8")


def main() -> int:
    """모든 아티팩트의 버전 일치를 검사한다."""
    init_text = _read("src/llmgw/__init__.py")
    match = re.search(r'__version__ = "([^"]+)"', init_text)
    if match is None:
        _fail("src/llmgw/__init__.py 에서 __version__ 을 찾을 수 없다")
        return 1
    version = match.group(1)
    print(f"기준 버전 (src/llmgw/__init__.py): {version}")

    pyproject = _read("pyproject.toml")
    if f'version = "{version}"' not in pyproject:
        found = re.search(r'^version = "([^"]+)"', pyproject, re.M)
        _fail(
            "pyproject.toml 버전이 다르다: "
            f"{found.group(1) if found else '없음'} != {version}"
        )

    changelog = _read("CHANGELOG.md")
    if f"## [{version}]" not in changelog:
        _fail(f"CHANGELOG.md 에 [{version}] 항목이 없다")

    # 이미지 태그 예시가 모두 현재 버전인지 본다. 릴리스 직전에는 아직
    # 이미지가 발행되지 않았지만, 태그를 밀면 같은 버전이 발행되므로
    # 문서가 미리 그 버전을 가리키는 것이 맞다.
    for relative in (
        "infra/app.yaml",
        "README.md",
        "README.en.md",
        "docs/extensions-v1.md",
    ):
        text = _read(relative)
        stale = {tag for tag in _IMAGE_TAG.findall(text) if tag != version}
        if stale:
            _fail(
                f"{relative} 의 이미지 태그가 뒤처졌다: "
                f"{sorted(stale)} (현재 {version})"
            )

    print("모든 아티팩트 버전이 일치한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
