"""단가 표와 비용 계산 테스트."""

from __future__ import annotations

import decimal
import json
import pathlib

import pytest

from llmgw import pricing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0"),
        (
            "us.anthropic.claude-3-haiku-20240307-v1:0",
            "anthropic.claude-3-haiku-20240307-v1:0",
        ),
        (
            "eu.anthropic.claude-3-haiku-20240307-v1:0",
            "anthropic.claude-3-haiku-20240307-v1:0",
        ),
        ("apac.amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"),
        ("global.amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"),
        ("  amazon.nova-micro-v1:0  ", "amazon.nova-micro-v1:0"),
        ("", ""),
        (
            "arn:aws:bedrock:us-east-1:1234:inference-profile/"
            "us.amazon.nova-pro-v1:0",
            "amazon.nova-pro-v1:0",
        ),
    ],
)
def test_normalize_model_id_접두어와arn을제거한다(
    raw: str, expected: str
) -> None:
    # Arrange / Act
    actual = pricing.normalize_model_id(raw)

    # Assert
    assert actual == expected, f"기대 {expected!r}, 실제 {actual!r}"


def test_calculate_정상경로_입출력단가가합산된다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange
    # nova-lite 는 입력 0.001, 출력 0.002 USD/1K 로 픽스처에 설정돼 있다.

    # Act
    result = pricing_table.calculate("amazon.nova-lite-v1:0", 2000, 1000)

    # Assert
    # 2000/1000*0.001 + 1000/1000*0.002 = 0.002 + 0.002 = 0.004
    assert result.cost_usd == decimal.Decimal(
        "0.0040000000"
    ), f"기대 0.004, 실제 {result.cost_usd}"
    assert result.pricing_known is True


def test_calculate_추론프로파일ID도같은단가를쓴다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange / Act
    base = pricing_table.calculate(
        "anthropic.claude-3-haiku-20240307-v1:0", 1000, 1000
    )
    profile = pricing_table.calculate(
        "us.anthropic.claude-3-haiku-20240307-v1:0", 1000, 1000
    )

    # Assert
    assert profile.cost_usd == base.cost_usd
    assert profile.pricing_known is True


def test_calculate_토큰0건_비용은0이다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange / Act
    result = pricing_table.calculate("amazon.nova-lite-v1:0", 0, 0)

    # Assert
    assert result.cost_usd == decimal.Decimal("0")
    assert result.pricing_known is True


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(0, 0), (1, 0), (0, 1), (1000, 500), (200_000, 200_000)],
)
def test_calculate_결과는DynamoDB에저장가능한10진표기다(
    pricing_table: pricing.PricingTable,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """지수 표기(`0E-10`)는 DynamoDB 숫자 문자열로 거부된다.

    `Decimal.quantize` 는 0을 `Decimal('0E-10')` 으로 만든다. 그 값을 그대로
    저장하면 TransactWriteItems 가 통째로 실패해 집계가 유실된다.
    """
    # Arrange / Act
    result = pricing_table.calculate(
        "amazon.nova-lite-v1:0", input_tokens, output_tokens
    )

    # Assert
    rendered = str(result.cost_usd)
    assert "E" not in rendered.upper(), f"지수 표기가 나왔다: {rendered!r}"


def test_calculate_음수토큰_0으로취급한다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange / Act
    result = pricing_table.calculate("amazon.nova-lite-v1:0", -100, -50)

    # Assert
    assert result.cost_usd == decimal.Decimal(
        "0"
    ), "음수 토큰이 마이너스 비용을 만들면 집계가 오염된다"


def test_calculate_단가없는모델_비용0과플래그를반환한다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange / Act
    result = pricing_table.calculate("unknown.brand-new-model", 1000, 1000)

    # Assert
    assert result.cost_usd == decimal.Decimal("0")
    assert (
        result.pricing_known is False
    ), "단가를 모르는 사실이 호출자에게 전달돼야 한다"


def test_calculate_아주작은비용도반올림에서사라지지않는다() -> None:
    # Arrange
    table = pricing.PricingTable(
        {
            "tiny.model": pricing.ModelPrice(
                model_id="tiny.model",
                input_per_1k_usd=decimal.Decimal("0.000035"),
                output_per_1k_usd=decimal.Decimal("0.00014"),
            )
        }
    )

    # Act
    result = table.calculate("tiny.model", 5, 2)

    # Assert
    # 5/1000*0.000035 + 2/1000*0.00014 = 1.75e-7 + 2.8e-7 = 4.55e-7
    assert result.cost_usd == decimal.Decimal(
        "0.0000004550"
    ), f"기대 4.55e-7, 실제 {result.cost_usd}"
    assert result.cost_usd > 0


def test_get_없는모델은None을반환한다(
    pricing_table: pricing.PricingTable,
) -> None:
    # Arrange / Act / Assert
    assert pricing_table.get("nope.nope") is None


def test_from_file_실제pricing_json을로드한다() -> None:
    # Arrange
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "llmgw"
        / "pricing.json"
    )

    # Act
    table = pricing.PricingTable.from_file(path)

    # Assert
    assert "amazon.nova-lite-v1:0" in table.known_model_ids()
    price = table.get("amazon.nova-lite-v1:0")
    assert price is not None
    assert price.input_per_1k_usd > 0
    assert price.output_per_1k_usd > 0


def test_from_file_배포템플릿의모든기본모델에단가가있다() -> None:
    """기본 허용 모델에 단가가 빠져 있으면 비용이 0으로 집계된다.

    `infra/app.yaml` 의 기본 허용 모델 목록과 단가 표가 어긋나는 것을
    배포 전에 잡는다.
    """
    # Arrange
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "llmgw"
        / "pricing.json"
    )
    table = pricing.PricingTable.from_file(path)
    seeded_models = [
        "amazon.nova-micro-v1:0",
        "amazon.nova-lite-v1:0",
        "amazon.nova-pro-v1:0",
        "us.anthropic.claude-3-haiku-20240307-v1:0",
    ]

    # Act
    missing = [
        model_id for model_id in seeded_models if table.get(model_id) is None
    ]

    # Assert
    assert not missing, f"단가 표에 없는 기본 모델: {missing}"


def test_from_file_없는파일_FileNotFoundError() -> None:
    # Arrange / Act / Assert
    with pytest.raises(FileNotFoundError):
        pricing.PricingTable.from_file("/tmp/does-not-exist-pricing.json")


def test_from_file_models키없음_ValueError(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange
    bad_file = tmp_path / "pricing.json"
    bad_file.write_text(json.dumps({"nope": {}}), encoding="utf-8")

    # Act / Assert
    with pytest.raises(ValueError, match="models"):
        pricing.PricingTable.from_file(bad_file)


def test_from_file_잘못된JSON_ValueError(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange
    bad_file = tmp_path / "pricing.json"
    bad_file.write_text("{not json", encoding="utf-8")

    # Act / Assert
    with pytest.raises(ValueError, match="JSON"):
        pricing.PricingTable.from_file(bad_file)


def test_from_file_단가필드누락_ValueError(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange
    bad_file = tmp_path / "pricing.json"
    bad_file.write_text(
        json.dumps({"models": {"x.y": {"input_per_1k_usd": "0.1"}}}),
        encoding="utf-8",
    )

    # Act / Assert
    with pytest.raises(ValueError, match="단가 항목"):
        pricing.PricingTable.from_file(bad_file)
