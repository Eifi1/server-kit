"""The account export (§6.5): one envelope, and nothing in it that looks like a secret."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eifi1_server_kit.auth import Budget
from eifi1_server_kit.user_admin import (
    EXPORT_FORMAT,
    EXPORT_PER_USER,
    EXPORT_VERSION,
    NEVER_EXPORT,
    ExportSecretError,
    assert_no_secrets,
    export_envelope,
    export_filename,
    secret_paths,
)

NOW = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)

#: What keksdose's export of a fully populated account may hold (§6.5), with every secret
#: kept OUT — the shape an app's own test checks.
CLEAN: dict[str, Any] = {
    "account": {
        "email": "ada@example.com",
        "first_name": "Ada",
        "last_name": "Example",
        "locale": "en",
        "roles": ["MEMBER"],
        "totp_enabled": True,
        "password_changed_at": "2026-09-01T10:00:00+00:00",
        "password_change_required": False,
        "passkeys": [{"name": "Phone", "created_at": "2026-09-01", "last_used_at": None}],
        "api_tokens": [{"name": "script", "scopes": ["read"], "budget_id": 4, "expires_at": "2027-01-01"}],
        "public_key_fingerprint": "ab:cd:ef",
        "key_epoch": 2,
        "invitations_sent": [{"email": "bob@example.com", "status": "open", "created_at": "2026-10-01"}],
    },
    "data": {
        "budgets_owned": [{"name": "Household", "currency": "CHF", "shares": [{"role": "guest"}]}],
        "push_registrations": [{"browser": "Firefox", "created_at": "2026-09-02", "failures": 0}],
        "feedback": [{"title": "Totals are off", "body": "The token count is wrong", "comments": []}],
        "sessions": [{"device": "Firefox on Linux", "ip": "203.0.113.4", "last_active_at": "2026-10-06"}],
    },
}


def test_the_envelope() -> None:
    envelope = export_envelope("keksdose", CLEAN["account"], CLEAN["data"], NOW)
    assert envelope == {
        "format": "eifi1-account-export",
        "version": 1,
        "app": "keksdose",
        "exported_at": "2026-10-07T09:30:00+00:00",
        "account": CLEAN["account"],
        "data": CLEAN["data"],
    }
    assert (EXPORT_FORMAT, EXPORT_VERSION) == ("eifi1-account-export", 1)
    naive = export_envelope("kastlan", {}, {}, datetime(2026, 10, 7, 9, 30), version=2)
    assert naive["exported_at"] == "2026-10-07T09:30:00+00:00" and naive["version"] == 2
    assert_no_secrets(envelope)


@pytest.mark.parametrize("app", ["", "Keksdose", "kurven schmiede", "-x", "kastlan/../x"])
def test_the_app_is_named_in_lower_case(app: str) -> None:
    with pytest.raises(ValueError, match="lower case"):
        export_envelope(app, {}, {}, NOW)
    with pytest.raises(ValueError, match="lower case"):
        export_filename(app, NOW)


def test_the_filename_and_the_throttle() -> None:
    assert export_filename("kurvenschmiede", NOW) == "kurvenschmiede-account-2026-10-07.json"
    assert Budget(1, 60) == EXPORT_PER_USER
    assert any("password hash" in rule for rule in NEVER_EXPORT) and any("E2EE" in rule for rule in NEVER_EXPORT)


def test_a_clean_export_passes() -> None:
    assert secret_paths(CLEAN) == []
    assert_no_secrets(CLEAN)


def test_every_secret_of_the_never_list_is_found() -> None:
    """§6.5's list, one leak each; containers named like secrets are walked, not judged."""
    leaky: dict[str, Any] = {
        "account": {
            "password_hash": "$2b$12$abcdefghijklmnopqrstuv",
            "totpSecret": "JBSWY3DPEHPK3PXP",
            "backup_codes": ["abcd-efgh", "ijkl-mnop"],
            "api_tokens": [{"name": "script", "token_hash": "x"}],
            "passkeys": [{"name": "Phone", "credentialId": "AAEC", "public_key": "pQECAyYg"}],
            "refresh_token": "opaque",
            "key_wraps": [{"kind": "password", "wrapped": "c2VjcmV0", "salt": "c2FsdA", "iv": "aXY"}],
            "recovery_verifier": "v",
        },
        "data": {
            "push": [{"endpoint": "https://push.example/abc", "keys": {"p256dh": "BOr", "auth": "x"}}],
            "invitations_sent": [{"email": "bob@example.com", "link": "https://app.example/register?invite=abc"}],
            "note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
            "digest": "",
        },
    }
    assert secret_paths(leaky) == [
        "account.password_hash",
        "account.totpSecret",
        "account.backup_codes[0]",
        "account.backup_codes[1]",
        "account.api_tokens[0].token_hash",
        "account.passkeys[0].credentialId",
        "account.passkeys[0].public_key",
        "account.refresh_token",
        "account.key_wraps[0].wrapped",
        "account.key_wraps[0].salt",
        "account.key_wraps[0].iv",
        "account.recovery_verifier",
        "data.push[0].endpoint",
        "data.push[0].keys.p256dh",
        "data.push[0].keys.auth",
        "data.invitations_sent[0].link",
        "data.note",
    ]
    with pytest.raises(ExportSecretError, match=r"account\.password_hash, account\.totpSecret") as raised:
        assert_no_secrets(leaky)
    assert len(raised.value.paths) == 17


def test_allow_skips_a_field_the_heuristic_flags_wrongly() -> None:
    attachment = {"attachments": [{"name": "screenshot.png", "content_hash": "a" * 64}]}
    assert secret_paths(attachment) == ["attachments[0].content_hash"]
    assert secret_paths(attachment, allow=["content_hash"]) == []
    assert secret_paths({"x": {"keys": {"auth": "a"}}}, allow={"keys"}) == []
