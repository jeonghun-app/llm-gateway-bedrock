"""커밋된 OpenAPI 스펙이 코드와 일치하는지 검증한다.

낡은 스펙은 없는 스펙보다 나쁘다. 라우터나 스키마를 바꾸고 스펙 갱신을
잊으면 이 테스트가 실패한다.
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import export_openapi  # noqa: E402


def test_커밋된openapi_json이코드와일치한다() -> None:
    # Arrange
    committed_path = export_openapi.OUTPUT_PATH
    assert committed_path.is_file(), (
        f"{committed_path} 가 없다. "
        "'./.venv/bin/python scripts/export_openapi.py' 로 생성한다."
    )

    # Act
    expected = export_openapi.render(export_openapi.build_spec())
    actual = committed_path.read_text(encoding="utf-8")

    # Assert
    assert actual == expected, (
        "커밋된 docs/openapi.json 이 코드와 다르다. "
        "'./.venv/bin/python scripts/export_openapi.py' 를 실행해 갱신한다."
    )


def test_스펙에핵심엔드포인트가모두있다() -> None:
    # Arrange
    spec = export_openapi.build_spec()

    # Act
    paths = set(spec["paths"])  # type: ignore[arg-type]

    # Assert
    required = {
        "/healthz",
        "/readyz",
        "/v1/models",
        "/v1/chat/completions",
        "/admin/accounts",
        "/admin/accounts/{account_id}/keys",
        "/analytics/dashboard",
        "/analytics/breakdown",
    }
    missing = required - paths
    assert not missing, f"스펙에 빠진 경로: {sorted(missing)}"
