"""The one-click unsubscribe token (notifications §3.6): signed, not stored, never expiring."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from eifi1_server_kit.notifications import (
    UnsubscribeClaim,
    UnsubscribeTokenError,
    read_unsubscribe_token,
    unsubscribe_key,
    unsubscribe_token,
)

SECRET = "jwt-secret-of-the-app-example"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _signed(data: str, secret: str = SECRET) -> str:
    """A token over any ``data``, signed the module's way: what a forger with the key could make."""
    mac = hmac.new(unsubscribe_key(secret), f"v1.{data}".encode(), hashlib.sha256).digest()
    return f"v1.{data}.{_b64(mac)}"


def test_the_key_is_derived_from_the_apps_secret() -> None:
    key = unsubscribe_key(SECRET)
    assert key == hmac.new(SECRET.encode(), b"eifi1-notifications-unsubscribe", hashlib.sha256).digest()
    assert len(key) == 32 and key != SECRET.encode()
    assert unsubscribe_key(SECRET.encode()) == key, "str or bytes"
    assert unsubscribe_key("another secret") != key
    with pytest.raises(ValueError, match="never empty"):
        unsubscribe_key("")
    with pytest.raises(ValueError, match="never empty"):
        unsubscribe_key(b"")


@pytest.mark.parametrize("user_id", [42, "42", "0b6c3a4e-1f2d-4c5b-9a8e-7d6c5b4a3f2e"])
def test_the_round_trip(user_id: int | str) -> None:
    token = unsubscribe_token(SECRET, user_id, "share_granted")
    version, data, mac = token.split(".")
    assert version == "v1" and len(mac) == 43
    assert base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode() == f"{user_id}:share_granted"
    assert all(char.isalnum() or char in "._-" for char in token), "URL-safe as it is"
    assert read_unsubscribe_token(SECRET, token) == UnsubscribeClaim(user_id=str(user_id), type_id="share_granted")


def test_the_token_holds_no_time_so_it_never_expires() -> None:
    first = unsubscribe_token(SECRET, 42, "feedback_answered")
    assert unsubscribe_token(SECRET, 42, "feedback_answered") == first
    assert unsubscribe_token(SECRET, 42, "team_added") != first
    assert unsubscribe_token(SECRET, 43, "feedback_answered") != first


def test_a_user_id_with_a_colon_still_reads_back() -> None:
    """The type id never holds a colon, so the claim splits at the last one."""
    token = unsubscribe_token(SECRET, "tenant:42", "digest")
    assert read_unsubscribe_token(SECRET, token) == UnsubscribeClaim(user_id="tenant:42", type_id="digest")


@pytest.mark.parametrize(
    ("user_id", "type_id"),
    [("", "digest"), ("x" * 65, "digest"), ("4\n2", "digest"), (42, "Digest"), (42, "share-granted"), (42, "")],
)
def test_minting_refuses_what_it_cannot_name(user_id: int | str, type_id: str) -> None:
    with pytest.raises(ValueError, match="unsubscribe token names"):
        unsubscribe_token(SECRET, user_id, type_id)


def _tampered() -> list[str]:
    token = unsubscribe_token(SECRET, 42, "share_granted")
    _, data, mac = token.split(".")
    other_data = _b64(b"43:share_granted")
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    return [
        f"v1.{other_data}.{mac}",  # another account, the old MAC
        f"v1.{_b64(b'42:team_added')}.{mac}",  # another type
        f"v1.{data}.{flipped}",  # the MAC changed
        f"v2.{data}.{mac}",  # another version
        f"V1.{data}.{mac}",
        f"v1.{data}=.{mac}",  # padding
        f"v1.{data}.{mac}=",
        f"v1.{data}.{mac[:-1]}",  # cut
        f"v1.{data}",
        f"v1.{data}.{mac}.x",
        f"{data}.{mac}",
        unsubscribe_token("another secret", 42, "share_granted"),
        "",
        " " + token,
        token + "\n",
        f"v1.{data}é.{mac}",  # outside base64url
        "v1." + "A" * 200 + "." + mac,
    ]


@pytest.mark.parametrize("token", _tampered())
def test_any_tampering_is_refused(token: str) -> None:
    with pytest.raises(UnsubscribeTokenError):
        read_unsubscribe_token(SECRET, token)


def test_a_token_that_is_no_string_is_refused() -> None:
    with pytest.raises(UnsubscribeTokenError):
        read_unsubscribe_token(SECRET, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "claim",
    [b"42", b":share_granted", b"42:", b"42:Share", b"\xff\xfe:digest", b"4\x002:digest", b"x" * 65 + b":digest"],
)
def test_a_signed_claim_that_is_no_account_and_type_is_refused(claim: bytes) -> None:
    """Only a holder of the key could sign these; the reader still refuses them."""
    with pytest.raises(UnsubscribeTokenError):
        read_unsubscribe_token(SECRET, _signed(_b64(claim)))


def test_signed_data_that_is_no_base64_is_refused() -> None:
    with pytest.raises(UnsubscribeTokenError):
        read_unsubscribe_token(SECRET, _signed("A"))  # one character more than a multiple of 4


def test_the_refusal_is_a_400_with_its_code() -> None:
    error = UnsubscribeTokenError()
    assert isinstance(error, ValueError)
    assert (error.status_code, error.code, str(error)) == (
        400,
        "unsubscribe_invalid",
        "This unsubscribe link is not valid",
    )
