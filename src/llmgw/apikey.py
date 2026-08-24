"""API 키 생성과 해시.

평문 키는 발급 응답에서 한 번만 노출되고 저장소에는 SHA-256 해시만 남는다.
저장소가 유출되어도 키를 복원할 수 없게 하는 것이 목적이다.

키에 salt 를 쓰지 않는다. 키 자체가 256비트 난수라 사전 공격 대상이
아니고, 인증 시 해시로 단일 조회(GetItem)를 해야 하기 때문이다. salt 를
쓰면 전체 키를 스캔해야 한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets

# OpenAI 클라이언트 SDK 중 일부가 `sk-` 접두어를 검증한다. 호환성을 위해
# 같은 접두어를 쓰고 뒤에 게이트웨이 식별자를 붙인다.
_KEY_PREFIX = "sk-llmgw"

# 난수 바이트 길이. token_urlsafe(32) 는 43자 URL-safe 문자열을 만든다.
_SECRET_BYTES = 32

# 화면에 노출할 접두어 길이. 키를 특정할 수 있을 만큼 짧게 유지한다.
_DISPLAY_PREFIX_LENGTH = 20


@dataclasses.dataclass(frozen=True)
class GeneratedKey:
    """새로 발급된 API 키.

    Attributes:
        plaintext: 클라이언트에게 한 번만 전달할 평문 키.
        key_hash: 저장소에 보관할 SHA-256 16진수 해시.
        key_prefix: 화면 표시용 접두어.
    """

    plaintext: str
    key_hash: str
    key_prefix: str


def hash_api_key(plaintext: str) -> str:
    """평문 API 키의 SHA-256 16진수 해시를 계산한다.

    Args:
        plaintext: 평문 API 키.

    Returns:
        64자 소문자 16진수 문자열.
    """
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def generate_api_key(env: str) -> GeneratedKey:
    """새 API 키를 만든다.

    Args:
        env: 배포 환경 식별자. 키 문자열에 포함되어 dev 키를 prod 에
            잘못 붙여 쓰는 실수를 눈으로 잡을 수 있게 한다.

    Returns:
        평문과 해시, 표시용 접두어를 담은 `GeneratedKey`.
    """
    safe_env = "".join(ch for ch in env.lower() if ch.isalnum()) or "dev"
    plaintext = (
        f"{_KEY_PREFIX}-{safe_env}-{secrets.token_urlsafe(_SECRET_BYTES)}"
    )
    return GeneratedKey(
        plaintext=plaintext,
        key_hash=hash_api_key(plaintext),
        key_prefix=plaintext[:_DISPLAY_PREFIX_LENGTH],
    )


def constant_time_equals(left: str, right: str) -> bool:
    """두 문자열을 타이밍 공격에 안전하게 비교한다.

    관리 토큰 비교에 쓴다. 문자 단위 조기 종료가 없는 `hmac.compare_digest`
    를 사용한다.

    Args:
        left: 비교 대상 1.
        right: 비교 대상 2.

    Returns:
        두 값이 같으면 `True`.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
