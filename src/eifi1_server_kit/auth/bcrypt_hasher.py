"""The account layer's :class:`~eifi1_server_kit.auth.ports.PasswordHasher`, ready-made: bcrypt
at the app's cost, the ``bcrypt`` extra (``eifi1-server-kit[bcrypt]``, 0.30).

The kit hashes no password itself (:mod:`~eifi1_server_kit.auth.passwords`); each app kept its
hasher behind the port, and all of them use pyca's bcrypt. Staunlabor's ``BcryptHasher``, the
first adopter's, is this one's model. Two inputs turned into 500s in copies that forgot
them, and here are no match instead:

* **a password past 72 bytes** (:data:`~eifi1_server_kit.auth.schemas.MAX_PASSWORD_BYTES`):
  bcrypt 5 raises, and bcrypt 4 compared the first 72 bytes only, so a longer password
  sharing them matched. :meth:`BcryptHasher.verify` answers ``False`` without hashing, on
  both (as :func:`~eifi1_server_kit.auth.absent_account_check` expects of it), and
  :meth:`BcryptHasher.hash` refuses one rather than store a hash of its first 72 bytes. The
  kit's request shapes refuse one before either is asked;
* **a hash that is no bcrypt hash** (empty, cut short, another scheme's, carried over from
  an older table): ``False``.

The cost is the app's setting, 12 by default (bcrypt's own, and every app's in
production); a test suite passes 4. A hash carries its cost, so rows hashed at one cost
verify under another. **What this does not do**: rehash on sign-in when the cost changed,
pepper, or migrate hashes of another scheme.

Imported only by an app that asks for it.
"""

from __future__ import annotations

try:
    import bcrypt
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.bcrypt_hasher needs bcrypt: install the kit with its bcrypt extra, "
        "eifi1-server-kit[bcrypt]."
    ) from exc

from eifi1_server_kit.auth.schemas import MAX_PASSWORD_BYTES

__all__ = ["DEFAULT_BCRYPT_ROUNDS", "MAX_BCRYPT_ROUNDS", "MIN_BCRYPT_ROUNDS", "BcryptHasher"]

#: bcrypt's own default cost, and the apps' in production: 2¹² rounds, about a quarter of a
#: second per hash.
DEFAULT_BCRYPT_ROUNDS = 12
#: The cheapest cost bcrypt takes: a test suite's.
MIN_BCRYPT_ROUNDS = 4
#: The dearest cost bcrypt takes.
MAX_BCRYPT_ROUNDS = 31


class BcryptHasher:
    """:class:`~eifi1_server_kit.auth.ports.PasswordHasher` over bcrypt::

        hasher = BcryptHasher(settings.bcrypt_rounds)

    A cost outside :data:`MIN_BCRYPT_ROUNDS`–:data:`MAX_BCRYPT_ROUNDS` is a :class:`ValueError`
    here, rather than at the first sign-up."""

    __slots__ = ("_rounds",)

    def __init__(self, rounds: int = DEFAULT_BCRYPT_ROUNDS) -> None:
        if (
            isinstance(rounds, bool)
            or not isinstance(rounds, int)
            or not MIN_BCRYPT_ROUNDS <= rounds <= MAX_BCRYPT_ROUNDS
        ):
            raise ValueError(f"bcrypt takes {MIN_BCRYPT_ROUNDS} to {MAX_BCRYPT_ROUNDS} rounds, not {rounds!r}")
        self._rounds = rounds

    @property
    def rounds(self) -> int:
        """The cost a new hash is made at."""
        return self._rounds

    def hash(self, password: str) -> str:
        """A new hash of the password, with a new salt. A password past
        :data:`~eifi1_server_kit.auth.schemas.MAX_PASSWORD_BYTES` is a :class:`ValueError`:
        bcrypt would hash only its first 72 bytes (or raise, from 5.0)."""
        encoded = password.encode()
        if len(encoded) > MAX_PASSWORD_BYTES:
            raise ValueError(f"bcrypt hashes at most {MAX_PASSWORD_BYTES} bytes of a password")
        return bcrypt.hashpw(encoded, bcrypt.gensalt(self._rounds)).decode("ascii")

    def verify(self, password: str, password_hash: str) -> bool:
        """Whether the password matches the hash. ``False``, without hashing, for a password
        past 72 bytes; ``False`` for a malformed hash. Never raises for either."""
        try:
            encoded = password.encode()
            if len(encoded) > MAX_PASSWORD_BYTES:
                return False
            return bcrypt.checkpw(encoded, password_hash.encode())
        except ValueError:
            # bcrypt's "Invalid salt" for a hash that is none, and a password with no UTF-8
            # (a lone surrogate): no match, never a 500.
            return False
