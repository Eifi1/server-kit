"""The address with no account costs what one with an account costs (auth §5.2, KS S3),
with a hasher written out here: the kit depends on none."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from eifi1_server_kit.auth import ABSENT_ACCOUNT_SECRET_BYTES, MAX_PASSWORD_BYTES, absent_account_check


@dataclass
class _Hasher:
    """An app's two functions — keksdose's shape: the cost read when hashing, an over-long
    password refused without hashing — counting what each does."""

    rounds: int = 12
    hashed: list[str] = field(default_factory=list)
    checked: list[tuple[str, str]] = field(default_factory=list)

    def hash_password(self, password: str) -> str:
        self.hashed.append(password)
        return f"{self.rounds}${hashlib.sha256(password.encode()).hexdigest()}"

    def verify_password(self, password: str, hashed: str) -> bool:
        if len(password.encode()) > MAX_PASSWORD_BYTES:
            return False  # no work: bcrypt refuses it
        self.checked.append((password, hashed))
        return hashed == self.hash_password(password)


def test_it_does_a_real_checks_work_and_answers_false() -> None:
    app = _Hasher()
    check_absent = absent_account_check(app.hash_password, app.verify_password)
    assert app.hashed == []  # lazily: nothing at build time
    assert check_absent("hunter22") is False
    (secret,) = app.hashed[:1]
    assert len(secret) >= ABSENT_ACCOUNT_SECRET_BYTES  # token_urlsafe(32): 43 characters
    assert app.checked == [("hunter22", app.hash_password(secret))]
    # Not even the dummy's own secret passes.
    assert check_absent(secret) is False


def test_the_dummy_is_random_and_built_once() -> None:
    app, other = _Hasher(), _Hasher()
    check_absent = absent_account_check(app.hash_password, app.verify_password)
    check_other = absent_account_check(other.hash_password, other.verify_password)
    for _ in range(3):
        check_absent("hunter22")
    check_other("hunter22")
    assert len(app.hashed) == 1 + 3  # one dummy, then the fake verify's own hash per check
    assert app.checked[0][1] == app.checked[2][1]  # the same dummy every time
    assert app.checked[0][1] != other.checked[0][1]  # a random secret, never a constant


def test_the_dummy_follows_the_apps_cost() -> None:
    """Kurvenschmiede's rule: a test that lowers the cost lowers the dummy's with it."""
    app = _Hasher()
    check_absent = absent_account_check(app.hash_password, app.verify_password, cost=lambda: app.rounds)
    check_absent("hunter22")
    assert app.checked[-1][1].startswith("12$")
    app.rounds = 4
    check_absent("hunter22")
    assert app.checked[-1][1].startswith("4$")
    dummies = {hashed for _, hashed in app.checked}
    check_absent("hunter22")
    assert {hashed for _, hashed in app.checked} == dummies  # unchanged cost: no new dummy


def test_what_the_app_refuses_without_hashing_is_refused_here_without_hashing() -> None:
    """A short-circuit on one path only is the same oracle in a narrower doorway."""
    app = _Hasher()
    check_absent = absent_account_check(app.hash_password, app.verify_password)
    assert check_absent("ü" * MAX_PASSWORD_BYTES) is False
    assert app.checked == []
