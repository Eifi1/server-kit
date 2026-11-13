"""A session's life on the server (0.27): a shared computer's is short, a normal one keeps
the app's lifetime — Marcel's decisions of 2026-11-12, after kastlan found that a closed tab
comes back with its ``sessionStorage``."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from eifi1_server_kit.auth import (
    ACCESS_TOKEN_LIFETIME,
    REFRESH_TOKEN_LIFETIME,
    SHARED_ABSOLUTE,
    SHARED_ACCESS_LIFETIME,
    SHARED_IDLE,
    SessionPolicy,
    access_lifetime,
    refresh_claims,
    refresh_lifetime,
    session_expired,
    session_expires_at,
)

POLICY = SessionPolicy()
#: kastlan's: fifteen minutes of access (``access_token_expire_minutes = 15``).
KASTLAN = SessionPolicy(access_lifetime=timedelta(minutes=15))
STARTED = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
MICRO = timedelta(microseconds=1)


def test_the_defaults_are_marcels_and_the_apps() -> None:
    """15 minutes idle and 4 hours at most on a shared computer; a normal session keeps the
    apps' 24 h access and 30 days refresh (keksdose's and Kurvenschmiede's settings)."""
    assert (timedelta(minutes=15), timedelta(hours=4)) == (SHARED_IDLE, SHARED_ABSOLUTE)
    assert timedelta(minutes=5) == SHARED_ACCESS_LIFETIME
    assert (
        SessionPolicy(
            access_lifetime=ACCESS_TOKEN_LIFETIME,
            refresh_lifetime=REFRESH_TOKEN_LIFETIME,
            shared_idle=SHARED_IDLE,
            shared_absolute=SHARED_ABSOLUTE,
            shared_access_lifetime=SHARED_ACCESS_LIFETIME,
        )
        == POLICY
    )
    assert (POLICY.access_lifetime, POLICY.refresh_lifetime) == (timedelta(hours=24), timedelta(days=30))
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLICY.shared_idle = timedelta(hours=1)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"shared_idle": timedelta(0)}, "shared_idle must be positive"),
        ({"shared_absolute": -timedelta(minutes=1)}, "shared_absolute must be positive"),
        ({"access_lifetime": timedelta(0)}, "access_lifetime must be positive"),
        ({"refresh_lifetime": timedelta(0)}, "refresh_lifetime must be positive"),
        ({"shared_access_lifetime": timedelta(0)}, "shared_access_lifetime must be positive"),
        ({"shared_access_lifetime": timedelta(minutes=15)}, "shorter than shared_idle"),
        ({"shared_idle": timedelta(hours=5)}, "shared_idle cannot be longer than shared_absolute"),
    ],
)
def test_a_policy_that_cannot_work_is_refused(fields: dict[str, timedelta], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SessionPolicy(**fields)


def test_a_shared_session_ends_after_the_idle_limit_at_the_limit_itself() -> None:
    """Ended AT the limit, as a token's ``exp`` and :func:`is_expired` are."""
    last = STARTED + timedelta(minutes=30)
    expired = {
        idle: session_expired(POLICY, shared=True, started_at=STARTED, last_active=last, now=last + idle)
        for idle in (timedelta(0), SHARED_IDLE - MICRO, SHARED_IDLE, SHARED_IDLE + MICRO)
    }
    assert expired == {timedelta(0): False, SHARED_IDLE - MICRO: False, SHARED_IDLE: True, SHARED_IDLE + MICRO: True}


def test_a_shared_session_ends_at_its_absolute_limit_however_busy() -> None:
    end = STARTED + SHARED_ABSOLUTE
    for now, ended in ((end - MICRO, False), (end, True), (end + timedelta(days=1), True)):
        # Active a second ago: only the absolute limit can end it.
        assert session_expired(POLICY, shared=True, started_at=STARTED, last_active=now - MICRO, now=now) is ended


def test_a_normal_session_ends_when_its_refresh_token_would() -> None:
    """What the apps do today: every refresh re-mints the full 30 days, so a session ends
    after 30 days unused, and never for its age."""
    last = STARTED + timedelta(days=400)
    for idle, ended in ((REFRESH_TOKEN_LIFETIME - MICRO, False), (REFRESH_TOKEN_LIFETIME, True)):
        assert session_expired(POLICY, shared=False, started_at=STARTED, last_active=last, now=last + idle) is ended
    assert not session_expired(
        POLICY, shared=False, started_at=STARTED, last_active=last, now=last + timedelta(hours=5)
    ), "400 days old, used five hours ago"


def test_a_clock_behind_the_last_activity_is_idle_zero_never_an_ended_session() -> None:
    """Two clocks a little apart (two instances, the database's ``now()``): a ``now`` before
    ``last_active`` or ``started_at`` ends nothing. The absolute limit still holds."""
    last = STARTED + timedelta(minutes=20)
    before = last - timedelta(seconds=3)
    for shared in (True, False):
        assert not session_expired(POLICY, shared=shared, started_at=STARTED, last_active=last, now=before)
        assert not session_expired(POLICY, shared=shared, started_at=STARTED, last_active=STARTED, now=STARTED - MICRO)
    late = STARTED + SHARED_ABSOLUTE
    assert session_expired(POLICY, shared=True, started_at=STARTED, last_active=late + timedelta(minutes=1), now=late)


def test_the_expiry_is_the_earlier_limit_and_naive_is_utc() -> None:
    """The ``expires_at`` column: written with ``last_active`` at sign-in and at every
    refresh, so the per-request check can be ``expires_at > now`` in SQL."""
    early = STARTED + timedelta(minutes=10)
    assert session_expires_at(POLICY, shared=True, started_at=STARTED, last_active=early) == early + SHARED_IDLE
    late = STARTED + timedelta(hours=3, minutes=50)
    assert session_expires_at(POLICY, shared=True, started_at=STARTED, last_active=late) == STARTED + SHARED_ABSOLUTE
    assert session_expires_at(POLICY, shared=False, started_at=STARTED, last_active=late) == late + timedelta(days=30)
    naive = STARTED.replace(tzinfo=None)
    assert session_expires_at(POLICY, shared=True, started_at=naive, last_active=naive) == STARTED + SHARED_IDLE
    assert session_expired(POLICY, shared=True, started_at=naive, last_active=naive, now=STARTED + SHARED_IDLE)


def test_a_shared_refresh_token_lives_the_idle_limit_and_never_past_the_end() -> None:
    lifetime = {
        age: refresh_lifetime(POLICY, shared=True, started_at=STARTED, now=STARTED + age)
        for age in (
            timedelta(0),
            SHARED_ABSOLUTE - SHARED_IDLE,
            SHARED_ABSOLUTE - timedelta(minutes=4),
            SHARED_ABSOLUTE,
            SHARED_ABSOLUTE + timedelta(hours=1),
        )
    }
    assert list(lifetime.values()) == [SHARED_IDLE, SHARED_IDLE, timedelta(minutes=4), timedelta(0), timedelta(0)]
    assert refresh_lifetime(POLICY, shared=True, started_at=STARTED, now=STARTED - timedelta(hours=1)) == SHARED_IDLE
    assert refresh_lifetime(POLICY, shared=False, started_at=STARTED, now=STARTED + timedelta(days=99)) == timedelta(
        days=30
    )


def test_a_shared_access_token_is_shorter_than_the_idle_limit() -> None:
    """Shorter than the refresh token, so the client's refresh — on the first 401 after the
    access token ends — finds its refresh token still alive."""
    now = STARTED + timedelta(hours=1)
    assert access_lifetime(POLICY, shared=True, started_at=STARTED, now=now) == SHARED_ACCESS_LIFETIME
    assert access_lifetime(KASTLAN, shared=True, started_at=STARTED, now=now) == SHARED_ACCESS_LIFETIME
    brief = SessionPolicy(access_lifetime=timedelta(minutes=2))
    assert access_lifetime(brief, shared=True, started_at=STARTED, now=now) == timedelta(minutes=2), "never longer"
    near_end = STARTED + SHARED_ABSOLUTE - timedelta(minutes=3)
    assert access_lifetime(POLICY, shared=True, started_at=STARTED, now=near_end) == timedelta(minutes=3)
    assert access_lifetime(POLICY, shared=True, started_at=STARTED, now=STARTED + SHARED_ABSOLUTE) == timedelta(0)
    assert access_lifetime(POLICY, shared=False, started_at=STARTED, now=now) == timedelta(hours=24)
    assert access_lifetime(KASTLAN, shared=False, started_at=STARTED, now=now) == timedelta(minutes=15)


def test_no_shared_token_outlives_its_session() -> None:
    """``exp`` is whole seconds, rounded down, so the token ends at or before the row."""
    now = STARTED + timedelta(hours=3, minutes=51, seconds=7, microseconds=900_000)
    claims = refresh_claims(7, now=now, lifetime=refresh_lifetime(POLICY, shared=True, started_at=STARTED, now=now))
    row_end = session_expires_at(POLICY, shared=True, started_at=STARTED, last_active=now)
    assert claims["exp"] <= row_end.timestamp()
    assert row_end == STARTED + SHARED_ABSOLUTE


def _signed_in_until(policy: SessionPolicy, requests: list[timedelta]) -> timedelta | None:
    """The three call sites over a timeline, as the client drives them: every request is
    checked against the row; one sent with an ended access token is a 401, and the client
    refreshes (ui-kit's ``attachSessionRefresh``) — refused when the refresh token has
    ended or the row has; a refresh moves ``last_active`` and mints a new pair. Answers the
    offset of the first request that found the session ended, or ``None``."""
    last_active = STARTED
    access_end = STARTED + access_lifetime(policy, shared=True, started_at=STARTED, now=STARTED)
    refresh_end = STARTED + refresh_lifetime(policy, shared=True, started_at=STARTED, now=STARTED)
    for offset in requests:
        now = STARTED + offset
        if now >= access_end:
            ended = now >= refresh_end or session_expired(
                policy, shared=True, started_at=STARTED, last_active=last_active, now=now
            )
            if ended:
                return offset
            last_active = now
            access_end = now + access_lifetime(policy, shared=True, started_at=STARTED, now=now)
            refresh_end = now + refresh_lifetime(policy, shared=True, started_at=STARTED, now=now)
        elif session_expired(policy, shared=True, started_at=STARTED, last_active=last_active, now=now):
            return offset
    return None


@pytest.mark.parametrize("policy", [POLICY, KASTLAN], ids=["keksdose", "kastlan"])
def test_somebody_at_work_stays_signed_in_until_the_absolute_limit(policy: SessionPolicy) -> None:
    """A request every ten minutes — never idle for the limit — signs out only at 4 hours,
    whatever the app's own access lifetime (kastlan's 15 minutes would equal the idle limit
    and end the refresh token with the access token)."""
    every_ten = [timedelta(minutes=minute) for minute in range(1, 24 * 60, 10)]
    assert _signed_in_until(policy, every_ten) == timedelta(hours=4, minutes=1)


@pytest.mark.parametrize("policy", [POLICY, KASTLAN], ids=["keksdose", "kastlan"])
def test_an_abandoned_tab_is_refused_once_the_idle_limit_has_passed(policy: SessionPolicy) -> None:
    """The library PC: a request, then nobody. The tab restored 15 minutes after the last
    refresh finds the session ended. The idle limit counts from the last refresh, which is
    at most one shared access lifetime before the last request, so it ends between 10 and
    15 minutes after the last request."""
    assert _signed_in_until(policy, [timedelta(minutes=1), timedelta(minutes=16)]) == timedelta(minutes=16)
    assert _signed_in_until(policy, [timedelta(minutes=6), timedelta(minutes=21)]) == timedelta(minutes=21)
    last_request = timedelta(minutes=4, seconds=59)  # on the sign-in's access token: no refresh
    assert _signed_in_until(policy, [last_request, SHARED_IDLE]) == SHARED_IDLE, "10 min 1 s after it"
    assert _signed_in_until(policy, [timedelta(minutes=14)]) is None, "idle 14 minutes: still signed in"
