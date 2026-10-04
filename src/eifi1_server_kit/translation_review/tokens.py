"""Review tokens: the short-lived sign-in the kit's showcase reviews the kit's wording with.

The FORMAT and the policy are shared — lifted from keksdose
``backend/keksdose/domain/services/translation_review_token_service.py`` and
``domain/models/translation_review_token.py`` — the PREFIX is each app's (keksdose's is
``kdr_``, :data:`KEKSDOSE_REVIEW_TOKEN_PREFIX`):

* ``<prefix>`` + ``secrets.token_urlsafe(32)`` — 43 URL-safe characters;
* only the SHA-256 hex digest is stored, like an API token; the raw value exists once, in
  the mint response;
* at most :data:`MAX_LIVE_REVIEW_TOKENS` live per reviewer — a new one past the cap
  retires the oldest, so a mint loop cannot fill the table;
* :data:`DEFAULT_REVIEW_TOKEN_HOURS` long (a setting, 1–72), because it is handed to
  another site in a URL fragment;
* dead once expired, revoked, or minted before the account's
  ``sessions_invalid_before`` (a password reset ends it like it ends a JWT).

The table, the queries and the account checks (active, still a reviewer of the kit) stay
app-side; :func:`review_token_is_live` is the pure part of keksdose's ``authenticate``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

#: keksdose's prefix — what tells its review token apart in the Bearer slot from a JWT
#: and from its API tokens (``kdp_``).
KEKSDOSE_REVIEW_TOKEN_PREFIX = "kdr_"
#: Entropy of the random part; ``token_urlsafe(32)`` is 43 characters.
REVIEW_TOKEN_BYTES = 32
REVIEW_TOKEN_RANDOM_LENGTH = 43
#: Live tokens per reviewer (keksdose ``MAX_LIVE_TOKENS``).
MAX_LIVE_REVIEW_TOKENS = 5
#: keksdose's ``translation_review_token_hours`` default, and its bounds.
DEFAULT_REVIEW_TOKEN_HOURS = 12
MIN_REVIEW_TOKEN_HOURS = 1
MAX_REVIEW_TOKEN_HOURS = 72

_PREFIX_RE = re.compile(r"^[A-Za-z0-9]+_$")


def _check_prefix(prefix: str) -> str:
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f"a review token prefix is letters/digits ending in '_': {prefix!r}")
    return prefix


def new_review_token(prefix: str) -> str:
    """A fresh raw token: ``prefix`` + 43 URL-safe characters. Show it once; store its hash."""
    return _check_prefix(prefix) + secrets.token_urlsafe(REVIEW_TOKEN_BYTES)


def hash_review_token(raw: str) -> str:
    """The SHA-256 hex digest the table stores (64 characters; keksdose ``_hash``)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def looks_like_review_token(raw: str, prefix: str) -> bool:
    """Does the Bearer value carry the review-token prefix? (keksdose ``looks_like_review_token``)."""
    return raw.startswith(prefix)


def review_token_pattern(prefix: str) -> re.Pattern[str]:
    """The exact shape of a raw token with ``prefix``."""
    return re.compile(rf"^{re.escape(_check_prefix(prefix))}[A-Za-z0-9_-]{{{REVIEW_TOKEN_RANDOM_LENGTH}}}$")


def is_well_formed_review_token(raw: str, prefix: str) -> bool:
    """``prefix`` + exactly 43 URL-safe characters."""
    return review_token_pattern(prefix).match(raw) is not None


def review_token_expiry(now: datetime, hours: int = DEFAULT_REVIEW_TOKEN_HOURS) -> datetime:
    """When a token minted at ``now`` stops working; ``hours`` within 1–72."""
    if not MIN_REVIEW_TOKEN_HOURS <= hours <= MAX_REVIEW_TOKEN_HOURS:
        raise ValueError(f"a review token lasts {MIN_REVIEW_TOKEN_HOURS}–{MAX_REVIEW_TOKEN_HOURS} hours, not {hours}")
    return now + timedelta(hours=hours)


def tokens_to_retire[T](live_oldest_first: Sequence[T], cap: int = MAX_LIVE_REVIEW_TOKENS) -> list[T]:
    """The live tokens to revoke before minting one more, so at most ``cap`` stay live
    (keksdose ``mint``: ``live[: max(0, len(live) - MAX_LIVE_TOKENS + 1)]``)."""
    return list(live_oldest_first[: max(0, len(live_oldest_first) - cap + 1)])


def _aware(moment: datetime) -> datetime:
    """A naive datetime read as UTC — what SQLite hands back (keksdose ``models/base.as_aware``)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def review_token_is_live(
    *,
    now: datetime,
    expires_at: datetime,
    revoked_at: datetime | None,
    created_at: datetime,
    sessions_invalid_before: datetime | None,
) -> bool:
    """The token row's own half of keksdose's ``authenticate`` (``:105-123``): not revoked,
    not expired, and not minted before the account's sessions were last invalidated.

    The account's half — it exists, it is active, its grant still covers the kit
    (:func:`eifi1_server_kit.translation_review.can_review_kit`) — is the app's.
    """
    if revoked_at is not None or _aware(expires_at) <= _aware(now):
        return False
    return sessions_invalid_before is None or _aware(created_at) >= _aware(sessions_invalid_before)
