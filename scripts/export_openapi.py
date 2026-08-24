#!/usr/bin/env python3
"""OpenAPI 스펙을 `docs/openapi.json` 으로 내보낸다.

스펙을 리포지토리에 커밋해 두면 배포하지 않고도 API 계약을 볼 수 있다.
대신 코드와 어긋날 위험이 생기므로, `tests/test_openapi_export.py` 가 이
스크립트의 출력과 커밋된 파일을 비교해 불일치를 실패로 잡는다.

라우터나 스키마를 바꾼 뒤에는 이 스크립트를 다시 실행한다.

    ./.venv/bin/python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from llmgw import app as app_module  # noqa: E402
from llmgw import config  # noqa: E402

OUTPUT_PATH = _REPO_ROOT / "docs" / "openapi.json"


def build_spec() -> dict[str, object]:
    """앱의 OpenAPI 스펙을 만든다.

    AWS 호출 없이 스펙만 필요하므로, 실제 자격증명이나 테이블이 없어도
    동작하는 더미 설정으로 앱을 만든다.

    Returns:
        OpenAPI 문서 딕셔너리.
    """
    settings = config.Settings(
        env="docs",
        aws_region="us-east-1",
        registry_table="llmgw-docs-registry",
        usage_table="llmgw-docs-usage",
        usage_agg_table="llmgw-docs-usage-agg",
        admin_token="placeholder-for-spec-generation",
    )
    application = app_module.create_app(settings)
    return dict(application.openapi())


def render(spec: dict[str, object]) -> str:
    """스펙을 결정적인 JSON 문자열로 만든다.

    키를 정렬하고 개행을 고정해 diff 가 안정적으로 나오게 한다.

    Args:
        spec: OpenAPI 문서.

    Returns:
        파일에 쓸 문자열.
    """
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    """스펙을 파일로 쓴다.

    Returns:
        프로세스 종료 코드.
    """
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render(build_spec()), encoding="utf-8")
    print(f"{OUTPUT_PATH.relative_to(_REPO_ROOT)} 갱신 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
