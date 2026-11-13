"""The ready ``PasswordHasher`` (0.30, the ``bcrypt`` extra): bcrypt at the app's cost, and no
match, never an exception, for a password past 72 bytes or a hash that is no bcrypt hash."""

from __future__ import annotations

import importlib
import sys

import bcrypt
import pytest

from eifi1_server_kit.auth import MAX_PASSWORD_BYTES, absent_account_check
from eifi1_server_kit.auth.bcrypt_hasher import DEFAULT_BCRYPT_ROUNDS, BcryptHasher
from eifi1_server_kit.auth.ports import PasswordHasher

#: bcrypt's cheapest cost, so the suite stays fast; a hash carries its cost.
ROUNDS = 4


def test_it_is_the_port_and_a_hash_verifies_its_password_only() -> None:
    hasher: PasswordHasher = BcryptHasher(ROUNDS)
    hashed = hasher.hash("correct horse battery staple")
    assert hashed.startswith("$2b$04$")
    assert hasher.verify("correct horse battery staple", hashed) is True
    assert hasher.verify("correct horse battery stapler", hashed) is False
    assert hasher.hash("correct horse battery staple") != hashed  # a new salt each time


def test_the_cost_is_bcrypt_s_own_default_unless_the_app_says_otherwise() -> None:
    assert BcryptHasher().rounds == DEFAULT_BCRYPT_ROUNDS == 12
    assert BcryptHasher(ROUNDS).rounds == ROUNDS


def test_a_hash_made_at_another_cost_still_verifies() -> None:
    """A hash carries its cost: rows hashed at 12 verify under a suite that hashes at 4."""
    other = bcrypt.hashpw(b"secret", bcrypt.gensalt(5)).decode()
    assert BcryptHasher(ROUNDS).verify("secret", other) is True


@pytest.mark.parametrize("rounds", [3, 32, 0, -1, True, 12.0])
def test_a_cost_bcrypt_has_not_is_refused_at_construction(rounds: object) -> None:
    with pytest.raises(ValueError, match="rounds"):
        BcryptHasher(rounds)  # type: ignore[arg-type]


def test_a_password_past_72_bytes_is_no_match_never_its_first_72_bytes() -> None:
    """bcrypt 4 compares the first 72 bytes only, and bcrypt 5 raises: the first would let a
    longer password in, the second answered a 500 in the pilot. Neither: no match."""
    hasher = BcryptHasher(ROUNDS)
    first = "a" * MAX_PASSWORD_BYTES
    hashed = hasher.hash(first)
    assert hasher.verify(first, hashed) is True
    assert hasher.verify(first + "b", hashed) is False
    # Bytes, not characters: 36 × "é" is 72 bytes, 37 × is 74.
    assert hasher.verify("é" * 36, hasher.hash("é" * 36)) is True
    assert hasher.verify("é" * 37, hashed) is False


def test_a_password_past_72_bytes_is_not_hashed() -> None:
    with pytest.raises(ValueError, match="72 bytes"):
        BcryptHasher(ROUNDS).hash("a" * (MAX_PASSWORD_BYTES + 1))


@pytest.mark.parametrize(
    "hashed",
    ["", "not a hash", "$2b$04$short", "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", "€", "$2b$99$" + "a" * 53],
)
def test_a_malformed_hash_is_no_match_never_an_exception(hashed: str) -> None:
    assert BcryptHasher(ROUNDS).verify("secret", hashed) is False


def test_a_password_that_is_no_text_is_no_match() -> None:
    """A lone surrogate has no UTF-8: no bytes to compare, so no match."""
    hasher = BcryptHasher(ROUNDS)
    assert hasher.verify("\ud800", hasher.hash("secret")) is False


def test_it_drives_the_absent_account_check() -> None:
    hasher = BcryptHasher(ROUNDS)
    check = absent_account_check(hasher.hash, hasher.verify)
    assert check("anything") is False
    assert check("a" * (MAX_PASSWORD_BYTES + 1)) is False


def test_the_module_names_its_extra_without_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import bcrypt`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.auth.bcrypt_hasher", raising=False)
    monkeypatch.setitem(sys.modules, "bcrypt", None)
    with pytest.raises(ImportError, match=r"install the kit with its bcrypt extra, eifi1-server-kit\[bcrypt\]"):
        importlib.import_module("eifi1_server_kit.auth.bcrypt_hasher")
