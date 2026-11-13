"""The account layer's :class:`~eifi1_server_kit.auth.ports.TokenSigner`, ready-made: HS256 with
PyJWT, the ``jwt`` extra (``eifi1-server-kit[jwt]``, 0.30).

The kit builds and checks the claims (:mod:`~eifi1_server_kit.auth.tokens`) and left the
signing to each app, which kept its JWT library behind the port. Every app wrote the same
class over PyJWT (Staunlabor's ``JwtSigner``, the first adopter's, is this one's model), and
each copy had to remember the same three rules, any one of which, forgotten, lets a forged
token in:

* **one algorithm, pinned on verify** (:data:`JWT_ALGORITHM`): ``algorithms=["HS256"]``, so a
  token whose header names another (``none``, ``HS512``, an asymmetric one a confused
  library would check with the secret as a public key) is refused, never tried;
* **``exp`` required**: every token the kit mints ends, so one without an end was not this
  app's. :meth:`JwtSigner.sign` refuses claims without it, too;
* **one answer for every fault**: a bad signature, an expired token, garbage, another
  algorithm are all ``None``, which the kit answers as ``session_ended`` or ``token_invalid``;
  never an exception, which would be a 500.

**What this does not do.** It keeps no key ring and no rotation (one secret, as every app
has), reads no ``aud`` or ``iss`` (the kit sets none, so a token carrying an ``aud`` is
refused), allows no leeway, and does not judge the secret beyond refusing an empty one: how
long and how random is the boot check's, :func:`~eifi1_server_kit.settings.secret_problems`,
fatal or a warning as the app decides (PyJWT warns, too, below 32 bytes).

Imported only by an app that asks for it; the ports, the service and the claims need no JWT
library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

try:
    import jwt
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.jwt_signer needs PyJWT: install the kit with its jwt extra, eifi1-server-kit[jwt]."
    ) from exc

__all__ = ["JWT_ALGORITHM", "JwtSigner"]

#: The one algorithm a token is signed with and the only one :meth:`JwtSigner.verify` accepts.
JWT_ALGORITHM = "HS256"


class JwtSigner:
    """:class:`~eifi1_server_kit.auth.ports.TokenSigner` over PyJWT, HS256 with the app's
    secret::

        signer = JwtSigner(settings.jwt_secret)  # a str or a pydantic SecretStr

    An empty secret is a :class:`ValueError` here, rather than a 500 at the first sign-in.
    The secret is kept private and never in the ``repr``."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str | SecretStr) -> None:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if not value.strip():
            raise ValueError("JwtSigner needs a secret to sign with; it is empty")
        self._secret = value

    def sign(self, claims: Mapping[str, Any]) -> str:
        """The signed token for these claims. Claims without ``exp`` are a
        :class:`ValueError`: :meth:`verify` would refuse the token they make."""
        if "exp" not in claims:
            raise ValueError("a token the kit signs ends: the claims need an exp")
        return jwt.encode(dict(claims), self._secret, algorithm=JWT_ALGORITHM)

    def verify(self, token: str) -> Mapping[str, Any] | None:
        """The claims of a token this signer signed, under :data:`JWT_ALGORITHM`, that names
        an ``exp`` not yet past; ``None`` for anything else."""
        try:
            return jwt.decode(token, self._secret, algorithms=[JWT_ALGORITHM], options={"require": ["exp"]})
        except jwt.PyJWTError:
            # A bad signature, another algorithm, an expired token, no token at all: one answer.
            return None
