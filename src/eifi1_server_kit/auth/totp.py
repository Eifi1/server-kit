"""The second factor's code: which time step a TOTP code matches, and whether that step may
still be accepted — RFC 6238 §5.2, a code is accepted ONCE.

``docs/auth-harmonization.md`` §5.1 (the 2FA step, ``POST /auth/login/2fa``) in
``Eifi1/ui-kit``. keksdose is the canon: its audit of 2026-10-30 (AU-10) found that one code
— or one 2FA challenge plus its code — signed in again for as long as the window lasted,
about 90 seconds, because nothing remembered the step a code had been accepted for. kastlan's ``pyotp.TOTP(secret).verify(code, valid_window=1)`` did the
same (the kastlan reuse audit of 2026-11-02, proposal 9). The fix, keksdose
``domain/services/auth_service.py`` ``_matched_step`` / ``verify_totp`` and kastlan
``domain/services/two_factor_service.py`` ``_matched_step`` / ``_totp_ok`` since, is two
rules, which this module holds once:

* :func:`totp_matched_step` — the time step a code belongs to, one step either side of now
  (a phone clock a little off still signs in), or ``None``. **The latest match wins**, so a
  code that also matches a spent step still counts for the newer one. Every step in the
  window is computed and compared in constant time (:func:`hmac.compare_digest`), so the
  answer's timing says nothing about which step, if any, matched;
* :func:`totp_step_accepted` — the replay rule: only a step AFTER the last accepted one.

The app keeps the last accepted step on the account (keksdose and kastlan
``users.totp_last_step``, an integer, ``NULL`` until a code is accepted), stamps the matched
step there when it accepts a code, and clears it with the secret — a new secret has accepted
nothing::

    matched = totp_matched_step(secret, payload.code, now=datetime.now(UTC))
    if not totp_step_accepted(matched, user.totp_last_step):
        return False  # wrong, malformed, or already spent: one answer
    user.totp_last_step = matched  # in the transaction that accepts the code
    return True

**Every path that accepts a code spends it**: the 2FA sign-in, the setup's first code and
the disable alike, so the first sign-in after enrolling waits for the next code. A code
whose step is spent is refused like a wrong one — the caller gets the same answer — and an
app may count it apart (keksdose's ``replayed_code``: ``matched is not None`` and not
accepted). Two redemptions racing each other must not both read the old step: lock the
row (``SELECT … FOR UPDATE``, re-read) before the check, as kastlan's ``lock_challenge``
does.

**One challenge, one sign-in** (0.21, keksdose's AU-10 again). The 2FA challenge is a
stateless JWT that stands in for the password for five minutes, so the code alone cannot
make it single-use: whoever held a redeemed challenge could redeem it again with the next
code. It carries the account's last accepted step as it stood at minting
(:data:`TOTP_STEP_CLAIM`, :func:`two_factor_challenge_claims`), and every accepted code
moves that step, so after one redemption the copy no longer matches
(:func:`two_factor_challenge_is_spent`)::

    token = jwt.encode(two_factor_challenge_claims(user.id, last_step=user.totp_last_step, now=now), …)
    ...
    claims = decode(payload.challenge_token)  # type 2fa_challenge, unexpired
    if two_factor_challenge_is_spent(claims, user.totp_last_step):  # the row locked, re-read
        raise AuthError(AuthErrorCode.TOKEN_INVALID)

No ``pyotp``: the standard library's :mod:`hmac` computes the code — HOTP (RFC 4226) over
the time step, HMAC-SHA1, six digits, thirty-second steps, what every authenticator app
reads from an ``otpauth://totp/…`` URI without parameters (kastlan's
``digits=6&period=30``). The secret is base32, as the URI carries it, with or without its
``=`` padding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from eifi1_server_kit.auth.tokens import ChallengeKind, challenge_claims, hash_token

__all__ = [
    "BACKUP_CODE_ALPHABET",
    "BACKUP_CODE_COUNT",
    "TOTP_DIGITS",
    "TOTP_STEP_CLAIM",
    "TOTP_STEP_SECONDS",
    "TOTP_WINDOW",
    "backup_code_digest",
    "backup_code_spent",
    "new_backup_codes",
    "new_totp_secret",
    "totp_code",
    "totp_matched_step",
    "totp_step_accepted",
    "totp_uri",
    "two_factor_challenge_claims",
    "two_factor_challenge_is_spent",
]

#: A code's digits (RFC 6238's default, every authenticator app's).
TOTP_DIGITS = 6
#: A time step's length in seconds (RFC 6238 §4.1's ``X``, pyotp's ``interval``).
TOTP_STEP_SECONDS = 30
#: Steps accepted either side of now (keksdose and kastlan: one, ``valid_window=1``), so a
#: code stays valid for about 90 seconds and a phone a little off still signs in.
TOTP_WINDOW = 1

#: The 2FA challenge's copy of the account's last accepted step, as it stood at minting
#: (keksdose ``infrastructure/security.py:74-77``, kastlan ``domain/services/tokens.py:29-32``).
TOTP_STEP_CLAIM = "totp_step"

_CODE = re.compile(rf"^[0-9]{{{TOTP_DIGITS}}}$")


def _key(secret: str) -> bytes:
    """The secret's bytes: base32, without case, its ``=`` padding optional (pyotp's
    ``byte_secret``). A secret that is empty or not base32 is the app's bug, never the
    caller's input, so it raises — the check fails closed, it never accepts."""
    cleaned = secret.strip().rstrip("=").upper()
    if not cleaned:
        raise ValueError("a TOTP secret is required")
    try:
        return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8), casefold=True)
    except binascii.Error as exc:
        raise ValueError("the TOTP secret is not base32") from exc


def _code(key: bytes, step: int) -> str:
    """HOTP (RFC 4226 §5.3) at counter ``step``: HMAC-SHA1, dynamic truncation, six digits."""
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFF_FFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def _timestamp(now: datetime | float) -> float:
    if isinstance(now, datetime):
        return (now if now.tzinfo is not None else now.replace(tzinfo=UTC)).timestamp()
    return float(now)


#: A new secret's bytes: 160 bits, RFC 4226 §4's recommended length and pyotp's
#: ``random_base32()`` (32 base32 characters, no padding).
_SECRET_BYTES = 20


def new_totp_secret() -> str:
    """A fresh TOTP secret, base32 without padding (0.32, keksdose ``begin_totp_setup``'s
    ``pyotp.random_base32()``): 32 characters from :mod:`secrets`."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii")


def totp_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://totp/…`` URI an authenticator app reads from a QR code (0.32; pyotp's
    ``provisioning_uri(name=account, issuer_name=issuer)``, byte for byte): the label
    ``issuer:account``, the secret and the issuer, nothing else — SHA-1, six digits and thirty
    seconds are every app's defaults."""
    label = f"{quote(issuer)}:{quote(account)}"
    query = urlencode({"secret": secret, "issuer": issuer}).replace("+", "%20")
    return f"otpauth://totp/{label}?{query}"


#: Backup codes in a set (0.32): made when two-factor is switched on, and again on request.
BACKUP_CODE_COUNT = 10
#: What a backup code is made of: letters and digits without I, O, 0 and 1, which read alike.
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#: A backup code's letters and digits: 16 of 32, 80 bits — kastlan's and ui-kit's
#: `SignInForm` length, printed in groups of four.
_BACKUP_CODE_LENGTH = 16
_BACKUP_SEPARATORS = re.compile(r"[\s\-‐-―]")


def new_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    """A fresh set of backup codes (0.32), each ``ABCD-EFGH-JKLM-NPQR`` from
    :data:`BACKUP_CODE_ALPHABET` through :mod:`secrets`: shown to the person once, kept as
    :func:`backup_code_digest` only."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(_BACKUP_CODE_LENGTH))
        codes.append("-".join(raw[index : index + 4] for index in range(0, _BACKUP_CODE_LENGTH, 4)))
    return codes


def backup_code_digest(code: str) -> str | None:
    """A backup code's digest as stored (:func:`~eifi1_server_kit.auth.hash_token` of the code
    in upper case, without spaces or dashes, NFKC-normalised as typed), or ``None`` for
    anything that is not one's shape — a six-digit TOTP code among them."""
    typed = _BACKUP_SEPARATORS.sub("", unicodedata.normalize("NFKC", code)).upper()
    if len(typed) != _BACKUP_CODE_LENGTH or any(char not in BACKUP_CODE_ALPHABET for char in typed):
        return None
    return hash_token(typed)


def backup_code_spent(code: str, digests: Sequence[str]) -> tuple[str, ...] | None:
    """The digests left once ``code`` is spent, or ``None`` when it matches none of
    ``digests`` (0.32): a backup code works once. Every digest is compared in constant time,
    whatever matched."""
    digest = backup_code_digest(code)
    if digest is None:
        return None
    found: str | None = None
    for stored in digests:
        if hmac.compare_digest(stored.encode(), digest.encode()):
            found = stored
    if found is None:
        return None
    left = list(digests)
    left.remove(found)
    return tuple(left)


def totp_code(secret: str, *, now: datetime | float) -> str:
    """The code ``secret`` shows at ``now`` (0.32): what an authenticator app displays, for a
    test or a tool. A server never needs it — it checks with :func:`totp_matched_step`."""
    return _code(_key(secret), int(_timestamp(now) // TOTP_STEP_SECONDS))


def totp_matched_step(secret: str, code: str, *, now: datetime | float, window: int = TOTP_WINDOW) -> int | None:
    """The time step ``code`` matches under ``secret`` — ``window`` steps either side of
    ``now`` — or ``None`` (keksdose and kastlan ``_matched_step``).

    * ``secret`` — the account's TOTP secret, unsealed, base32 (an empty or non-base32 one
      raises :class:`ValueError`: the app's bug, and the check must not pass);
    * ``code`` — as typed: surrounding whitespace is dropped and the text NFKC-normalised
      (pyotp's ``strings_equal``: full-width digits read as digits); anything but exactly
      six digits then is ``None``, as is a backup code;
    * ``now`` — an aware ``datetime`` (a naive one is read as UTC) or a POSIX timestamp, as
      ``time.time()`` gives: the app's clock seam, so a test steps to the next 30 seconds
      instead of sleeping through them;
    * ``window`` — steps either side, :data:`TOTP_WINDOW` (1); ``0`` is the current step only.

    **The latest match wins**: a code that matches two steps (one in a million) counts for
    the newer, so a spent older step does not refuse it. Every step in the window is
    computed and compared with :func:`hmac.compare_digest`, whatever matched. Pass the
    answer to :func:`totp_step_accepted` with the account's last accepted step.
    """
    if window < 0:
        raise ValueError("window counts steps either side of now: 0 or more")
    key = _key(secret)
    typed = unicodedata.normalize("NFKC", code).strip()
    if not _CODE.match(typed):
        return None
    current = int(_timestamp(now) // TOTP_STEP_SECONDS)
    matched: int | None = None
    for step in range(current - window, current + window + 1):
        if step >= 0 and hmac.compare_digest(_code(key, step).encode(), typed.encode()):
            matched = step  # ascending, so the latest match is the last one kept
    return matched


def totp_step_accepted(matched: int | None, last_step: int | None) -> bool:
    """May a code of step ``matched`` be accepted, after ``last_step``? (RFC 6238 §5.2,
    keksdose ``verify_totp``.)

    Only a step AFTER the last accepted one: ``None`` (no match) never, a step at or before
    ``last_step`` never — the same code again, or an older one still inside the window —
    and anything when no code was accepted yet (``last_step`` ``None``). On ``True`` the
    app stamps ``matched`` as the account's new last step, in the same transaction.
    """
    if matched is None:
        return False
    return last_step is None or matched > last_step


def two_factor_challenge_claims(
    sub: int | str, *, last_step: int | None, now: datetime, shared_device: bool = False
) -> dict[str, Any]:
    """The 2FA challenge's payload: :func:`~eifi1_server_kit.auth.challenge_claims` of
    :attr:`~eifi1_server_kit.auth.ChallengeKind.TWO_FACTOR` (five minutes), with the
    account's last accepted step under :data:`TOTP_STEP_CLAIM` — ``None`` when it has
    accepted none (keksdose ``create_2fa_challenge_token``, kastlan's the same).

    ``shared_device`` (0.27): the sign-in form's "This is a shared computer", which
    ``/login/2fa`` reads back with :func:`~eifi1_server_kit.auth.shared_device_of` to
    start the session it asked for."""
    return challenge_claims(
        sub, ChallengeKind.TWO_FACTOR, now=now, extra={TOTP_STEP_CLAIM: last_step}, shared_device=shared_device
    )


def two_factor_challenge_is_spent(claims: Mapping[str, Any], last_step: int | None) -> bool:
    """Has a code been accepted since this 2FA challenge was minted? (keksdose
    ``auth_service.two_factor_challenge_is_spent``, kastlan
    ``TwoFactorService.lock_challenge``.)

    ``claims`` is the decoded challenge, already checked for its type and expiry;
    ``last_step`` the account's ``totp_last_step`` NOW — read with the row locked, so two
    redemptions racing each other cannot both see the old step. Spent when its copy
    differs: every accepted code moves the step, so after one redemption no copy matches. A
    challenge without the claim (minted before it existed) reads as ``None``, which matches
    an account that has accepted no code since. Anything but an integer or ``None`` there is
    spent. A spent challenge is refused like an expired one (``token_invalid``).
    """
    step = claims.get(TOTP_STEP_CLAIM)
    if step is not None and (isinstance(step, bool) or not isinstance(step, int)):
        return True
    return step != last_step
