"""What looks like a secret: a key's name, or a value's shape.

``docs/user-admin-harmonization.md`` §4.3 and §6.5 in ``Eifi1/ui-kit``. Two places must
never carry a secret, and both are written by app code the kit does not see: the
``detail`` of an ``admin_actions`` row (:func:`~eifi1_server_kit.user_admin.audit_detail`)
and the account export (:func:`~eifi1_server_kit.user_admin.assert_no_secrets`). This is
the one judgement both use, so the two cannot disagree.

A HEURISTIC, tuned to the fields the three apps hold — a guard that makes the common
mistake loud, not a proof. It judges a LEAF (a key with a scalar value, or a list of
scalars); a key whose value is an object or a list of objects is a container
(``api_tokens: [{name, scopes, …}]``, ``passkeys: [...]``) and is walked into instead.

* **A key looks secret** when one of :data:`SECRET_KEY_TERMS` appears in it as whole
  words — ``password_hash``, ``totpSecret``, ``backup_codes``, ``credential_id`` — or it
  IS one of :data:`SECRET_WHOLE_KEYS` (a push subscription's ``auth``). Words, not
  substrings: ``tokenizer`` is not a token.
* **Except** a key whose last word says it holds a fact about the secret rather than the
  secret (:data:`FACT_SUFFIXES`: ``password_changed_at``, ``totp_enabled``,
  ``public_key_fingerprint``, ``key_epoch``), and a value that cannot carry one: a
  boolean, ``None``, or an empty string or list.
* **A value looks secret** whatever its key when it has a secret's shape
  (:data:`SECRET_VALUE_PATTERNS`): a bcrypt, Argon2, scrypt or PBKDF2 hash, a JWT, a
  SHA-256 hex digest — the kit's token digests — or a link carrying a token.
"""

from __future__ import annotations

import re

__all__ = [
    "FACT_SUFFIXES",
    "SECRET_KEY_TERMS",
    "SECRET_VALUE_PATTERNS",
    "SECRET_WHOLE_KEYS",
    "looks_secret",
    "looks_secret_key",
    "looks_secret_value",
]

#: Words that make a key a secret's (§6.5 "never leaves"): the password and its hash,
#: TOTP secrets and backup-code digests, every token and token hash, passkey credential
#: ids and public keys, push endpoints and their keys, and the E2EE material — key wraps,
#: salts, nonces, recovery verifiers. Multi-word terms match as consecutive words.
SECRET_KEY_TERMS: tuple[str, ...] = (
    "password",
    "passwd",
    "hash",
    "digest",
    "token",
    "secret",
    "totp",
    "otp",
    "key_wrap",
    "wrap",
    "wrapped",
    "p256dh",
    "endpoint",
    "credential_id",
    "raw_id",
    "public_key",
    "private_key",
    "api_key",
    "salt",
    "nonce",
    "iv",
    "ciphertext",
    "backup_code",
    "recovery_code",
    "verifier",
    "kek",
    "dek",
)
#: Keys that are a secret only as the WHOLE key: a push subscription's
#: ``{"p256dh": …, "auth": …}``, and a bare ``key``.
SECRET_WHOLE_KEYS: frozenset[str] = frozenset({"auth", "key", "keys"})
#: A key's last word that says it holds a FACT about a secret — when, whether, how many,
#: which one — and not the secret.
FACT_SUFFIXES: frozenset[str] = frozenset(
    {
        "at",
        "on",
        "until",
        "date",
        "count",
        "enabled",
        "required",
        "fingerprint",
        "epoch",
        "kind",
        "type",
        # 0.5.1 (keksdose): `api_tokens_revoked`, `sessions_valid` are facts about a secret,
        # not the secret — keksdose had renamed them to `*_count` to get past the check.
        "revoked",
        "valid",
    }
)

#: Shapes no export and no audit detail holds legitimately.
SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\$2[abxy]?\$\d{2}\$"),  # bcrypt
    re.compile(r"^\$argon2(id|i|d)\$"),  # Argon2
    re.compile(r"^\$scrypt\$|^\$7\$"),  # scrypt
    re.compile(r"^(\$pbkdf2|pbkdf2[_:$])", re.IGNORECASE),  # PBKDF2 (passlib, Django)
    re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*$"),  # a JWT
    re.compile(r"^[0-9a-fA-F]{64}$"),  # a SHA-256 digest: the kit's one-time-token hash
    re.compile(r"[?&](token|invite|code|key)=", re.IGNORECASE),  # a link that carries one
    # 0.5.1 (keksdose): a Web Push subscription's endpoint is a capability URL — anyone
    # holding it can push to the device — and it may sit under a harmless key.
    re.compile(
        r"^https://(fcm\.googleapis\.com|updates\.push\.services\.mozilla\.com"
        r"|[a-z0-9.-]*\.push\.apple\.com|[a-z0-9.-]*\.notify\.windows\.com)/",
        re.IGNORECASE,
    ),
)

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(key: str) -> list[str]:
    """``"publicKey"`` / ``"public-key"`` / ``"PUBLIC_KEY"`` → ``["public", "key"]``."""
    return [word for word in re.split(r"[_\-\s.]+", _CAMEL.sub("_", key).lower()) if word]


def _same_word(word: str, term: str) -> bool:
    """``word`` is ``term`` or its plural (``codes``, ``hashes``)."""
    return word in (term, f"{term}s", f"{term}es")


def _contains(words: list[str], term: str) -> bool:
    parts = term.split("_")
    return any(
        all(_same_word(words[start + i], part) for i, part in enumerate(parts))
        for start in range(len(words) - len(parts) + 1)
    )


def looks_secret_key(key: str) -> bool:
    """Whether a key's NAME says it holds a secret (see the module docstring). A key that
    ends in a fact word (:data:`FACT_SUFFIXES`) does not."""
    words = _words(key)
    if not words or words[-1] in FACT_SUFFIXES:
        return False
    if "_".join(words) in SECRET_WHOLE_KEYS:
        return True
    return any(_contains(words, term) for term in SECRET_KEY_TERMS)


def looks_secret_value(value: object) -> bool:
    """Whether a string has a secret's SHAPE (:data:`SECRET_VALUE_PATTERNS`); anything
    else is ``False``."""
    return isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def looks_secret(key: str, value: object) -> bool:
    """Whether the leaf ``key: value`` looks like a secret: its value's shape, or its
    key's name unless the value cannot carry one (a boolean, ``None``, ``""``, ``[]``).

    For a list of scalars, call it per item with the list's key.
    """
    if looks_secret_value(value):
        return True
    if value is None or isinstance(value, bool) or value == "" or value == []:
        return False
    return looks_secret_key(key)
