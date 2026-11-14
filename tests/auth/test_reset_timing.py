"""The reset request's time tells nobody whether an account exists (0.31, G11; Kurvenschmiede's
audit F29): the mail is committed and sent inside the route, before the answer, and every path
answers when ``AccountPolicy.answer_floor`` has passed since the windows were charged.

The clock is a fake one: the store's write and the mailer's round trip move it, and the route's
wait moves it by what it waits. So each request's time is exact, and so is the difference."""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import OneTimeTokenKind
from eifi1_server_kit.auth.schemas import PasswordResetRequest
from eifi1_server_kit.mail import MailMessage
from tests.auth._world import POLICY, World

FLOOR = 3.0
#: What the database's write and the provider's round trip cost on the fake clock (binary
#: fractions, so every sum is exact).
WRITE, ROUND_TRIP = 0.0625, 0.375


class Timer:
    """A monotonic clock a test moves, and a ``sleep`` that moves it."""

    def __init__(self) -> None:
        self.at = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.at

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += seconds


class TimedWorld(World):
    """The world, with the store's write and the mailer's round trip costing time."""

    def __init__(self, timer: Timer) -> None:
        super().__init__(
            cutoff=True, timer=timer, sleep=timer.sleep, policy=replace(POLICY, answer_floor=timedelta(seconds=FLOOR))
        )
        self.timer = timer
        put, send = self.tokens.put, self.mailer.send

        async def slow_put(kind: OneTimeTokenKind, **values: Any) -> Any:
            timer.at += WRITE
            return await put(kind, **values)

        async def slow_send(to: str, message: MailMessage, *, kind: str = "other") -> bool:
            timer.at += ROUND_TRIP
            return await send(to, message, kind=kind)

        self.tokens.put = slow_put  # type: ignore[method-assign]
        self.mailer.send = slow_send  # type: ignore[method-assign]

    async def timed(self, email: str, *, ip: str = "198.51.100.1") -> tuple[int, float]:
        """One reset request and its time on the fake clock, the commit included (the
        function-scoped unit of work commits before the answer, and the work queued for
        after the commit runs in it)."""
        start = self.timer()
        response = await self.call(
            "POST", "/auth/password-reset/request", json={"email": email}, headers={"x-test-ip": ip}
        )
        return response.status_code, self.timer() - start


@pytest.fixture
async def timed() -> AsyncIterator[TimedWorld]:
    built = TimedWorld(Timer())
    async with built.client:
        yield built


async def test_an_account_and_no_account_answer_in_the_same_time(timed: TimedWorld) -> None:
    timed.account(email="ada@example.com")
    timed.account(email="off@example.com", is_active=False)
    timed.account(email="demo@example.com", is_demo=True)
    answers = {email: await timed.timed(email) for email in ("ada@example.com", "nobody@example.com")}
    answers["off"] = await timed.timed("off@example.com")
    answers["demo"] = await timed.timed("demo@example.com")
    assert set(answers.values()) == {(204, FLOOR)}, answers
    (mail,) = timed.mailer.of("password_reset")
    assert mail.to == "ada@example.com"


async def test_the_reset_mail_is_committed_and_sent_before_the_answer(timed: TimedWorld) -> None:
    timed.account()
    commits = timed.after.commits
    service = timed.service(timer=timed.timer, sleep=timed.timer.sleep)
    await service.request_password_reset(PasswordResetRequest(email="ada@example.com"), ip="198.51.100.1")
    (mail,) = timed.mailer.of("password_reset")
    assert mail.after_commits == commits + 1, "the route committed, then sent: nothing waits for after the answer"
    assert timed.after.queue == []


async def test_a_send_slower_than_the_floor_is_cut_and_logged_and_the_answer_keeps_the_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timer = Timer()
    world = TimedWorld(timer)
    world.account()
    started = asyncio.Event()

    async def hangs(to: str, message: MailMessage, *, kind: str = "other") -> bool:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("never answers")

    world.mailer.send = hangs  # type: ignore[method-assign]
    # The write costs 0.0625 of the 0.125: the send gets the rest, on the loop's own clock.
    policy = replace(POLICY, answer_floor=timedelta(seconds=0.125))
    service = world.service(timer=timer, sleep=timer.sleep, policy=policy)
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.auth"):
        start = timer()
        await service.request_password_reset(PasswordResetRequest(email="ada@example.com"), ip="198.51.100.1")
    assert started.is_set() and timer() - start == 0.125
    assert "password_reset" in caplog.text and "ada@example.com" not in caplog.text


async def test_a_write_slower_than_the_floor_still_sends_the_mail_unbounded() -> None:
    timer = Timer()
    world = TimedWorld(timer)
    world.account()
    put = world.tokens.put

    async def stuck_put(kind: OneTimeTokenKind, **values: Any) -> Any:
        timer.at += FLOOR + 1
        return await put(kind, **values)

    world.tokens.put = stuck_put  # type: ignore[method-assign]
    async with world.client:
        status, took = await world.timed("ada@example.com")
    assert status == 204 and took == FLOOR + 1 + WRITE + ROUND_TRIP, "the answer is late already: the mail goes"
    assert len(world.mailer.of("password_reset")) == 1 and timer.slept == []


async def test_a_refused_window_answers_at_once_account_or_not(timed: TimedWorld) -> None:
    timed.account(email="ada@example.com")
    for email in ("ada@example.com", "nobody@example.com"):
        for _ in range(3):
            await timed.timed(email)
    assert await timed.timed("ada@example.com") == (429, 0.0)
    assert await timed.timed("nobody@example.com") == (429, 0.0)


async def test_the_floor_is_zero_or_more() -> None:
    with pytest.raises(ValueError, match="answer_floor"):
        replace(POLICY, answer_floor=timedelta(seconds=-1))
