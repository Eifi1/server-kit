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
does. The 2FA challenge token carries the step as it stood at minting (``totp_step``), so
one challenge completes one sign-in; that claim stays the app's.

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
import unicodedata
from datetime import UTC, datetime

__all__ = [
    "TOTP_DIGITS",
    "TOTP_STEP_SECONDS",
    "TOTP_WINDOW",
    "totp_matched_step",
    "totp_step_accepted",
]

#: A code's digits (RFC 6238's default, every authenticator app's).
TOTP_DIGITS = 6
#: A time step's length in seconds (RFC 6238 §4.1's ``X``, pyotp's ``interval``).
TOTP_STEP_SECONDS = 30
#: Steps accepted either side of now (keksdose and kastlan: one, ``valid_window=1``), so a
#: code stays valid for about 90 seconds and a phone a little off still signs in.
TOTP_WINDOW = 1

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
