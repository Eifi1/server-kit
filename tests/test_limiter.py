"""The in-process sliding-window limiter — keksdose ``tests/unit/test_rate_limit.py``, ported."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from eifi1_server_kit.limiter import (
    FEEDBACK_COMMENTS_PER_HOUR,
    FEEDBACK_CRASHES_PER_HOUR,
    FEEDBACK_UPLOADS_PER_HOUR,
    FEEDBACK_VOTES_PER_HOUR,
    UNKNOWN_CLIENT,
    FeedbackLimiters,
    SlidingWindowRateLimiter,
    ThrottledError,
    charge,
    client_ip,
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
    assert retry_after_header(-5.0) == "1", "never below one second"


# --- charge and ThrottledError (0.21): one 429, uncoded, with Retry-After --------------------


def _full(clock: _Clock, *, max_hits: int = 2, window: float = 60) -> SlidingWindowRateLimiter:
    limiter = SlidingWindowRateLimiter(max_hits=max_hits, window_seconds=window, clock=clock)
    for _ in range(max_hits):
        charge(limiter, "ip", "never said")
    return limiter


def test_charge_lets_a_hit_in_and_records_it() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=2, window_seconds=60, clock=_Clock())
    charge(limiter, "ip", "Too many")
    assert limiter.count("ip") == 1


def test_a_full_window_is_a_429_with_the_apps_sentence() -> None:
    clock = _Clock()
    limiter = _full(clock)
    clock.t += 10.4
    with pytest.raises(ThrottledError) as refused:
        charge(limiter, "ip", "Too many sign-in attempts — please try again in a moment.")
    error = refused.value
    assert isinstance(error, ValueError) and error.status_code == 429
    assert str(error) == "Too many sign-in attempts — please try again in a moment."
    assert error.retry_after == pytest.approx(49.6)
    assert error.headers == {"Retry-After": "50"}
    assert not hasattr(error, "code"), "Marcel, 2026-11-04: the 429 carries no code"
    assert limiter.count("ip") == 2, "a refused hit is not recorded"


@pytest.mark.parametrize(
    ("wait", "header"), [(0.0, "1"), (0.001, "1"), (0.999, "1"), (1.0, "2"), (59.2, "60"), (3600.0, "3601")]
)
def test_the_header_is_whole_seconds_rounded_up_at_least_one(wait: float, header: str) -> None:
    """keksdose's ``str(int(retry_after) + 1)``: its feedback test pins ``"3601"`` for a full hour."""
    assert ThrottledError("Too many", wait).headers == {"Retry-After": header}


def test_a_hit_of_zero_seconds_is_a_refusal() -> None:
    """keksdose ``_throttle``'s note: ``hit`` may answer ``0.0``, and ``0.0`` is falsy."""

    class Zero(SlidingWindowRateLimiter):
        def hit(self, key: str) -> float | None:
            return 0.0

    with pytest.raises(ThrottledError) as refused:
        charge(Zero(max_hits=1, window_seconds=60), "ip", lambda seconds: f"Try again in {seconds} seconds.")
    assert str(refused.value) == "Try again in 1 seconds."
    assert refused.value.headers == {"Retry-After": "1"}


def test_the_sentence_may_say_the_seconds_the_header_says() -> None:
    """Kurvenschmiede's: "Try again in N seconds", N the ``Retry-After``."""
    clock = _Clock()
    limiter = _full(clock, max_hits=1, window=300)
    clock.t += 0.5
    with pytest.raises(ThrottledError) as refused:
        charge(
            limiter, "ip", lambda seconds: f"Too many new accounts from this address. Try again in {seconds} seconds."
        )
    assert str(refused.value) == "Too many new accounts from this address. Try again in 300 seconds."
    assert refused.value.headers == {"Retry-After": "300"}


def test_a_disabled_limiter_charges_nothing() -> None:
    limiter = SlidingWindowRateLimiter(max_hits=0, window_seconds=60, clock=_Clock())
    for _ in range(100):
        charge(limiter, "ip", "never")


async def test_the_answer_is_fastapis_body_and_the_header() -> None:
    """Through ``install_contract_error_handlers``, as the apps' ``HTTPException`` answered:
    ``{"detail": …}``, nothing else, and ``Retry-After`` — beside an app's ValueError → 400."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient

    from eifi1_server_kit.errors import install_contract_error_handlers

    clock = _Clock()
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=60, clock=clock)
    app = FastAPI()

    async def bad_request(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.add_exception_handler(ValueError, bad_request)  # keksdose's and Kurvenschmiede's
    install_contract_error_handlers(app)

    @app.post("/auth/login")
    async def login() -> dict[str, bool]:
        charge(limiter, "ip", "Too many sign-in attempts — please try again in a moment.")
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/auth/login")).status_code == 200
        clock.t += 30.2
        refused = await client.post("/auth/login")
    assert refused.status_code == 429
    assert refused.json() == {"detail": "Too many sign-in attempts — please try again in a moment."}
    assert refused.headers["Retry-After"] == "30"


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


def test_public_feedback_has_its_own_comment_and_vote_budgets() -> None:
    """Feedback §9.2 decision 19: comments 20 an hour, votes 60, per user, each its own window."""
    clock = _Clock()
    limiters = FeedbackLimiters(clock=clock)
    assert limiters.comment.max_hits == FEEDBACK_COMMENTS_PER_HOUR == 20
    assert limiters.vote.max_hits == FEEDBACK_VOTES_PER_HOUR == 60
    assert limiters.comment.window_seconds == limiters.vote.window_seconds == 3600
    for _ in range(20):
        assert limiters.comment.hit("7") is None
    assert limiters.comment.hit("7") is not None
    assert limiters.vote.hit("7") is None and limiters.upload.hit("7") is None, "separate windows"
    limiters.reset()
    assert limiters.comment.hit("7") is None
    assert limiters.vote.count("7") == 0, "reset clears the vote window too"
    tight = FeedbackLimiters(comments_per_hour=1, votes_per_hour=1, clock=clock)
    assert tight.comment.hit("7") is None and tight.comment.hit("7") is not None
    assert tight.vote.hit("7") is None and tight.vote.hit("7") is not None


# ── client_ip: counted from the right (landing-demo contract §5.1) ──────────


def _xff(*lines: str) -> Headers:
    return Headers(raw=[(b"x-forwarded-for", line.encode()) for line in lines])


@pytest.mark.parametrize(
    ("header", "hops", "expected"),
    [
        # Caddy alone appends the client's address: the right-most entry.
        ("198.51.100.4", 1, "198.51.100.4"),
        # A client-written entry sits LEFT of what the proxies append, and is never read.
        ("203.0.113.7, 198.51.100.4", 1, "198.51.100.4"),
        ("1.1.1.1, 2.2.2.2, 3.3.3.3, 198.51.100.4", 1, "198.51.100.4"),
        # Two proxies (a front end, then Caddy): the second from the right.
        ("203.0.113.7, 198.51.100.4, 10.0.0.2", 2, "198.51.100.4"),
        ("198.51.100.4, 10.0.0.2", 2, "198.51.100.4"),
        # Spaces and empty entries don't count.
        (" 203.0.113.7 ,, 198.51.100.4 ,", 1, "198.51.100.4"),
        ("2001:db8::7", 1, "2001:db8::7"),
    ],
)
def test_client_ip_counts_from_the_right(header: str, hops: int, expected: str) -> None:
    assert client_ip(_xff(header), "10.0.0.9", trusted_hops=hops) == expected


def test_a_spoofed_left_most_entry_never_buys_a_fresh_window() -> None:
    """keksdose's review of the contract: a random header per request must not re-key the
    window, or 500 scripted demo starts would close the demo for a day."""
    limiter = SlidingWindowRateLimiter(max_hits=5, window_seconds=3600, clock=_Clock())
    answers = [
        limiter.hit(client_ip(_xff(f"203.0.113.{n}, 198.51.100.4"), "10.0.0.9", trusted_hops=1)) for n in range(6)
    ]
    assert answers[:5] == [None] * 5 and answers[5] is not None


def test_without_proxies_the_peer_answers_and_the_header_is_ignored() -> None:
    assert client_ip(_xff("203.0.113.7"), "192.0.2.1", trusted_hops=0) == "192.0.2.1"
    assert client_ip(_xff("203.0.113.7"), None, trusted_hops=0) == UNKNOWN_CLIENT == "unknown"


def test_a_header_shorter_than_the_proxies_counted_is_not_believed() -> None:
    """It did not pass every proxy, so nothing in it is vouched for: the peer, never the
    left-most entry."""
    assert client_ip(_xff("203.0.113.7"), "10.0.0.9", trusted_hops=2) == "10.0.0.9"
    assert client_ip(Headers(), "10.0.0.9", trusted_hops=1) == "10.0.0.9"
    assert client_ip(Headers(), None, trusted_hops=1) == "unknown"


def test_repeated_header_lines_are_one_list_in_order() -> None:
    """A client's own line comes first; the proxy's appended line last."""
    assert client_ip(_xff("203.0.113.7", "198.51.100.4"), "10.0.0.9", trusted_hops=1) == "198.51.100.4"
    assert client_ip(_xff("203.0.113.7", "198.51.100.4, 10.0.0.2"), "10.0.0.9", trusted_hops=3) == "203.0.113.7"


def test_a_plain_mapping_is_matched_without_regard_to_case() -> None:
    assert client_ip({"X-Forwarded-For": "203.0.113.7, 198.51.100.4"}, "10.0.0.9", trusted_hops=1) == "198.51.100.4"
    assert client_ip({"accept": "*/*"}, "10.0.0.9", trusted_hops=1) == "10.0.0.9"


def test_trusted_hops_counts_proxies() -> None:
    with pytest.raises(ValueError, match="0 or more"):
        client_ip(Headers(), "10.0.0.9", trusted_hops=-1)


def test_a_short_header_is_logged_once_per_process(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keksdose (0.5.1): a mis-measured hop count silently put every caller into one
    window; the first short header says so, the next ones don't repeat it."""
    import eifi1_server_kit.limiter as limiter_module

    monkeypatch.setattr(limiter_module, "_short_header_warned", False)
    with caplog.at_level("WARNING", logger="eifi1_server_kit.limiter"):
        client_ip(_xff("203.0.113.7"), "10.0.0.9", trusted_hops=3)
        client_ip(_xff("203.0.113.7"), "10.0.0.9", trusted_hops=3)
    warnings = [r for r in caplog.records if r.name == "eifi1_server_kit.limiter"]
    assert len(warnings) == 1
    assert "1 entries, fewer than trusted_hops=3" in warnings[0].getMessage()
    assert "203.0.113.7" not in warnings[0].getMessage()
