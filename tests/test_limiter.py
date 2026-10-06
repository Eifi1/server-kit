"""The in-process sliding-window limiter — keksdose ``tests/unit/test_rate_limit.py``, ported."""

from __future__ import annotations

from eifi1_server_kit.limiter import (
    FEEDBACK_CRASHES_PER_HOUR,
    FEEDBACK_UPLOADS_PER_HOUR,
    FeedbackLimiters,
    SlidingWindowRateLimiter,
    retry_after_header,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_allows_up_to_max_then_blocks() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=3, window_seconds=60, clock=_Clock())
    assert [limiter.hit("ip") for _ in range(3)] == [None, None, None]
    retry_after = limiter.hit("ip")
    assert retry_after is not None and 0 < retry_after <= 60


def test_window_slides_so_slots_free_up() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=clock)
    assert limiter.hit("ip") is None
    assert limiter.hit("ip") is not None
    clock.t += 61
    assert limiter.hit("ip") is None


def test_blocked_attempts_do_not_extend_the_penalty() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=clock)
    assert limiter.hit("ip") is None
    for _ in range(5):
        clock.t += 5
        assert limiter.hit("ip") is not None
    clock.t = 1060.0  # exactly one window after the accepted hit
    assert limiter.hit("ip") is None


def test_the_oldest_hit_ages_out_of_a_live_window() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=2, window_seconds=60, clock=clock)
    assert limiter.hit("ip") is None  # t=1000
    clock.t += 30
    assert limiter.hit("ip") is None  # t=1030
    assert limiter.hit("ip") == 30.0  # the first frees at 1060
    clock.t += 40  # t=1070: the first has aged out, the second has not
    assert limiter.hit("ip") is None
    assert limiter.hit("ip") is not None


def test_keys_are_independent() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=_Clock())
    assert limiter.hit("a") is None
    assert limiter.hit("a") is not None
    assert limiter.hit("b") is None


def test_disabled_when_max_non_positive() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=0, window_seconds=60, clock=_Clock())
    assert not limiter.enabled
    assert all(limiter.hit("ip") is None for _ in range(10))
    assert not SlidingWindowRateLimiter(max_hits=5, window_seconds=0).enabled


def test_reset_clears_windows() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=_Clock())
    assert limiter.hit("ip") is None
    assert limiter.hit("ip") is not None
    limiter.reset()
    assert limiter.hit("ip") is None


def test_count_reads_the_live_window_without_charging_it() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=2, window_seconds=60, clock=clock)
    assert limiter.count("ip") == 0 and "ip" not in limiter._hits
    limiter.hit("ip")
    clock.t += 30
    limiter.hit("ip")
    limiter.hit("ip")  # blocked, not recorded
    assert limiter.count("ip") == 2
    clock.t += 31  # the first has aged out
    assert limiter.count("ip") == 1
    assert limiter.count("ip") == 1, "counting charged a hit"


def test_forget_drops_one_key_only() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=_Clock())
    limiter.hit("a")
    limiter.hit("b")
    limiter.forget("a")
    limiter.forget("never-seen")
    assert limiter.hit("a") is None
    assert limiter.hit("b") is not None


def test_a_key_that_has_aged_out_stops_costing_memory() -> None:
    """keksdose ``:78``: the keys are client-supplied, so the map must come back down."""
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=3, window_seconds=60, clock=clock)
    for n in range(500):
        assert limiter.hit(f"10.0.0.{n}") is None
    assert len(limiter._hits) == 500
    clock.t += 61
    assert limiter.hit("10.0.1.1") is None
    assert len(limiter._hits) == 1


def test_the_sweep_keeps_a_LIVE_window() -> None:
    """keksdose ``:99``: a sweep that dropped a live window would hand an allowance back early."""
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=2, window_seconds=60, clock=clock)
    assert limiter.hit("live") is None
    clock.t += 61
    assert limiter.hit("live") is None
    assert limiter.hit("live") is None
    assert limiter.hit("live") is not None, "the sweep handed back a live allowance"


def test_the_sweep_runs_at_most_once_per_window() -> None:
    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=100, window_seconds=60, clock=clock)
    for n in range(50):
        limiter.hit(f"k{n}")
    swept = limiter._last_sweep
    for n in range(50, 100):
        limiter.hit(f"k{n}")
    assert limiter._last_sweep == swept, "the sweep ran again inside one window"


def test_retry_after_rounds_up_past_the_wait() -> None:
    """keksdose ``upload_guards.py:82``: ``str(int(retry_after) + 1)``."""
    assert retry_after_header(0.0) == "1"
    assert retry_after_header(59.2) == "60"
    assert retry_after_header(3599.99) == "3600"


def test_feedback_limiters_are_per_instance_with_the_contracts_budgets() -> None:
    """No module globals: each app (each test's app) has its own windows (Kurvenschmiede
    ``security.py:318`` ``FeedbackLimiters``)."""
    clock = _Clock()
    first, second = FeedbackLimiters(clock=clock), FeedbackLimiters(clock=clock)
    assert first.upload.max_hits == FEEDBACK_UPLOADS_PER_HOUR == 20
    assert first.crash.max_hits == FEEDBACK_CRASHES_PER_HOUR == 20
    assert first.upload.window_seconds == 3600
    for _ in range(20):
        assert first.upload.hit("7") is None
    assert first.upload.hit("7") is not None
    assert first.crash.hit("7") is None, "the two budgets are separate"
    assert second.upload.hit("7") is None, "a second app starts empty"
    first.reset()
    assert first.upload.hit("7") is None
    tight = FeedbackLimiters(uploads_per_hour=1, crashes_per_hour=1, clock=clock)
    assert tight.crash.hit("7") is None and tight.crash.hit("7") is not None
