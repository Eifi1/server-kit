"""What looks like a secret (§4.3, §6.5): one judgement for the audit detail and the export."""

from __future__ import annotations

import pytest

from eifi1_server_kit.user_admin import looks_secret, looks_secret_key, looks_secret_value


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("password", True),
        ("passwords", True),
        ("PASSWORD_HASH", True),
        ("passwordHash", True),
        ("password-hash", True),
        ("hashes", True),
        ("totp", True),
        ("credential_id", True),
        ("credential", False),
        ("publicKey", True),
        ("public_key_fingerprint", False),
        ("key", True),
        ("keys", True),
        ("auth", True),
        ("auth_method", False),
        ("key_epoch", False),
        ("password_changed_at", False),
        ("totp_enabled", False),
        ("token_type", False),
        ("tokenizer", False),
        ("api_tokens", True),  # as a LEAF; as a list of objects it is walked instead
        ("email", False),
        ("", False),
    ],
)
def test_looks_secret_key(key: str, secret: bool) -> None:
    assert looks_secret_key(key) is secret


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("$2b$12$abcdefghijklmnopqrstuv", True),
        ("$argon2id$v=19$m=65536", True),
        ("$scrypt$ln=16", True),
        ("pbkdf2_sha256$600000$salt$hash", True),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", True),
        ("0123456789abcdef" * 4, True),
        ("https://app.example/reset?token=abc", True),
        ("https://app.example/register?lang=en&invite=abc", True),
        ("https://app.example/settings?tab=security", False),
        ("0123456789abcdef" * 3, False),
        ("Ada Example", False),
        (42, False),
    ],
)
def test_looks_secret_value(value: object, secret: bool) -> None:
    assert looks_secret_value(value) is secret


def test_a_flag_or_an_empty_value_never_looks_secret() -> None:
    harmless: tuple[object, ...] = (True, False, None, "", [])
    for value in harmless:
        assert not looks_secret("password_hash", value)
    assert looks_secret("password_hash", 0)
    assert looks_secret("note", "$2b$12$abcdefghijklmnop"), "the shape wins over a harmless key"
