"""The one-click unsubscribe token (§3.6, decision 8).

A notification mail carries RFC 8058's ``List-Unsubscribe`` and a footer link, both with a
token that names one account and one type and can do one thing: turn that type's e-mail
off. So it is **signed, not stored, and never expires** — the link in a year-old mail still
works, and a copy in a request log costs little. It is not a one-time token
(:class:`~eifi1_server_kit.auth.OneTimeTokenKind`)::

    v1.<data>.<mac>
    data = base64url("<user_id>:<type id>")              # no padding
    mac  = base64url(HMAC-SHA256(key, "v1." + data))
    key  = HMAC-SHA256(<the app's signing secret>, "eifi1-notifications-unsubscribe")

**The key is derived from the secret each app already signs with** (Marcel, 2026-10-13),
so nothing new is provisioned, and the derivation keeps it apart from every other use of
that secret. Rotating the secret ends old links; the page then says to use Settings. ``v1``
leaves room for another key.

The routes, ``POST /notifications/unsubscribe/{token}`` and ``…/{token}/all``, answer no
account and need no cookie: a valid token is a 200 also when the switch is already off or
the account is gone, a bad one :class:`~.errors.UnsubscribeTokenError` (400). **A GET never
unsubscribes**: link scanners fetch every URL. The page reads the type from ``<data>`` on
the client and never checks the MAC; that is the server's.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from dataclasses import dataclass

from eifi1_server_kit.notifications.errors import UnsubscribeTokenError
from eifi1_server_kit.notifications.registry import _is_type_id

__all__ = ["UnsubscribeClaim", "read_unsubscribe_token", "unsubscribe_key", "unsubscribe_token"]

_KEY_CONTEXT = b"eifi1-notifications-unsubscribe"
_VERSION = "v1"
#: The longest user id a token carries: an integer or a UUID fits many times over.
_USER_ID_MAX_LENGTH = 64
#: ``v1.<data>.<mac>``: unpadded base64url, the MAC a SHA-256 digest's 43 characters. The
#: data is at most 64 + 1 + 40 bytes, 140 characters encoded.
_TOKEN = re.compile(r"^v1\.([A-Za-z0-9_-]{1,140})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class UnsubscribeClaim:
    """What a valid token names: the account (as text — the app reads its own id type)
    and the type whose e-mail to turn off."""

    user_id: str
    type_id: str


def _secret_bytes(secret: str | bytes) -> bytes:
    raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not raw:
        raise ValueError("the unsubscribe key is derived from the app's signing secret, which is never empty")
    return raw


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unsubscribe_key(secret: str | bytes) -> bytes:
    """The token's key: HMAC-SHA256 of ``eifi1-notifications-unsubscribe`` under the app's
    signing secret (its JWT secret). 32 bytes."""
    return hmac.new(_secret_bytes(secret), _KEY_CONTEXT, hashlib.sha256).digest()


def _mac(key: bytes, data: str) -> str:
    return _b64(hmac.new(key, f"{_VERSION}.{data}".encode("ascii"), hashlib.sha256).digest())


def unsubscribe_token(secret: str | bytes, user_id: int | str, type_id: str) -> str:
    """The token for one account and one type: ``v1.<data>.<mac>``, URL-safe as it is.

    A :class:`ValueError` for an empty or overlong user id, or a type id outside
    :data:`~.registry.TYPE_ID_PATTERN`.
    """
    user = str(user_id)
    if not user or len(user) > _USER_ID_MAX_LENGTH or not user.isprintable():
        raise ValueError(f"an unsubscribe token names a user id of 1–{_USER_ID_MAX_LENGTH} characters")
    if not _is_type_id(type_id):
        raise ValueError(f"an unsubscribe token names a notification type id: {type_id!r}")
    data = _b64(f"{user}:{type_id}".encode())
    return f"{_VERSION}.{data}.{_mac(unsubscribe_key(secret), data)}"


def read_unsubscribe_token(secret: str | bytes, token: str) -> UnsubscribeClaim:
    """The account and type a token names, after its MAC is checked in constant time.

    Anything else is an :class:`~.errors.UnsubscribeTokenError` (400): another version, a
    changed ``<data>`` or ``<mac>``, base64 padding, a token signed with another secret,
    or a claim that is no ``<user id>:<type id>``.
    """
    key = unsubscribe_key(secret)
    found = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if found is None:
        raise UnsubscribeTokenError()
    data, mac = found.groups()
    if not hmac.compare_digest(_mac(key, data).encode("ascii"), mac.encode("ascii")):
        raise UnsubscribeTokenError()
    try:
        claim = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
    except binascii.Error, UnicodeDecodeError:
        # Only reachable with a valid MAC over bytes this module never signs.
        raise UnsubscribeTokenError() from None
    user, _, type_id = claim.rpartition(":")
    if not user or len(user) > _USER_ID_MAX_LENGTH or not user.isprintable() or not _is_type_id(type_id):
        raise UnsubscribeTokenError()
    return UnsubscribeClaim(user_id=user, type_id=type_id)
