"""관리 UI(admin.js/app.js) 동작 검증.

브라우저 없이 관리 화면의 상호작용을 확인한다. `tests/js/admin_ui_harness.js`
가 최소 DOM 셰임과 fetch 목 위에서 실제 클릭·제출 이벤트를 발생시키고, 이
테스트가 그 하네스를 Node 로 실행해 결과를 확인한다.

코드 리뷰에서 지적됐던 세 결함의 회귀를 막는 것이 목적이다.

  1) 발급·재발급한 평문 키 모달이 폼 자동 닫기에 삼켜지지 않는다.
  2) 관리 탭 클릭이 모니터링 탭 상태(activeView, ARIA)를 깨지 않는다.
  3) 계정 생성 직후 상단 계정 선택 목록이 갱신된다.

Node 가 없으면 건너뛴다. 이 프로젝트는 npm 툴체인을 필수 개발 의존성으로
만들지 않는다. GitHub Actions 러너에는 Node 가 기본 설치되어 있어 CI 에서는
실제로 실행된다.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import typing

import pytest

_HARNESS = (
    pathlib.Path(__file__).resolve().parent / "js" / "admin_ui_harness.js"
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None, reason="node 가 없어 관리 UI 동작 검증을 건너뛴다"
)


@pytest.fixture(scope="module")
def outcome() -> dict[str, typing.Any]:
    """하네스를 실행해 시나리오 결과를 반환한다."""
    assert _NODE is not None
    completed = subprocess.run(  # noqa: S603 - 경로가 리포지토리 내부로 고정
        [_NODE, str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"하네스 실행 실패:\n{completed.stderr}"
    return typing.cast("dict[str, typing.Any]", json.loads(completed.stdout))


def test_관리탭클릭이모니터링탭상태를깨지않는다(
    outcome: dict[str, typing.Any],
) -> None:
    # Arrange
    tab = outcome["tabConflict"]

    # Act / Assert
    assert tab["userSelectedBefore"] is True, "사전 조건: 모니터링 탭 선택"
    assert (
        tab["monitorStillIntact"] is True
    ), "관리 탭 클릭이 모니터링 탭 aria-selected 를 바꾸면 안 된다"
    assert (
        tab["manageTabState"] is True
    ), "관리 탭은 자기들끼리만 선택 상태를 관리해야 한다"


def test_계정생성후계정선택목록이갱신된다(
    outcome: dict[str, typing.Any],
) -> None:
    # Arrange
    sync = outcome["accountSync"]

    # Act / Assert
    assert sync["createPosted"] is True, "계정 생성 요청이 전송돼야 한다"
    assert (
        sync["reloadedAfterCreate"] is True
    ), "생성 후 계정 목록을 다시 불러와야 한다"
    assert (
        sync["selectHasBeta"] is True
    ), "상단 계정 선택 목록에 새 계정이 반영돼야 한다"


def test_키발급후평문키모달이유지된다(
    outcome: dict[str, typing.Any],
) -> None:
    # Arrange
    issue = outcome["keyIssueModal"]

    # Act / Assert
    assert issue["modalOpen"] is True, "발급 후 모달이 열려 있어야 한다"
    assert (
        issue["showsPlaintext"] is True
    ), "발급 응답의 평문 키가 모달에 표시돼야 한다"


def test_키재발급후평문키모달이유지된다(
    outcome: dict[str, typing.Any],
) -> None:
    # Arrange
    rotate = outcome["keyRotateModal"]

    # Act / Assert
    assert rotate["modalOpen"] is True, "재발급 후 모달이 열려 있어야 한다"
    assert (
        rotate["showsPlaintext"] is True
    ), "재발급 응답의 평문 키가 모달에 표시돼야 한다"
