"""대시보드 차트 렌더링 검증.

차트는 외부 라이브러리 없이 직접 구현한 SVG 렌더러다. 브라우저 없이
검증하려고 `tests/js/charts_harness.js` 가 최소 DOM 셰임을 제공하고, 이
테스트가 그 하네스를 Node 로 실행해 결과를 확인한다.

Node 가 없으면 건너뛴다. 이 프로젝트는 npm 툴체인을 들이지 않기로 했고
Node 를 필수 개발 의존성으로 만들지 않는다. GitHub Actions 러너에는 Node 가
기본 설치되어 있어 CI 에서는 실제로 실행된다.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import typing

import pytest

_HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "charts_harness.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None, reason="node 가 없어 차트 렌더링 검증을 건너뛴다"
)

# 대시보드 API 응답과 같은 모양의 입력. 30일 시계열, 팀 3개, 모델 3개.
_DASHBOARD_FIXTURE: dict[str, typing.Any] = {
    "timeseries": [
        {
            "date": f"2026-08-{day:02d}",
            "cost_usd": round(day * 0.00012, 8),
            "requests": day % 7,
        }
        for day in range(1, 31)
    ],
    "breakdowns": {
        "team": [
            {"key": "platform", "label": "플랫폼팀", "cost_usd": 0.00021},
            {"key": "research", "label": "리서치팀", "cost_usd": 0.00019},
            {"key": "support", "label": "고객지원팀", "cost_usd": 0.00017},
        ],
        "model": [
            {
                "key": "amazon.nova-pro-v1:0",
                "label": "amazon.nova-pro-v1:0",
                "requests": 3,
            },
            {
                "key": "amazon.nova-lite-v1:0",
                "label": "amazon.nova-lite-v1:0",
                "requests": 5,
            },
            {
                "key": "amazon.nova-micro-v1:0",
                "label": "amazon.nova-micro-v1:0",
                "requests": 8,
            },
        ],
    },
}


@pytest.fixture(scope="module")
def rendered() -> dict[str, typing.Any]:
    """하네스를 실행해 렌더링 결과를 반환한다."""
    assert _NODE is not None
    completed = subprocess.run(  # noqa: S603 - 경로가 리포지토리 내부로 고정
        [_NODE, str(_HARNESS)],
        input=json.dumps(_DASHBOARD_FIXTURE),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"하네스 실행 실패:\n{completed.stderr}"
    return typing.cast("dict[str, typing.Any]", json.loads(completed.stdout))


def test_선그래프_시리즈2개가각각polyline으로그려진다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange
    line = rendered["line"]

    # Act / Assert
    assert line["rootTag"] == "svg"
    assert (
        line["polylines"] == 2
    ), f"시리즈 2개면 polyline 2개여야 한다. 실제 {line['polylines']}"


def test_선그래프_모든데이터포인트가표시된다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange
    expected = len(_DASHBOARD_FIXTURE["timeseries"]) * 2  # 시리즈 2개

    # Act
    actual = rendered["line"]["points"]

    # Assert
    assert actual == expected, f"기대 {expected}, 실제 {actual}"


def test_선그래프_접근성속성이있다(
    rendered: dict[str, typing.Any],
) -> None:
    """색만으로 정보를 전달하지 않도록 대체 텍스트가 필요하다."""
    # Arrange / Act
    line = rendered["line"]

    # Assert
    assert line["role"] == "img"
    assert line["hasAriaLabel"] is True


def test_막대그래프_항목수만큼막대가생긴다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange
    expected = len(_DASHBOARD_FIXTURE["breakdowns"]["team"])

    # Act
    bar = rendered["bar"]

    # Assert
    assert bar["rootTag"] == "svg"
    assert bar["rects"] == expected, f"기대 {expected}, 실제 {bar['rects']}"


def test_막대그래프_항목마다툴팁이붙는다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange
    expected = len(_DASHBOARD_FIXTURE["breakdowns"]["team"])

    # Act / Assert
    assert rendered["bar"]["titles"] == expected


def test_도넛_항목수만큼조각과범례가생긴다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange
    expected = len(_DASHBOARD_FIXTURE["breakdowns"]["model"])

    # Act
    donut = rendered["donut"]

    # Assert
    assert donut["rootTag"] == "svg"
    assert donut["paths"] == expected
    assert donut["legendRects"] == expected


def test_빈데이터는안내문구로대체된다(
    rendered: dict[str, typing.Any],
) -> None:
    """데이터가 없을 때 빈 SVG 를 그리면 사용자가 오류로 오해한다."""
    # Arrange / Act
    empty = rendered["emptyLine"]

    # Assert
    assert empty["rootTag"] == "p"
    assert empty["rootClass"] == "chart-empty"


def test_합이0인도넛도안내문구로대체된다(
    rendered: dict[str, typing.Any],
) -> None:
    """0으로 나누는 경로를 타지 않아야 한다."""
    # Arrange / Act
    zero = rendered["zeroDonut"]

    # Assert
    assert zero["rootTag"] == "p"
    assert zero["rootClass"] == "chart-empty"


def test_데이터1건이어도렌더된다(
    rendered: dict[str, typing.Any],
) -> None:
    """x 좌표 계산에서 (n-1) 로 나누는 경로의 경계값이다."""
    # Arrange / Act
    single = rendered["singlePoint"]

    # Assert
    assert single["rootTag"] == "svg"
    assert single["points"] == 1


def test_값이전부0인막대도렌더된다(
    rendered: dict[str, typing.Any],
) -> None:
    # Arrange / Act
    all_zero = rendered["allZeroBar"]

    # Assert
    assert all_zero["rootTag"] == "svg"
    assert all_zero["rects"] == 2


def test_조각이하나뿐인도넛도링이보인다(
    rendered: dict[str, typing.Any],
) -> None:
    """한 조각이 원 전체면 시작점과 끝점이 같아져 경로가 사라진다."""
    # Arrange / Act
    full = rendered["fullCircleDonut"]

    # Assert
    assert full["rootTag"] == "svg"
    assert full["paths"] == 1, "원 전체를 덮는 조각이 그려져야 한다"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("zero", 1),
        ("small", 0.005),
        ("mid", 200),
        ("negative", 1),
    ],
)
def test_축상단값이보기좋은값으로올림된다(
    rendered: dict[str, typing.Any], key: str, expected: float
) -> None:
    # Arrange / Act
    actual = rendered["niceCeiling"][key]

    # Assert
    assert actual == pytest.approx(
        expected
    ), f"{key}: 기대 {expected}, 실제 {actual}"
