"""The signed-out routes' budgets (§5.2): hard per IP, only a delay per address."""

from __future__ import annotations

import pytest

from eifi1_server_kit.auth import (
    CHALLENGE_PER_IP,
    CHALLENGE_PER_SUBJECT,
    LOGIN_FAILURE_MAX_DELAY,
    LOGIN_PER_IP,
    REGISTER_PER_IP,
    RESET_PER_ADDRESS,
    RESET_PER_IP,
    VERIFICATION_RESEND_PER_RECIPIENT,
    AuthLimiters,
    Budget,
    LoginFailureThrottle,
    login_failure_delay,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_budgets_are_keksdoses_and_kastlans() -> None:
    assert Budget(30, 300) == LOGIN_PER_IP
    assert (Budget(20, 300), Budget(10, 900)) == (CHALLENGE_PER_IP, CHALLENGE_PER_SUBJECT)
    assert Budget(5, 300) == REGISTER_PER_IP
    assert (Budget(10, 3600), Budget(3, 3600)) == (RESET_PER_IP, RESET_PER_ADDRESS), "kastlan's"
    assert Budget(10, 3600) == VERIFICATION_RESEND_PER_RECIPIENT


def test_the_delay_doubles_after_the_free_failures_and_stops_at_the_cap() -> None:
    assert [login_failure_delay(n) for n in range(12)] == [0, 0, 0, 0, 0, 1, 2, 4, 8, 16, 30, 30]
    assert login_failure_delay(10_000) == LOGIN_FAILURE_MAX_DELAY, "no overflow on a long attack"
    assert login_failure_delay(1, free=0, base=0.5, cap=3) == 1.0
    assert login_failure_delay(9, free=0, base=0.5, cap=3) == 3.0


def test_failures_slow_the_address_down_and_never_lock_it_out() -> None:
    """§5.2 / §10.10: a lock per address would be a denial-of-service lever, so the owner
    always gets an answer — at worst after the cap."""
    clock = _Clock()
    throttle = LoginFailureThrottle(clock=clock)
    assert throttle.delay("ada@example.com") == 0
    for _ in range(5):
        throttle.record_failure("ada@example.com")
    assert throttle.delay("ada@example.com") == 1.0
    for _ in range(100):
        throttle.record_failure("Ada@Example.com ")  # the same address, normalised
    assert throttle.delay(" ADA@example.com") == throttle.max_delay == 30.0
    assert throttle.delay("bob@example.com") == 0, "addresses are independent"
    clock.t += 901
    assert throttle.delay("ada@example.com") == 0, "failures age out of the window"


def test_a_successful_sign_in_clears_the_failures() -> None:
    throttle = LoginFailureThrottle(clock=_Clock())
    for _ in range(8):
        throttle.record_failure("ada@example.com")
    assert throttle.delay("ada@example.com") == 8.0
    throttle.clear("ADA@example.com")
    assert throttle.delay("ada@example.com") == 0
    for _ in range(8):
        throttle.record_failure("ada@example.com")
    throttle.reset()
    assert throttle.delay("ada@example.com") == 0


def test_the_throttle_remembers_no_more_than_the_cap_needs() -> None:
    throttle = LoginFailureThrottle(free=0, base_delay=2.0, max_delay=2.0, clock=_Clock())
    assert throttle.delay("ada@example.com") == 2.0, "free=0: every attempt waits"
    throttle.record_failure("ada@example.com")
    throttle.record_failure("ada@example.com")
    assert throttle._failures.max_hits == 1
    assert LoginFailureThrottle(clock=_Clock())._failures.max_hits == 10


@pytest.mark.parametrize(
    ("free", "base", "cap"),
    [(-1, 1.0, 30.0), (5, 0.0, 30.0), (5, 2.0, 1.0)],
)
def test_the_throttle_refuses_a_nonsense_curve(free: int, base: float, cap: float) -> None:
    with pytest.raises(ValueError, match="base_delay"):
        LoginFailureThrottle(free=free, base_delay=base, max_delay=cap)


def test_auth_limiters_are_per_instance_with_the_budgets() -> None:
    clock = _Clock()
    first, second = AuthLimiters(clock=clock), AuthLimiters(clock=clock)
    budgets = {
        "login": LOGIN_PER_IP,
        "challenge": CHALLENGE_PER_IP,
        "challenge_subject": CHALLENGE_PER_SUBJECT,
        "register": REGISTER_PER_IP,
        "reset_ip": RESET_PER_IP,
        "reset_address": RESET_PER_ADDRESS,
        "verification_resend": VERIFICATION_RESEND_PER_RECIPIENT,
    }
    for name, budget in budgets.items():
        limiter = getattr(first, name)
        assert (limiter.max_hits, limiter.window_seconds) == (budget.max_hits, budget.window_seconds), name
        for _ in range(budget.max_hits):
            assert limiter.hit("k") is None, name
        assert limiter.hit("k") is not None, name
        assert getattr(second, name).hit("k") is None, f"{name}: a second app starts empty"
    first.login_failures.record_failure("ada@example.com")
    first.reset()
    for name in budgets:
        assert getattr(first, name).hit("k") is None, f"{name} was not reset"
    assert first.login_failures._failures.count("ada@example.com") == 0


def test_an_app_keeps_its_own_numbers() -> None:
    """Kurvenschmiede's login window is 10 a minute; kastlan may pass its own throttle."""
    throttle = LoginFailureThrottle(free=3)
    limiters = AuthLimiters(login=Budget(10, 60), login_failures=throttle)
    assert (limiters.login.max_hits, limiters.login.window_seconds) == (10, 60)
    assert limiters.login_failures is throttle


def test_a_budget_s_limiter_defaults_to_the_monotonic_clock() -> None:
    # kastlan's 0.31 adoption: a per-demo PDF budget built without passing a clock.
    limiter = Budget(2, 60).limiter()
    assert limiter.hit("demo-1") is None
