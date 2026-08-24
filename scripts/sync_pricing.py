#!/usr/bin/env python3
"""AWS Price List API 로 모델 단가를 확인하고 `pricing.json` 갱신을 돕는다.

`src/llmgw/pricing.json` 은 손으로 관리하는 스냅샷이다. 단가가 바뀌거나 새
모델이 추가되면 실제 지출과 집계가 벌어진다. 이 스크립트는 권위 있는 출처인
Price List API 와 현재 표를 대조해 세 가지를 보고한다.

    누락  게이트웨이가 호출 가능한데 표에 없는 모델. 비용이 0으로 집계된다.
    불일치 표의 값과 API 값이 다른 모델.
    미확인 API 에도 없어 자동 확인이 불가능한 모델.

`--apply` 를 주면 API 로 확인된 값만 표에 반영한다. **확인되지 않은 모델의
단가를 추측해 넣지 않는다.** 틀린 단가는 조용히 잘못된 비용을 만들어, 표에
없어서 `pricing_known=false` 로 드러나는 상태보다 나쁘다.

사용법
    ./.venv/bin/python scripts/sync_pricing.py
    ./.venv/bin/python scripts/sync_pricing.py --region us-east-1 --apply
"""

from __future__ import annotations

import argparse
import collections
import decimal
import json
import pathlib
import sys
import typing

import boto3
import botocore.exceptions

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from llmgw import pricing as pricing_module  # noqa: E402

PRICING_FILE = _REPO_ROOT / "src" / "llmgw" / "pricing.json"

# Price List API 는 us-east-1 과 ap-south-1 에서만 제공된다.
_PRICING_API_REGION = "us-east-1"

# 입력/출력 토큰을 뜻하는 inferenceType 값. 배치·플렉스·우선순위 티어는
# 온디맨드 표준 단가와 다르므로 제외한다.
_INPUT_TYPES = frozenset({"Input tokens"})
_OUTPUT_TYPES = frozenset({"Output tokens"})

_PRICE_UNIT = "1K tokens"


class _Observed(typing.NamedTuple):
    """API 에서 관측한 단가."""

    model_label: str
    input_per_1k: decimal.Decimal | None
    output_per_1k: decimal.Decimal | None


def fetch_prices(region: str) -> dict[str, _Observed]:
    """Price List API 에서 온디맨드 토큰 단가를 수집한다.

    Args:
        region: 단가를 조회할 대상 리전 코드.

    Returns:
        모델 표시 이름(소문자)을 키로 갖는 관측 단가 매핑.

    Raises:
        SystemExit: API 호출 권한이 없거나 실패한 경우.
    """
    client = boto3.client("pricing", region_name=_PRICING_API_REGION)
    inputs: dict[str, decimal.Decimal] = {}
    outputs: dict[str, decimal.Decimal] = {}
    labels: dict[str, str] = {}

    try:
        paginator = client.get_paginator("get_products")
        pages = paginator.paginate(
            ServiceCode="AmazonBedrock",
            Filters=[
                {
                    "Type": "TERM_MATCH",
                    "Field": "regionCode",
                    "Value": region,
                }
            ],
        )
        for page in pages:
            for raw in page["PriceList"]:
                document = json.loads(raw) if isinstance(raw, str) else raw
                _collect(document, inputs, outputs, labels)
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.BotoCoreError,
    ) as exc:
        raise SystemExit(f"Price List API 호출 실패: {exc}") from exc

    observed: dict[str, _Observed] = {}
    for key in set(inputs) | set(outputs):
        observed[key] = _Observed(
            model_label=labels.get(key, key),
            input_per_1k=inputs.get(key),
            output_per_1k=outputs.get(key),
        )
    return observed


def _collect(
    document: dict[str, typing.Any],
    inputs: dict[str, decimal.Decimal],
    outputs: dict[str, decimal.Decimal],
    labels: dict[str, str],
) -> None:
    """가격 문서 하나에서 입력/출력 단가를 추출해 누적한다."""
    attributes = document.get("product", {}).get("attributes", {})
    model_label = str(attributes.get("model") or "").strip()
    if not model_label:
        return
    inference_type = str(attributes.get("inferenceType") or "")
    # tokenType 이 지정된 항목은 캐시 읽기/쓰기, 롱컨텍스트, mantle 전용 등
    # 표준 온디맨드 단가가 아니다.
    if attributes.get("tokenType"):
        return

    key = model_label.lower()
    labels[key] = model_label

    for term in document.get("terms", {}).get("OnDemand", {}).values():
        for dimension in term.get("priceDimensions", {}).values():
            if dimension.get("unit") != _PRICE_UNIT:
                continue
            raw_price = dimension.get("pricePerUnit", {}).get("USD")
            if raw_price is None:
                continue
            price = decimal.Decimal(str(raw_price))
            if inference_type in _INPUT_TYPES:
                inputs[key] = price
            elif inference_type in _OUTPUT_TYPES:
                outputs[key] = price


def load_table() -> dict[str, typing.Any]:
    """현재 `pricing.json` 을 읽는다."""
    return typing.cast(
        "dict[str, typing.Any]",
        json.loads(PRICING_FILE.read_text(encoding="utf-8")),
    )


def gateway_models(region: str) -> list[str]:
    """게이트웨이가 노출하는 모델 ID 목록을 얻는다.

    Args:
        region: Bedrock 리전.

    Returns:
        기반 모델과 활성 추론 프로파일 ID 를 합친 정렬된 목록.
    """
    client = boto3.client("bedrock", region_name=region)
    model_ids: set[str] = set()
    try:
        response = client.list_foundation_models(byOutputModality="TEXT")
        for summary in response.get("modelSummaries", []):
            model_id = summary.get("modelId")
            if model_id:
                model_ids.add(str(model_id))
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.BotoCoreError,
    ) as exc:
        print(f"  기반 모델 조회 실패: {exc}", file=sys.stderr)

    try:
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate():
            for profile in page.get("inferenceProfileSummaries", []):
                if profile.get("status") != "ACTIVE":
                    continue
                profile_id = profile.get("inferenceProfileId")
                if profile_id:
                    model_ids.add(str(profile_id))
    except (
        botocore.exceptions.ClientError,
        botocore.exceptions.BotoCoreError,
    ):
        pass

    return sorted(model_ids)


def _match_observed(
    model_id: str, observed: dict[str, _Observed]
) -> _Observed | None:
    """모델 ID 에 대응하는 관측 단가를 찾는다.

    Price List API 는 `Claude 3 Haiku` 같은 표시 이름을 쓰고 Bedrock API 는
    `anthropic.claude-3-haiku-20240307-v1:0` 같은 ID 를 쓴다. 둘을 정확히
    매핑하는 공개 키가 없어 느슨한 토큰 포함 비교를 한다. 확실하지 않으면
    `None` 을 반환해 사람이 판단하게 남긴다.

    Args:
        model_id: Bedrock 모델 ID.
        observed: API 에서 관측한 단가 매핑.

    Returns:
        일치하는 관측값. 판단이 모호하면 `None`.
    """
    normalized = pricing_module.normalize_model_id(model_id)
    # 공급자 접두어를 떼고 하이픈·언더바를 공백으로 바꿔 토큰 집합을 만든다.
    tail = normalized.split(".", 1)[-1]
    tail = tail.split(":", 1)[0]
    tokens = {
        token
        for token in tail.replace("-", " ").replace("_", " ").split()
        # 날짜 문자열(20240307)과 버전 조각은 비교에서 제외한다.
        if not token.isdigit() or len(token) <= 2
    }
    if not tokens:
        return None

    candidates = [
        entry
        for key, entry in observed.items()
        if tokens.issubset(set(key.replace("-", " ").split()))
    ]
    # 여러 후보가 잡히면 사람이 확인해야 한다.
    return candidates[0] if len(candidates) == 1 else None


def main() -> int:
    """엔트리 포인트.

    Returns:
        프로세스 종료 코드. 누락이나 불일치가 있으면 1.
    """
    parser = argparse.ArgumentParser(
        description="Bedrock 모델 단가 표 점검과 갱신"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="단가와 모델 목록을 조회할 리전 (기본 us-east-1)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="API 로 확인된 값만 pricing.json 에 반영한다",
    )
    args = parser.parse_args()

    table_document = load_table()
    table = pricing_module.PricingTable.from_file(PRICING_FILE)

    print(f"리전 {args.region} · 단가 표 항목 {len(table.known_model_ids())}개")
    print("Price List API 조회 중...")
    observed = fetch_prices(args.region)
    print(f"  API 에서 {len(observed)}개 모델 단가 확인")

    print("게이트웨이 노출 모델 조회 중...")
    available = gateway_models(args.region)
    print(f"  {len(available)}개 모델")

    report: dict[str, set[str]] = collections.defaultdict(set)
    updates: dict[str, tuple[decimal.Decimal, decimal.Decimal]] = {}

    # 기반 모델과 us./global. 추론 프로파일은 같은 ID 로 정규화된다.
    # 중복 보고를 피하려고 정규화된 ID 로 한 번만 검사한다.
    seen: set[str] = set()
    for model_id in available:
        normalized = pricing_module.normalize_model_id(model_id)
        if normalized in seen:
            continue
        seen.add(normalized)
        current = table.get(model_id)
        match = _match_observed(model_id, observed)

        if match is None or match.input_per_1k is None:
            if current is None:
                report["미확인·누락"].add(normalized)
            continue

        api_input = match.input_per_1k
        api_output = (
            match.output_per_1k
            if match.output_per_1k is not None
            else decimal.Decimal("0")
        )
        if current is None:
            report["누락(API 확인됨)"].add(
                f"{normalized}  입력 {api_input} / 출력 {api_output}"
            )
            updates[normalized] = (api_input, api_output)
        elif (
            current.input_per_1k_usd != api_input
            or current.output_per_1k_usd != api_output
        ):
            report["불일치"].add(
                f"{normalized}  표 {current.input_per_1k_usd}/"
                f"{current.output_per_1k_usd}  →  API {api_input}/{api_output}"
            )
            updates[normalized] = (api_input, api_output)

    print()
    for section in ("불일치", "누락(API 확인됨)", "미확인·누락"):
        entries = report.get(section, set())
        print(f"── {section}: {len(entries)}건")
        for entry in sorted(entries):
            print(f"     {entry}")
    print()

    unverified = report.get("미확인·누락", set())
    if unverified:
        print(
            "미확인·누락 모델은 단가를 자동으로 정할 수 없다. 이 상태에서는"
            " 비용이 0으로 집계되고 usage 레코드에 pricing_known=false 가"
            " 남으며, 대시보드의 '단가 미등록' 건수에 잡힌다."
        )
        print(
            "  AWS 요금 페이지에서 값을 확인해"
            " src/llmgw/pricing.json 에 직접 추가한다."
        )
        print("  추측한 값을 넣지 않는다. 틀린 단가는 누락보다 나쁘다.")
        print()

    if args.apply and updates:
        models = table_document.setdefault("models", {})
        for model_id, (price_in, price_out) in sorted(updates.items()):
            models[model_id] = {
                "input_per_1k_usd": str(price_in),
                "output_per_1k_usd": str(price_out),
            }
        meta = table_document.setdefault("_meta", {})
        meta["synced_from_price_list_api"] = args.region
        PRICING_FILE.write_text(
            json.dumps(table_document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"{len(updates)}건을 pricing.json 에 반영했다.")
        print("변경 내용을 git diff 로 확인하고 테스트를 실행한다.")
    elif updates:
        print(f"{len(updates)}건을 반영할 수 있다. --apply 를 주면 적용한다.")

    return 1 if (updates or unverified) else 0


if __name__ == "__main__":
    sys.exit(main())
