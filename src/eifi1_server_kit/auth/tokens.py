"""Tokens: the one-time links in a mail, and the claims of a session's JWTs.

``docs/auth-harmonization.md`` §4.4, §6.2 and §8 in ``Eifi1/ui-kit``.

**One-time tokens** — password reset, email verification, invitation. The recipe is
keksdose's (``password_reset_service.py`` ``issue_token`` / ``hash_token``), the same as
the review tokens' (:mod:`eifi1_server_kit.translation_review.tokens`):

* ``secrets.token_urlsafe(32)`` — 256 bits, 43 URL-safe characters — behind an optional
  prefix;
* only the SHA-256 hex digest is stored. No salt and no work factor, on purpose: the
  input is 256 random bits, not a human's secret, so there is nothing to grind, and the
  digest doubles as the indexed lookup key. Whoever reads the table cannot use a link;
* SINGLE USE and one live link per purpose and account: the app deletes the row when it
  is redeemed, and deletes the account's earlier rows when it mints a new one (keksdose's
  "one live link per account"). "Resend" mints a new token (§4.4);
* a lifetime per purpose: :data:`RESET_TTL` 1 h (the link is a credential, and the user
  is at their inbox waiting), :data:`VERIFY_TTL` 48 h (keksdose's
  ``email_verification_ttl_hours``), :data:`INVITE_TTL` 14 days (keksdose's budget-share
  invite, ``budget_share_service.INVITE_TTL``).

keksdose stored its verification token in plaintext; the kit stores every one-time token
hashed. The table, the delete-on-redeem and the address check stay in the app.

**Session claims** — the payload of the access and refresh JWTs. The kit does NOT sign:
each app keeps its JWT library (PyJWT in all three) and its secret, and hands these dicts
to ``jwt.encode``. What is shared is the shape (§6.2):

* the access token carries what the server needs to authorise — ``sub``, ``type``,
  ``iat``, ``exp`` (kastlan also ``company_id``, ``roles`` and a ``sid``) — and **no
  names, no email, no locale**: the shell reads those from ``/auth/me``, because a token
  is proof of who, not a profile. :func:`access_claims` refuses them;
* ``iat`` keeps its FRACTION of a second, which :func:`token_is_revoked` depends on;
* every request checks revocation (:func:`token_is_revoked` against
  ``sessions_invalid_before``), so ending sessions takes effect at once, whatever the
  lifetime. The lifetimes are per-app settings defaulting to
  :data:`ACCESS_TOKEN_LIFETIME` (24 h) and :data:`REFRESH_TOKEN_LIFETIME` (30 days).
"""

from __future__ import annotations

import enum
import hashlib
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

#: A password-reset link (keksdose ``password_reset_service.TOKEN_TTL``).
RESET_TTL = timedelta(hours=1)
#: An email-verification link (keksdose ``settings.email_verification_ttl_hours = 48``).
VERIFY_TTL = timedelta(hours=48)
#: An invitation (keksdose ``budget_share_service.INVITE_TTL``, §4.4).
INVITE_TTL = timedelta(days=14)

#: Entropy of a one-time token's random part; ``token_urlsafe(32)`` is 43 characters.
ONE_TIME_TOKEN_BYTES = 32

_PREFIX_RE = re.compile(r"^[A-Za-z0-9]+_$")


class MintedToken(NamedTuple):
    """A fresh one-time token: ``raw`` goes into the mail, once; ``digest`` into the table."""

    raw: str
    digest: str


def hash_token(raw: str) -> str:
    """The SHA-256 hex digest a one-time token is stored and looked up by (64 characters;
    keksdose ``password_reset_service.hash_token``)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(prefix: str = "") -> MintedToken:
    """A fresh one-time token and its digest: ``raw, digest = mint()``.

    ``prefix`` is optional — letters or digits ending in ``_`` — for an app that wants to
    tell its tokens apart where they are pasted; keksdose's links carry none.
    """
    if prefix and not _PREFIX_RE.match(prefix):
        raise ValueError(f"a token prefix is letters/digits ending in '_': {prefix!r}")
    raw = prefix + secrets.token_urlsafe(ONE_TIME_TOKEN_BYTES)
    return MintedToken(raw, hash_token(raw))


def _aware(moment: datetime) -> datetime:
    """A naive datetime read as UTC — what SQLite hands back (keksdose ``models/base.as_aware``)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def is_expired(issued_at: datetime, ttl: timedelta, now: datetime) -> bool:
    """Has a token issued at ``issued_at`` with lifetime ``ttl`` run out at ``now``?

    Expired AT the boundary, as keksdose's ``expires_at <= utcnow()``. Naive datetimes
    are read as UTC.
    """
    return _aware(issued_at) + ttl <= _aware(now)


#: The session's two lifetimes, the defaults of each app's settings (§6.2, §10.3).
#: keksdose and Kurvenschmiede run these; kastlan keeps 15 minutes of access until its
#: per-request check exists.
ACCESS_TOKEN_LIFETIME = timedelta(hours=24)
REFRESH_TOKEN_LIFETIME = timedelta(days=30)
#: The same, in the minutes the apps' settings hold (``access_token_expire_minutes``).
ACCESS_TOKEN_EXPIRE_MINUTES = int(ACCESS_TOKEN_LIFETIME.total_seconds() // 60)
REFRESH_TOKEN_EXPIRE_MINUTES = int(REFRESH_TOKEN_LIFETIME.total_seconds() // 60)

#: The ``type`` claim of each JWT; every consumer discriminates on it, so a refresh
#: token is never accepted as an access token, nor a challenge as either.
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


class ChallengeKind(enum.StrEnum):
    """The ``type`` of a sign-in challenge token (keksdose ``infrastructure/security.py``)."""

    #: ``{requires_2fa, challenge_token}`` → ``POST /auth/login/2fa``.
    TWO_FACTOR = "2fa_challenge"
    #: ``{requires_password_change, challenge_token}`` → ``POST /auth/login/set-password``.
    PASSWORD_CHANGE = "password_change"


#: How long a challenge lasts (keksdose): five minutes to copy a code out of an app that
#: is already open; ten to invent, type and confirm a password.
CHALLENGE_LIFETIMES: Mapping[ChallengeKind, timedelta] = {
    ChallengeKind.TWO_FACTOR: timedelta(minutes=5),
    ChallengeKind.PASSWORD_CHANGE: timedelta(minutes=10),
}

#: The claims a builder sets itself; ``extra`` may not override them.
RESERVED_CLAIMS: frozenset[str] = frozenset({"sub", "type", "iat", "exp"})
#: What an access token must not carry (§6.2): the profile is read from ``/auth/me``.
PROFILE_CLAIMS: frozenset[str] = frozenset(
    {"email", "name", "first_name", "last_name", "display_name", "given_name", "family_name", "locale"}
)


def _claims(
    sub: int | str,
    *,
    token_type: str,
    now: datetime,
    lifetime: timedelta,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    extra = dict(extra or {})
    if clash := sorted(RESERVED_CLAIMS & extra.keys()):
        raise ValueError(f"the builder sets {', '.join(clash)} itself")
    if profile := sorted(PROFILE_CLAIMS & extra.keys()):
        raise ValueError(f"a session token carries no profile (§6.2): {', '.join(profile)}")
    issued = _aware(now)
    return {
        **extra,
        # PyJWT ≥ 2.10 refuses a non-string ``sub`` (RFC 7519 §4.1.2: a StringOrURI).
        "sub": str(sub),
        "type": token_type,
        "iat": issued.timestamp(),
        "exp": int((issued + lifetime).timestamp()),
    }


def access_claims(
    sub: int | str,
    *,
    now: datetime,
    lifetime: timedelta = ACCESS_TOKEN_LIFETIME,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The payload of an access token (keksdose ``auth_service.issue_access_token``).

    ``{sub, type: "access", iat, exp, **extra}``. ``iat`` is ``now`` with its fraction of
    a second; ``exp`` is whole seconds. ``extra`` is what the app needs to authorise —
    keksdose's ``role``, kastlan's ``company_id``, ``roles`` and ``sid`` — and may not set
    the four claims above, nor a name, an email or a locale (:data:`PROFILE_CLAIMS`).
    """
    return _claims(sub, token_type=ACCESS_TOKEN_TYPE, now=now, lifetime=lifetime, extra=extra)


def refresh_claims(
    sub: int | str,
    *,
    now: datetime,
    lifetime: timedelta = REFRESH_TOKEN_LIFETIME,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The payload of a refresh token (keksdose ``auth_service.issue_refresh_token``).

    ``{sub, type: "refresh", iat, exp, **extra}``. The ``iat`` is what makes a stateless
    session revocable at all: a reset or a password change stamps
    ``sessions_invalid_before``, and :func:`token_is_revoked` compares the two. It keeps
    its fraction of a second because with whole seconds a user who resets and signs
    straight back in gets a token stamped in the cut-off's second, and the check throws
    the brand-new session away; rounding the other way lets an old session survive by up
    to a second.
    """
    return _claims(sub, token_type=REFRESH_TOKEN_TYPE, now=now, lifetime=lifetime, extra=extra)


def challenge_claims(sub: int | str, kind: ChallengeKind, *, now: datetime) -> dict[str, Any]:
    """The payload of a sign-in challenge token, at its kind's lifetime (keksdose
    ``create_2fa_challenge_token`` / ``create_password_change_challenge_token``).

    Single-purpose through its ``type``: ``get_current_user`` and ``/auth/refresh`` refuse
    it, and each challenge endpoint accepts only its own kind.
    """
    return _claims(sub, token_type=kind.value, now=now, lifetime=CHALLENGE_LIFETIMES[kind], extra=None)


def token_is_revoked(claims_iat: object, sessions_invalid_before: datetime | None) -> bool:
    """Was this token — access or refresh — minted at or before the account's cut-off?
    (keksdose ``auth_service.token_is_revoked``, Kurvenschmiede's line for line.)

    Call it with ``claims.get("iat")`` on EVERY request (``get_current_user``) and on
    refresh: ending only the refresh chain leaves a tab with an access token signed in
    for the rest of its lifetime.

    * No cut-off: never revoked.
    * The comparison is exact, to the microsecond: a token minted after the cut-off in
      the same second survives — the user who resets and signs straight back in — and
      one minted at the cut-off itself does not.
    * Fails CLOSED on a token with no ``iat``, or one that is not a number: only an
      account that has had a cut-off gets this far, so "I cannot tell when this was
      issued" has to mean "before". The cost is one extra sign-in for a stale tab.

    Not a query: the caller has loaded the user to authenticate at all, and the cut-off
    is a column on it. A naive cut-off (SQLite) is read as UTC.
    """
    if sessions_invalid_before is None:
        return False
    if isinstance(claims_iat, bool) or not isinstance(claims_iat, int | float | str):
        return True
    try:
        issued = datetime.fromtimestamp(float(claims_iat), UTC)
    except ValueError, OverflowError, OSError:
        return True
    return issued <= _aware(sessions_invalid_before)
