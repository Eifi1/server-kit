"""One-time tokens and the session's claims (§4.4, §6.2) — keksdose ``auth_service``'s rules."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ACCESS_TOKEN_LIFETIME,
    CHALLENGE_LIFETIMES,
    INVITE_TTL,
    REFRESH_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_LIFETIME,
    RESET_TTL,
    VERIFY_TTL,
    ChallengeKind,
    MintedToken,
    access_claims,
    challenge_claims,
    hash_token,
    is_expired,
    mint,
    refresh_claims,
    token_is_revoked,
)

NOW = datetime(2026, 10, 6, 9, 30, 15, 250_000, tzinfo=UTC)


def test_the_lifetimes_are_keksdoses() -> None:
    assert (timedelta(hours=1), timedelta(hours=48), timedelta(days=14)) == (RESET_TTL, VERIFY_TTL, INVITE_TTL)
    assert (timedelta(hours=24), timedelta(days=30)) == (ACCESS_TOKEN_LIFETIME, REFRESH_TOKEN_LIFETIME)
    assert (ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_MINUTES) == (60 * 24, 60 * 24 * 30)
    assert {
        ChallengeKind.TWO_FACTOR: timedelta(minutes=5),
        ChallengeKind.PASSWORD_CHANGE: timedelta(minutes=10),
    } == CHALLENGE_LIFETIMES


def test_mint_gives_the_raw_token_once_and_its_digest() -> None:
    raw, digest = mint()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", raw)
    assert digest == hash_token(raw) == hashlib.sha256(raw.encode()).hexdigest()
    assert len(digest) == 64
    assert mint().raw != raw, "fresh every time"
    prefixed = mint("ksi_")
    assert isinstance(prefixed, MintedToken)
    assert prefixed.raw.startswith("ksi_") and len(prefixed.raw) == 47
    assert prefixed.digest == hash_token(prefixed.raw)
    for bad in ("ksi", "_", "k-s_", "kö_"):
        with pytest.raises(ValueError, match="token prefix"):
            mint(bad)


def test_expired_at_the_boundary_and_naive_is_utc() -> None:
    issued = NOW - RESET_TTL
    assert is_expired(issued, RESET_TTL, NOW), "expires_at <= now, as keksdose"
    assert not is_expired(issued + timedelta(microseconds=1), RESET_TTL, NOW)
    assert is_expired(issued.replace(tzinfo=None), RESET_TTL, NOW), "SQLite's naive datetime"
    assert not is_expired(NOW.replace(tzinfo=None), VERIFY_TTL, NOW + timedelta(hours=47))


def test_access_claims() -> None:
    claims = access_claims(7, now=NOW, extra={"role": "ADMIN"})
    assert claims == {
        "role": "ADMIN",
        "sub": "7",
        "type": "access",
        "iat": NOW.timestamp(),
        "exp": int((NOW + ACCESS_TOKEN_LIFETIME).timestamp()),
    }
    assert claims["iat"] % 1 == 0.25, "iat keeps its fraction of a second"
    short = access_claims("u-1", now=NOW.replace(tzinfo=None), lifetime=timedelta(minutes=15))
    assert short["sub"] == "u-1" and short["exp"] == int((NOW + timedelta(minutes=15)).timestamp())
    assert set(short) == {"sub", "type", "iat", "exp"}


def test_kastlans_claims_ride_along() -> None:
    claims = access_claims(3, now=NOW, extra={"company_id": 9, "roles": ["ADMIN"], "sid": "s-1"})
    assert claims["company_id"] == 9 and claims["roles"] == ["ADMIN"] and claims["sid"] == "s-1"


@pytest.mark.parametrize("name", ["sub", "type", "iat", "exp"])
def test_extra_cannot_override_what_the_builder_sets(name: str) -> None:
    """keksdose's ``payload.update(claims)`` let ``{"type": "refresh"}`` turn an access
    token into a refresh token; the kit refuses it."""
    with pytest.raises(ValueError, match="sets"):
        access_claims(1, now=NOW, extra={name: "x"})


@pytest.mark.parametrize("name", ["email", "first_name", "last_name", "display_name", "locale", "name"])
def test_an_access_token_carries_no_profile(name: str) -> None:
    """§6.2: no names, no email, no locale — the shell reads ``/auth/me``."""
    with pytest.raises(ValueError, match="no profile"):
        access_claims(1, now=NOW, extra={name: "x"})
    with pytest.raises(ValueError, match="no profile"):
        refresh_claims(1, now=NOW, extra={name: "x"})


def test_refresh_claims() -> None:
    claims = refresh_claims(7, now=NOW)
    assert claims == {
        "sub": "7",
        "type": "refresh",
        "iat": NOW.timestamp(),
        "exp": int((NOW + REFRESH_TOKEN_LIFETIME).timestamp()),
    }
    assert refresh_claims(7, now=NOW, extra={"sid": "s-1"})["sid"] == "s-1"


def test_challenge_claims() -> None:
    two_factor = challenge_claims(7, ChallengeKind.TWO_FACTOR, now=NOW)
    assert two_factor["type"] == "2fa_challenge"
    assert two_factor["exp"] == int((NOW + timedelta(minutes=5)).timestamp())
    change = challenge_claims(7, ChallengeKind.PASSWORD_CHANGE, now=NOW)
    assert change["type"] == "password_change"
    assert change["exp"] == int((NOW + timedelta(minutes=10)).timestamp())


def test_no_cut_off_revokes_nothing() -> None:
    assert not token_is_revoked(NOW.timestamp(), None)
    assert not token_is_revoked(None, None), "a demo token without iat, on an account never cut off"


def test_the_cut_off_is_exact() -> None:
    """keksdose feedback #198: a token minted after the cut-off in the SAME SECOND survives."""
    cutoff = NOW
    minted_after = refresh_claims(1, now=cutoff + timedelta(microseconds=1))
    minted_at = access_claims(1, now=cutoff)
    minted_before = access_claims(1, now=cutoff - timedelta(microseconds=1))
    assert int(minted_after["iat"]) == int(cutoff.timestamp()), "same second"
    assert not token_is_revoked(minted_after["iat"], cutoff)
    assert token_is_revoked(minted_at["iat"], cutoff)
    assert token_is_revoked(minted_before["iat"], cutoff)
    assert token_is_revoked(minted_at["iat"], cutoff.replace(tzinfo=None)), "a naive cut-off is UTC"
    later = NOW + timedelta(hours=1)
    assert not token_is_revoked(int(later.timestamp()), cutoff), "an integer iat"
    assert not token_is_revoked(str(later.timestamp()), cutoff), "a numeric string, as float() reads it"


@pytest.mark.parametrize("iat", [None, "soon", True, [1], {"t": 1}, float("nan"), float("inf"), 1e20])
def test_revocation_fails_closed_on_an_unreadable_iat(iat: Any) -> None:
    assert token_is_revoked(iat, NOW)
