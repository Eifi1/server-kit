"""The account layer's ports (auth §12.3): the shapes an app's adapters fill."""

from __future__ import annotations

from datetime import UTC, datetime

from eifi1_server_kit.auth.ports import (
    ACCOUNT_FIELDS,
    OPTIONAL_ACCOUNT_FIELDS,
    NewAccount,
    NewInvitation,
    NewPasskey,
    NewSession,
)

AT = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_the_optional_members_are_writable_and_have_a_default() -> None:
    """``is_demo`` is no store write (an app sets it at its own demo), the cut-off is."""
    assert OPTIONAL_ACCOUNT_FIELDS == {"is_demo": False, "sessions_invalid_before": None}
    assert "sessions_invalid_before" in ACCOUNT_FIELDS and "is_demo" not in ACCOUNT_FIELDS
    assert {"id", "created_at"}.isdisjoint(ACCOUNT_FIELDS)


def test_the_new_rows_default_what_an_app_may_leave_out() -> None:
    account = NewAccount(
        email="a@example.com", password_hash="h", first_name="A", last_name="", locale="de-CH", role="MEMBER"
    )
    assert account.email_verified_at is None and account.device_timezone is None
    session = NewSession(sid="s", user_id=1, started_at=AT, expires_at=AT)
    assert session.shared_device is False and session.device is None
    invitation = NewInvitation(
        email="b@example.com", token_hash="t", expires_at=AT, locale="en", invited_by_user_id=None
    )
    assert invitation.role is None and invitation.scope is None and invitation.note is None
    passkey = NewPasskey(user_id=1, credential_id=b"c", public_key=b"k", sign_count=0, name="Laptop")
    assert passkey.transports is None
