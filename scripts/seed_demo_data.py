#!/usr/bin/env python3
"""데모 계정 구조를 만들고 대시보드에 보일 사용량을 생성한다.

만드는 것
    계정 2개 (acme, beta), 팀 3개, 사용자 5명, 사용자별 API 키 1개.
    그리고 여러 사용자·모델 조합으로 실제 Bedrock 호출을 몇 건 넣어
    대시보드의 계정 / 팀 / 사용자 / 모델 축이 모두 채워지게 한다.

표준 라이브러리만 쓴다. 배포 호스트에 가상환경이 없어도 시스템 python3 로
그대로 실행되어야 하기 때문이다.

발급된 평문 API 키는 `--output` 파일에 저장된다. 이 파일은 시크릿이다.
`.deploy/` 는 `.gitignore` 에 포함되어 있으니 다른 경로로 옮기지 않는다.

사용법
    LLMGW_BASE_URL=http://alb-dns LLMGW_ADMIN_TOKEN=... \\
        python3 scripts/seed_demo_data.py --output .deploy/demo-keys.json
"""

from __future__ import annotations

import argparse
import http
import json
import os
import pathlib
import sys
import typing
import urllib.error
import urllib.request

# 데모 조직 구조. 팀과 사용자를 여러 개 두어 대시보드의 축별 분해가
# 의미 있게 보이도록 했다.
_ACCOUNTS: tuple[dict[str, typing.Any], ...] = (
    {
        "account_id": "acme",
        "name": "Acme Corporation",
        "monthly_budget_usd": 500,
        "teams": [
            {
                "team_id": "platform",
                "name": "플랫폼팀",
                "monthly_budget_usd": 200,
            },
            {
                "team_id": "research",
                "name": "리서치팀",
                "monthly_budget_usd": 200,
            },
            {"team_id": "support", "name": "고객지원팀"},
        ],
        "users": [
            {
                "user_id": "alice",
                "name": "김앨리스",
                "team_id": "platform",
                "email": "alice@example.com",
                "monthly_budget_usd": 100,
            },
            {
                "user_id": "bob",
                "name": "박밥",
                "team_id": "platform",
                "email": "bob@example.com",
            },
            {
                "user_id": "carol",
                "name": "이캐롤",
                "team_id": "research",
                "email": "carol@example.com",
                "monthly_budget_usd": 150,
            },
            {
                "user_id": "dave",
                "name": "최데이브",
                "team_id": "research",
                "email": "dave@example.com",
            },
            {
                "user_id": "eve",
                "name": "정이브",
                "team_id": "support",
                "email": "eve@example.com",
            },
        ],
    },
    {
        "account_id": "beta",
        "name": "Beta Startup",
        "monthly_budget_usd": 100,
        "teams": [
            {"team_id": "engineering", "name": "엔지니어링"},
        ],
        "users": [
            {
                "user_id": "frank",
                "name": "한프랭크",
                "team_id": "engineering",
                "email": "frank@example.com",
            },
        ],
    },
)

# 트래픽 생성에 쓸 모델. 저렴하고 온디맨드로 널리 열려 있는 것을 고른다.
# 계정에서 활성화되지 않은 모델은 건너뛴다.
_TRAFFIC_MODELS = (
    "amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0",
)

_TRAFFIC_PROMPTS = (
    "한 문장으로 자기소개를 해줘.",
    "1부터 5까지 세어줘.",
    "'안녕'을 영어로 번역해줘.",
    "파이썬에서 리스트와 튜플의 차이를 한 문장으로 설명해줘.",
)

_HTTP_TIMEOUT_SECONDS = 120


class SeedError(RuntimeError):
    """시드 과정에서 복구할 수 없는 오류."""


def _request(
    *,
    base_url: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, typing.Any] | None = None,
) -> tuple[int, dict[str, typing.Any]]:
    """JSON HTTP 요청을 보낸다.

    Args:
        base_url: 게이트웨이 기본 URL.
        method: HTTP 메서드.
        path: 경로.
        headers: 추가 헤더.
        body: JSON 본문. `None` 이면 본문 없이 보낸다.

    Returns:
        (상태 코드, 파싱된 본문) 튜플. 본문이 JSON 이 아니면 빈 딕셔너리.
    """
    payload = None
    request_headers = dict(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(  # noqa: S310 - 호출자가 준 신뢰된 URL
        url=f"{base_url.rstrip('/')}{path}",
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=_HTTP_TIMEOUT_SECONDS
        ) as response:
            raw = response.read().decode("utf-8")
            return response.status, _safe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json(raw)
    except urllib.error.URLError as exc:
        raise SeedError(f"{path} 요청 실패: {exc.reason}") from exc


def _safe_json(raw: str) -> dict[str, typing.Any]:
    """JSON 문자열을 딕셔너리로 파싱한다. 실패하면 원문을 담아 반환한다."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:400]}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _admin_post(
    base_url: str, token: str, path: str, body: dict[str, typing.Any]
) -> dict[str, typing.Any]:
    """관리 API POST 를 보내고, 이미 존재하는 리소스는 성공으로 취급한다.

    시드를 여러 번 돌려도 안전해야 한다. 409 를 오류로 다루면 재실행이
    불가능해진다.

    Args:
        base_url: 게이트웨이 기본 URL.
        token: 관리 토큰.
        path: 경로.
        body: 요청 본문.

    Returns:
        응답 본문. 이미 존재하면 `{"already_exists": True}`.

    Raises:
        SeedError: 생성도 아니고 중복도 아닌 오류가 발생한 경우.
    """
    status, response = _request(
        base_url=base_url,
        method="POST",
        path=path,
        headers={"X-Admin-Token": token},
        body=body,
    )
    if status == http.HTTPStatus.CREATED:
        return response
    if status == http.HTTPStatus.CONFLICT:
        return {"already_exists": True}
    message = (
        response.get("error", {}).get("message")
        if isinstance(response.get("error"), dict)
        else json.dumps(response, ensure_ascii=False)[:300]
    )
    raise SeedError(f"{path} 실패 (HTTP {status}): {message}")


def _available_models(base_url: str, token: str) -> set[str]:
    """게이트웨이가 노출하는 모델 ID 집합을 얻는다."""
    status, response = _request(
        base_url=base_url,
        method="GET",
        path="/admin/models",
        headers={"X-Admin-Token": token},
    )
    if status != http.HTTPStatus.OK:
        return set()
    return {
        str(entry.get("model_id"))
        for entry in response.get("data", [])
        if entry.get("model_id")
    }


def seed_structure(base_url: str, token: str) -> list[dict[str, typing.Any]]:
    """계정·팀·사용자·키를 만들고 발급된 키 목록을 반환한다.

    Args:
        base_url: 게이트웨이 기본 URL.
        token: 관리 토큰.

    Returns:
        발급된 키 정보 목록. 이미 있던 사용자는 키를 새로 발급한다.

    Raises:
        SeedError: 생성이 실패한 경우.
    """
    issued: list[dict[str, typing.Any]] = []

    for account in _ACCOUNTS:
        account_id = str(account["account_id"])
        body: dict[str, typing.Any] = {
            "account_id": account_id,
            "name": account["name"],
        }
        if "monthly_budget_usd" in account:
            body["monthly_budget_usd"] = account["monthly_budget_usd"]
        _admin_post(base_url, token, "/admin/accounts", body)
        print(f"  계정 {account_id}")

        for team in account["teams"]:
            _admin_post(
                base_url,
                token,
                f"/admin/accounts/{account_id}/teams",
                dict(team),
            )
            print(f"    팀 {team['team_id']}")

        for user in account["users"]:
            _admin_post(
                base_url,
                token,
                f"/admin/accounts/{account_id}/users",
                dict(user),
            )
            created = _admin_post(
                base_url,
                token,
                f"/admin/accounts/{account_id}/keys",
                {
                    "user_id": user["user_id"],
                    "name": f"{user['name']} 데모 키",
                },
            )
            if "api_key" in created:
                issued.append(
                    {
                        "account_id": account_id,
                        "team_id": user.get("team_id", ""),
                        "user_id": user["user_id"],
                        "key_id": created.get("key_id", ""),
                        "api_key": created["api_key"],
                    }
                )
                print(f"    사용자 {user['user_id']} · 키 발급")
    return issued


def generate_traffic(
    base_url: str,
    keys: list[dict[str, typing.Any]],
    models: set[str],
    calls_per_key: int,
) -> tuple[int, int]:
    """발급된 키로 실제 Bedrock 호출을 넣는다.

    대시보드가 비어 있으면 구성이 맞는지 확인할 수 없다. 사용자와 모델을
    돌려가며 호출해 모든 집계 축에 값이 들어가게 한다.

    Args:
        base_url: 게이트웨이 기본 URL.
        keys: 발급된 키 목록.
        models: 사용 가능한 모델 ID 집합.
        calls_per_key: 키당 호출 수.

    Returns:
        (성공 건수, 실패 건수) 튜플.
    """
    usable = [model for model in _TRAFFIC_MODELS if model in models]
    if not usable:
        print("  사용 가능한 데모 모델이 없다. 트래픽 생성을 건너뛴다.")
        return 0, 0

    succeeded = 0
    failed = 0
    counter = 0
    for entry in keys:
        for index in range(calls_per_key):
            model = usable[counter % len(usable)]
            prompt = _TRAFFIC_PROMPTS[counter % len(_TRAFFIC_PROMPTS)]
            counter += 1
            # 마지막 호출은 스트리밍으로 보내 streamed 집계도 채운다.
            use_stream = index == calls_per_key - 1
            status, response = _request(
                base_url=base_url,
                method="POST",
                path="/v1/chat/completions",
                headers={"Authorization": f"Bearer {entry['api_key']}"},
                body={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 48,
                    "temperature": 0,
                    "stream": use_stream,
                },
            )
            if status == http.HTTPStatus.OK:
                succeeded += 1
            else:
                failed += 1
                detail = json.dumps(response, ensure_ascii=False)[:160]
                print(
                    f"  호출 실패 {entry['user_id']} / {model}"
                    f" (HTTP {status}): {detail}"
                )
    return succeeded, failed


def main() -> int:
    """엔트리 포인트.

    Returns:
        프로세스 종료 코드.
    """
    parser = argparse.ArgumentParser(description="LLM Gateway 데모 데이터 시드")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LLMGW_BASE_URL", ""),
        help="게이트웨이 기본 URL. 환경변수 LLMGW_BASE_URL 로도 지정 가능.",
    )
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("LLMGW_ADMIN_TOKEN", ""),
        help="관리 토큰. 환경변수 LLMGW_ADMIN_TOKEN 로도 지정 가능.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="발급된 평문 키를 저장할 JSON 경로. 시크릿으로 취급한다.",
    )
    parser.add_argument(
        "--calls-per-key",
        type=int,
        default=2,
        help="키당 생성할 Bedrock 호출 수 (기본 2). 0 이면 생성하지 않는다.",
    )
    args = parser.parse_args()

    if not args.base_url:
        print("--base-url 또는 LLMGW_BASE_URL 이 필요하다.", file=sys.stderr)
        return 2
    if not args.admin_token:
        print(
            "--admin-token 또는 LLMGW_ADMIN_TOKEN 이 필요하다.",
            file=sys.stderr,
        )
        return 2

    try:
        print("계정 구조 생성")
        issued = seed_structure(args.base_url, args.admin_token)

        if args.calls_per_key > 0 and issued:
            print(f"사용량 생성 (키당 {args.calls_per_key}회)")
            models = _available_models(args.base_url, args.admin_token)
            succeeded, failed = generate_traffic(
                args.base_url, issued, models, args.calls_per_key
            )
            print(f"  성공 {succeeded}건 · 실패 {failed}건")
    except SeedError as exc:
        print(f"시드 실패: {exc}", file=sys.stderr)
        return 1

    if args.output and issued:
        output_path = pathlib.Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(issued, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # 평문 키가 담긴 파일이므로 소유자만 읽게 한다.
        output_path.chmod(0o600)
        print(f"발급된 키 {len(issued)}개를 {output_path} 에 저장했다.")
        print("이 파일은 시크릿이다. 커밋하거나 공유하지 않는다.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
