"""The ready ``TokenSigner`` (0.30, the ``jwt`` extra): HS256 with PyJWT, the algorithm pinned,
``exp`` required, and None for every token it did not sign or that has ended."""

from __future__ import annotations

import base64
import importlib
import json
import sys
import time
from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from eifi1_server_kit.auth.jwt_signer import JWT_ALGORITHM, JwtSigner
from eifi1_server_kit.auth.ports import TokenSigner
from eifi1_server_kit.auth.tokens import ChallengeKind, access_claims, challenge_claims, refresh_claims
from eifi1_server_kit.clock import utc_now

#: 64 bytes, so even the HS512 forgery below is signed with a key PyJWT does not warn about.
SECRET = "a-test-signing-key-long-enough-for-hs512-sixty-four-bytes-in-all"


def _ahead(seconds: int = 60) -> int:
    return int(time.time()) + seconds


def _segment(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def test_it_is_the_port_and_signs_hs256() -> None:
    signer: TokenSigner = JwtSigner(SECRET)
    token = signer.sign({"sub": "7", "exp": _ahead()})
    assert jwt.get_unverified_header(token)["alg"] == JWT_ALGORITHM == "HS256"
    assert signer.verify(token) == {"sub": "7", "exp": jwt.decode(token, options={"verify_signature": False})["exp"]}


def test_the_kit_s_own_claims_come_back_as_they_went_in() -> None:
    """``iat`` keeps its fraction of a second, which the cut-off depends on."""
    signer = JwtSigner(SECRET)
    now = utc_now()
    for claims in (
        access_claims(7, now=now, extra={"sid": "s1"}),
        refresh_claims(7, now=now, extra={"sid": "s1"}),
        challenge_claims(7, ChallengeKind.PASSWORD_CHANGE, now=now, shared_device=True),
    ):
        assert signer.verify(signer.sign(claims)) == claims


def test_a_secret_may_come_as_a_secret_str_and_never_shows() -> None:
    signer = JwtSigner(SecretStr(SECRET))
    assert JwtSigner(SECRET).verify(signer.sign({"exp": _ahead()})) is not None
    assert SECRET not in repr(signer)


@pytest.mark.parametrize("secret", ["", "   ", SecretStr("")])
def test_an_empty_secret_is_refused_at_construction(secret: str | SecretStr) -> None:
    with pytest.raises(ValueError, match="secret"):
        JwtSigner(secret)


def test_claims_without_an_end_are_not_signed() -> None:
    with pytest.raises(ValueError, match="exp"):
        JwtSigner(SECRET).sign({"sub": "7"})


def test_an_expired_token_is_none() -> None:
    signer = JwtSigner(SECRET)
    assert signer.verify(signer.sign({"sub": "7", "exp": _ahead(-1)})) is None


def test_a_token_without_an_end_is_none_even_when_signed_with_the_secret() -> None:
    assert JwtSigner(SECRET).verify(jwt.encode({"sub": "7"}, SECRET, algorithm="HS256")) is None


def test_another_key_or_a_changed_payload_is_none() -> None:
    signer = JwtSigner(SECRET)
    other = JwtSigner("another-signing-key-of-forty-two-characters")
    assert signer.verify(other.sign({"sub": "7", "exp": _ahead()})) is None
    header, _payload, signature = signer.sign({"sub": "7", "exp": _ahead()}).split(".")
    forged = ".".join((header, _segment({"sub": "1", "exp": _ahead()}), signature))
    assert signer.verify(forged) is None


def test_another_algorithm_is_refused_even_with_the_right_key() -> None:
    """The algorithm is pinned on verify: a token that names another is never tried."""
    signer = JwtSigner(SECRET)
    for algorithm in ("HS384", "HS512"):
        assert signer.verify(jwt.encode({"sub": "7", "exp": _ahead()}, SECRET, algorithm=algorithm)) is None


def test_an_unsigned_token_is_refused() -> None:
    """``alg: none``, the classic forgery: no signature at all."""
    unsigned = f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment({'sub': '1', 'exp': _ahead()})}."
    assert JwtSigner(SECRET).verify(unsigned) is None


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "..", "Bearer x"])
def test_anything_that_is_no_token_is_none(token: str) -> None:
    assert JwtSigner(SECRET).verify(token) is None


def test_the_module_names_its_extra_without_pyjwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import jwt`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.auth.jwt_signer", raising=False)
    monkeypatch.setitem(sys.modules, "jwt", None)
    with pytest.raises(ImportError, match=r"install the kit with its jwt extra, eifi1-server-kit\[jwt\]"):
        importlib.import_module("eifi1_server_kit.auth.jwt_signer")
