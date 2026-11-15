"""Sealing a secret the server has to read back (0.32, keksdose's KG-1): the TOTP secret.

keksdose ``infrastructure/secret_box.py`` (live #278) is the canon. Most of an account's
secrets are one-way — a password is hashed, a mailed link's token is a digest — but a TOTP
secret mints every second-factor code, so the check needs the plaintext. In the clear, anyone
who reads the database (an export, a backup) mints valid codes for every account with 2FA.

**What this is.** AES-256-GCM under keys the app holds outside the database (its secret
manager). A sealed value is::

    enc:v1:<base64url( nonce(12) || ciphertext || tag(16) )>

The prefix answers "is this sealed?" without a key, and it is punctuated, so a base32 secret
(``[A-Z2-7]+``) never collides with it.

**The AAD binds a ciphertext to its row**: :func:`totp_secret_aad` and
:func:`totp_pending_secret_aad` name the column and the account, so a value pasted into
another account's row, or a pending secret pasted over the live one, fails the tag instead of
working. keksdose's bytes exactly, so its stored values open unchanged.

**Several keys, newest first.** The first seals, every one is tried on unseal: rotating is
"prepend the new key, deploy, drop the old one later", and restoring an old key is a
configuration change, not a data recovery.

**No plaintext fallback, ever.** :meth:`SecretBox.seal` raises :class:`SecretKeyMissingError`
without a key; it never returns its input.

:class:`SecretBox` needs the ``seal`` extra (``eifi1-server-kit[seal]``, pyca's
``cryptography``), imported when it seals or opens; the errors and the AAD need nothing. An
app with a box of its own implements :class:`~eifi1_server_kit.auth.ports.SecretSeal` and
raises these errors.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Sequence
from typing import Any

__all__ = [
    "DEFAULT_SEAL_LABEL",
    "SEALED_PREFIX",
    "SecretBox",
    "SecretKeyMissingError",
    "SecretSealError",
    "SecretUnreadableError",
    "totp_pending_secret_aad",
    "totp_secret_aad",
]

#: Marks a value as sealed. Versioned: a different construction gets its own prefix, so both
#: read during a rollout.
SEALED_PREFIX = "enc:v1:"
#: The key derivation's domain separation for a new app: the same operator string used for two
#: purposes never derives the same AES key. keksdose passes its own,
#: ``b"keksdose.secret_box.v1"``, so its stored values open.
DEFAULT_SEAL_LABEL = b"eifi1.secret_box.v1"

#: AES-GCM's standard nonce, 96 bits.
_NONCE_BYTES = 12
#: The configured keys, as one string: separated by whitespace or commas.
_KEY_SEPARATORS = re.compile(r"[\s,]+")


class SecretSealError(RuntimeError):
    """This server could not seal or open a secret it is responsible for: the operator's to
    mend, never the person's (an unreadable second factor is ``503 two_factor_unavailable``)."""


class SecretKeyMissingError(SecretSealError):
    """No key is configured: nothing is sealed in the clear instead."""


class SecretUnreadableError(SecretSealError):
    """A sealed value opened under no configured key: a key wrong or rotated out, a value
    tampered with or pasted into another row, or not a sealed value at all. One error for all,
    since each means "the operator must act"."""


def totp_secret_aad(user_id: int) -> bytes:
    """The context the account's live TOTP secret is sealed in (keksdose ``totp_aad``)."""
    return f"users.totp_secret:{user_id}".encode()


def totp_pending_secret_aad(user_id: int) -> bytes:
    """The context of a secret set up and not yet confirmed (keksdose ``totp_pending_aad``).
    Its own, so an unconfirmed secret pasted over the live one does not open there: the one way
    from pending to live is the confirmation re-sealing it."""
    return f"users.totp_pending_secret:{user_id}".encode()


def _aesgcm() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - the extra's absence, tested by test_package
        raise ImportError(
            "eifi1_server_kit.auth.seal.SecretBox needs cryptography: install the kit with its seal extra, "
            "eifi1-server-kit[seal]."
        ) from exc
    return AESGCM


class SecretBox:
    """A :class:`~eifi1_server_kit.auth.ports.SecretSeal` with AES-256-GCM (keksdose's).

    * ``keys`` — the operator's strings, newest first: a sequence, or one string separated by
      whitespace or commas (one setting, so a key holds neither). Each is hashed into a 256-bit
      key, so any format an operator pastes works (``openssl rand -hex 32``); none is allowed
      and refuses to seal.
    * ``label`` — the derivation's domain separation, :data:`DEFAULT_SEAL_LABEL`; keksdose
      passes ``b"keksdose.secret_box.v1"``.
    """

    def __init__(self, keys: Sequence[str] | str, *, label: bytes = DEFAULT_SEAL_LABEL) -> None:
        listed = _KEY_SEPARATORS.split(keys) if isinstance(keys, str) else list(keys)
        self._keys = tuple(
            hashlib.sha256(label + b"|" + material.strip().encode()).digest() for material in listed if material.strip()
        )

    @property
    def key_is_configured(self) -> bool:
        """Is there a key to seal with? Read it before offering what will have to seal."""
        return bool(self._keys)

    def seal(self, plaintext: str, *, aad: bytes) -> str:
        """Encrypt under the newest key, or raise :class:`SecretKeyMissingError`."""
        if not self._keys:
            raise SecretKeyMissingError("no key is configured: a secret is never stored in the clear")
        nonce = os.urandom(_NONCE_BYTES)
        blob = _aesgcm()(self._keys[0]).encrypt(nonce, plaintext.encode(), aad)
        return SEALED_PREFIX + base64.urlsafe_b64encode(nonce + blob).decode("ascii")

    def unseal(self, sealed: str, *, aad: bytes) -> str:
        """Open a value :meth:`seal` made, trying every key in order:
        :class:`SecretKeyMissingError` without a key, :class:`SecretUnreadableError` when none
        opens it."""
        if not self._keys:
            raise SecretKeyMissingError("no key is configured: the stored secret cannot be read")
        if not sealed.startswith(SEALED_PREFIX):
            raise SecretUnreadableError("the value is not sealed")
        try:
            raw = base64.urlsafe_b64decode(sealed[len(SEALED_PREFIX) :].encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretUnreadableError("the sealed value is not base64url") from exc
        if len(raw) <= _NONCE_BYTES:
            raise SecretUnreadableError("the sealed value is too short to hold a nonce")
        from cryptography.exceptions import InvalidTag

        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        aesgcm = _aesgcm()
        for key in self._keys:
            try:
                plain: bytes = aesgcm(key).decrypt(nonce, blob, aad)
            except InvalidTag:
                continue
            return plain.decode()
        raise SecretUnreadableError(f"no configured key opens the value ({len(self._keys)} tried)")
