"""모델 단가 표와 비용 계산.

Bedrock 은 요청 응답에 비용을 돌려주지 않는다. 그래서 토큰 수와 자체
단가 표를 곱해 비용을 산출한다. 단가는 `pricing.json` 에 스냅샷으로 두고
운영 중 갱신할 수 있게 파일 경로를 설정으로 뺐다.

표에 없는 모델은 예외를 던지지 않는다. 새 모델이 나올 때마다 게이트웨이가
요청을 거부하면 가용성이 떨어지기 때문이다. 대신 비용을 0으로 기록하고
`pricing_known=False` 플래그를 남겨 과소 집계를 추적할 수 있게 한다.
"""

from __future__ import annotations

import dataclasses
import decimal
import json
import pathlib
import typing

# 크로스리전 추론 프로파일 ID 는 기반 모델 ID 앞에 리전 그룹 접두어가
# 붙는다. 예: `us.anthropic.claude-3-haiku-20240307-v1:0`. 단가는 동일하므로
# 표를 중복시키지 않고 접두어만 제거해 조회한다.
_INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "us-gov.", "global.")

# 단가는 1,000 토큰 단위로 표기된다.
_TOKENS_PER_PRICE_UNIT = decimal.Decimal("1000")

# 비용 반올림 자리수. nova-micro 로 토큰 몇 개를 쓰면 1e-7 USD 수준이
# 나오므로 8자리로는 0으로 뭉개진다. 10자리를 쓴다.
_COST_EXPONENT = decimal.Decimal("0.0000000001")


@dataclasses.dataclass(frozen=True)
class ModelPrice:
    """모델 하나의 입출력 단가.

    Attributes:
        model_id: 정규화된 기반 모델 ID.
        input_per_1k_usd: 입력 1,000 토큰당 USD.
        output_per_1k_usd: 출력 1,000 토큰당 USD.
    """

    model_id: str
    input_per_1k_usd: decimal.Decimal
    output_per_1k_usd: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class CostResult:
    """비용 계산 결과.

    Attributes:
        cost_usd: 계산된 비용. 단가를 모르면 0.
        pricing_known: 단가 표에서 모델을 찾았는지 여부.
    """

    cost_usd: decimal.Decimal
    pricing_known: bool


def normalize_model_id(model_id: str) -> str:
    """모델 ID 에서 추론 프로파일 접두어를 제거한다.

    `us.anthropic.claude-3-haiku-...` 와 `anthropic.claude-3-haiku-...` 는
    같은 단가를 가지므로 하나로 접는다. 모델 ARN 이 들어오면 마지막
    슬래시 뒤 조각만 사용한다.

    Args:
        model_id: 클라이언트가 보낸 모델 ID 또는 추론 프로파일 ID/ARN.

    Returns:
        정규화된 모델 ID. 입력이 비어 있으면 빈 문자열.

    예시:
        >>> normalize_model_id("us.anthropic.claude-3-haiku-20240307-v1:0")
        'anthropic.claude-3-haiku-20240307-v1:0'
    """
    candidate = model_id.strip()
    if not candidate:
        return ""
    if candidate.startswith("arn:"):
        candidate = candidate.rsplit("/", 1)[-1]
    for prefix in _INFERENCE_PROFILE_PREFIXES:
        if candidate.startswith(prefix):
            return candidate[len(prefix) :]
    return candidate


class PricingTable:
    """모델 단가 조회와 비용 계산을 담당한다."""

    def __init__(self, prices: typing.Mapping[str, ModelPrice]) -> None:
        """단가 매핑으로 표를 만든다.

        Args:
            prices: 정규화된 모델 ID 를 키로 갖는 단가 매핑.
        """
        self._prices = dict(prices)

    @classmethod
    def from_file(cls, path: str | pathlib.Path) -> PricingTable:
        """JSON 파일에서 단가 표를 읽는다.

        금액은 이진 부동소수 오차를 피하려고 JSON 에서 문자열로 저장하고
        `Decimal` 로 파싱한다.

        Args:
            path: `pricing.json` 경로.

        Returns:
            로드된 `PricingTable`.

        Raises:
            FileNotFoundError: 파일이 없는 경우.
            ValueError: JSON 구조가 예상과 다르거나 금액을 파싱할 수 없는
                경우.
        """
        raw_text = pathlib.Path(path).read_text(encoding="utf-8")
        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"단가 파일 JSON 파싱 실패: {path}") from exc

        models = document.get("models")
        if not isinstance(models, dict):
            # 설정 파일 내용이 잘못된 것이므로 TypeError 가 아니라
            # ValueError 가 맞다. 호출자는 파일 경로만 알면 고칠 수 있다.
            raise ValueError(  # noqa: TRY004
                f"단가 파일에 models 객체가 없다: {path}"
            )

        prices: dict[str, ModelPrice] = {}
        for model_id, entry in models.items():
            normalized = normalize_model_id(model_id)
            try:
                prices[normalized] = ModelPrice(
                    model_id=normalized,
                    input_per_1k_usd=decimal.Decimal(
                        str(entry["input_per_1k_usd"])
                    ),
                    output_per_1k_usd=decimal.Decimal(
                        str(entry["output_per_1k_usd"])
                    ),
                )
            except (KeyError, TypeError, decimal.InvalidOperation) as exc:
                raise ValueError(
                    f"단가 항목이 올바르지 않다: {model_id}"
                ) from exc
        return cls(prices)

    def get(self, model_id: str) -> ModelPrice | None:
        """모델 단가를 조회한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.

        Returns:
            찾은 단가. 표에 없으면 `None`.
        """
        return self._prices.get(normalize_model_id(model_id))

    def known_model_ids(self) -> tuple[str, ...]:
        """단가를 아는 모델 ID 목록을 정렬해 반환한다."""
        return tuple(sorted(self._prices))

    def calculate(
        self, model_id: str, input_tokens: int, output_tokens: int
    ) -> CostResult:
        """토큰 수로 비용을 계산한다.

        Args:
            model_id: 모델 ID 또는 추론 프로파일 ID.
            input_tokens: 입력 토큰 수. 음수는 0으로 취급한다.
            output_tokens: 출력 토큰 수. 음수는 0으로 취급한다.

        Returns:
            비용과 단가 인지 여부를 담은 `CostResult`.
        """
        price = self.get(model_id)
        if price is None:
            return CostResult(
                cost_usd=decimal.Decimal("0"), pricing_known=False
            )

        safe_input = decimal.Decimal(max(input_tokens, 0))
        safe_output = decimal.Decimal(max(output_tokens, 0))
        raw_cost = (
            safe_input * price.input_per_1k_usd
            + safe_output * price.output_per_1k_usd
        ) / _TOKENS_PER_PRICE_UNIT
        # 은행가 반올림 대신 절반 올림을 명시적으로 고른다. 과소 청구보다
        # 과대 집계가 운영상 안전하다.
        quantized = raw_cost.quantize(
            _COST_EXPONENT, rounding=decimal.ROUND_HALF_UP
        )
        return CostResult(cost_usd=_storable(quantized), pricing_known=True)


def _storable(value: decimal.Decimal) -> decimal.Decimal:
    """DynamoDB 숫자 속성으로 저장 가능한 형태로 정규화한다.

    `Decimal("0").quantize(Decimal("1e-10"))` 은 `Decimal('0E-10')` 이 되고,
    문자열로는 `"0E-10"` 이 된다. DynamoDB 는 이 지수 표기를 거부하므로
    사용량 기록 트랜잭션 전체가 실패한다. 0은 지수 없는 `Decimal("0")` 로
    바꿔 그 경로를 막는다.

    Args:
        value: 정규화할 값.

    Returns:
        10진 표기로 직렬화되는 `Decimal`.
    """
    if value == 0:
        return decimal.Decimal("0")
    return value
