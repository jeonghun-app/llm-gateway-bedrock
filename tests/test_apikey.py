"""API 키 생성과 해시 테스트."""

from __future__ import annotations

from llmgw import apikey


def test_generate_api_key_접두어에환경이포함된다() -> None:
    # Arrange / Act
    generated = apikey.generate_api_key("prod")

    # Assert
    assert generated.plaintext.startswith(
        "sk-llmgw-prod-"
    ), f"실제 값: {generated.plaintext[:20]}"


def test_generate_api_key_평문은저장되지않도록해시가함께나온다() -> None:
    # Arrange / Act
    generated = apikey.generate_api_key("dev")

    # Assert
    assert generated.key_hash == apikey.hash_api_key(generated.plaintext)
    assert len(generated.key_hash) == 64
    assert generated.plaintext not in generated.key_hash


def test_generate_api_key_매번다른키를만든다() -> None:
    # Arrange / Act
    first = apikey.generate_api_key("dev")
    second = apikey.generate_api_key("dev")

    # Assert
    assert first.plaintext != second.plaintext
    assert first.key_hash != second.key_hash


def test_generate_api_key_표시용접두어는평문의앞부분이다() -> None:
    # Arrange / Act
    generated = apikey.generate_api_key("dev")

    # Assert
    assert generated.plaintext.startswith(generated.key_prefix)
    assert len(generated.key_prefix) == 20


def test_generate_api_key_환경명의특수문자를제거한다() -> None:
    # Arrange / Act
    generated = apikey.generate_api_key("STG-2/x")

    # Assert
    assert generated.plaintext.startswith(
        "sk-llmgw-stg2x-"
    ), f"실제 값: {generated.plaintext[:22]}"


def test_generate_api_key_빈환경명은dev로대체한다() -> None:
    # Arrange / Act
    generated = apikey.generate_api_key("///")

    # Assert
    assert generated.plaintext.startswith("sk-llmgw-dev-")


def test_hash_api_key_앞뒤공백을무시한다() -> None:
    # Arrange
    key = "sk-llmgw-dev-abc"

    # Act / Assert
    assert apikey.hash_api_key(key) == apikey.hash_api_key(f"  {key}  ")


def test_hash_api_key_같은입력은같은해시() -> None:
    # Arrange / Act / Assert
    assert apikey.hash_api_key("abc") == apikey.hash_api_key("abc")


def test_hash_api_key_다른입력은다른해시() -> None:
    # Arrange / Act / Assert
    assert apikey.hash_api_key("abc") != apikey.hash_api_key("abd")


def test_constant_time_equals_같은값_True() -> None:
    # Arrange / Act / Assert
    assert apikey.constant_time_equals("token", "token") is True


def test_constant_time_equals_다른값_False() -> None:
    # Arrange / Act / Assert
    assert apikey.constant_time_equals("token", "TOKEN") is False


def test_constant_time_equals_길이가달라도예외없이False() -> None:
    # Arrange / Act / Assert
    assert apikey.constant_time_equals("a", "aaaaaaaa") is False


def test_constant_time_equals_빈문자열끼리는True() -> None:
    # Arrange / Act / Assert
    assert apikey.constant_time_equals("", "") is True
