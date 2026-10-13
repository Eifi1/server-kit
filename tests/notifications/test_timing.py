"""Quiet hours, the zone and the digest's period (notifications §2.4–§2.6, §3.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from eifi1_server_kit.notifications import (
    DEFAULT_ZONE,
    DIGEST_CADENCES,
    DIGEST_LATE_DAYS,
    QUIET_HOURS_PATTERN,
    digest_due,
    digest_period,
    digest_tag,
    in_quiet_hours,
    is_known_zone,
    resolve_zone,
)

ZURICH = ZoneInfo("Europe/Zurich")


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ── the zone ─────────────────────────────────────────────────────────────────


def test_the_constants() -> None:
    assert DEFAULT_ZONE == "Europe/Zurich"
    assert DIGEST_CADENCES == ("weekly", "monthly") and DIGEST_LATE_DAYS == 3
    assert QUIET_HOURS_PATTERN == r"^([01]\d|2[0-3]):[0-5]\d$"


@pytest.mark.parametrize("name", ["Europe/Zurich", "UTC", "America/Argentina/Buenos_Aires", "Asia/Kolkata"])
def test_a_known_zone(name: str) -> None:
    assert is_known_zone(name)


@pytest.mark.parametrize(
    "name", [None, "", "Europe", "Mars/Olympus", "../etc/passwd", "/etc/localtime", "Europe/Zurich/", "Europe/Zurich "]
)
def test_an_unknown_zone(name: str | None) -> None:
    assert not is_known_zone(name)


def test_an_unset_zone_is_the_apps_zone_never_utc() -> None:
    assert resolve_zone(None) == ZURICH
    assert resolve_zone("") == ZURICH
    assert resolve_zone("America/New_York") == ZoneInfo("America/New_York")


def test_a_zone_that_no_longer_resolves_counts_as_unset() -> None:
    """keksdose fell back to UTC (``notification_preferences_service.py:169-174``)."""
    assert resolve_zone("Mars/Olympus") == ZURICH
    assert resolve_zone("Mars/Olympus", fallback="UTC") == ZoneInfo("UTC")
    assert resolve_zone("Asia/Tokyo", fallback="UTC") == ZoneInfo("Asia/Tokyo")


# ── quiet hours ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("clock", "inside"),
    [("08:00", True), ("11:59", True), ("12:00", False), ("07:59", False), ("23:00", False)],
)
def test_a_window_within_the_day(clock: str, inside: bool) -> None:
    hours, minutes = map(int, clock.split(":"))
    now = _utc(2026, 10, 13, hours, minutes)
    assert in_quiet_hours("08:00", "12:00", ZoneInfo("UTC"), now) is inside


@pytest.mark.parametrize(
    ("clock", "inside"),
    [("22:00", True), ("23:30", True), ("00:00", True), ("06:59", True), ("07:00", False), ("21:59", False)],
)
def test_a_window_across_midnight(clock: str, inside: bool) -> None:
    hours, minutes = map(int, clock.split(":"))
    now = _utc(2026, 10, 13, hours, minutes)
    assert in_quiet_hours("22:00", "07:00", ZoneInfo("UTC"), now) is inside


def test_the_window_is_read_in_the_accounts_zone() -> None:
    # 21:30 UTC is 23:30 in Zurich (summer time) and 17:30 in New York.
    now = _utc(2026, 10, 13, 21, 30)
    assert in_quiet_hours("22:00", "07:00", ZURICH, now)
    assert not in_quiet_hours("22:00", "07:00", ZoneInfo("America/New_York"), now)
    assert in_quiet_hours("22:00", "07:00", ZURICH, now.astimezone(ZoneInfo("Asia/Tokyo"))), "the same instant"


def test_the_spring_change_in_zurich() -> None:
    """2026-03-29: 02:00 CET is 03:00 CEST, at 01:00 UTC. The skipped hour never happens."""
    assert not in_quiet_hours("02:00", "03:00", ZURICH, _utc(2026, 3, 29, 0, 59)), "01:59 CET"
    assert not in_quiet_hours("02:00", "03:00", ZURICH, _utc(2026, 3, 29, 1, 0)), "03:00 CEST"
    assert in_quiet_hours("01:30", "03:30", ZURICH, _utc(2026, 3, 29, 1, 0)), "03:00 CEST"
    assert not in_quiet_hours("01:30", "03:30", ZURICH, _utc(2026, 3, 29, 1, 30)), "03:30 CEST"
    assert in_quiet_hours("22:00", "07:00", ZURICH, _utc(2026, 3, 29, 4, 59)), "06:59 CEST"
    assert not in_quiet_hours("22:00", "07:00", ZURICH, _utc(2026, 3, 29, 5, 0)), "07:00 CEST"


def test_the_autumn_change_in_zurich() -> None:
    """2026-10-25: 03:00 CEST is 02:00 CET, at 01:00 UTC. The repeated hour holds both times."""
    assert in_quiet_hours("02:00", "03:00", ZURICH, _utc(2026, 10, 25, 0, 30)), "02:30 CEST"
    assert in_quiet_hours("02:00", "03:00", ZURICH, _utc(2026, 10, 25, 1, 30)), "02:30 CET"
    assert not in_quiet_hours("02:00", "03:00", ZURICH, _utc(2026, 10, 25, 2, 0)), "03:00 CET"
    assert in_quiet_hours("22:00", "07:00", ZURICH, _utc(2026, 10, 25, 5, 59)), "06:59 CET"
    assert not in_quiet_hours("22:00", "07:00", ZURICH, _utc(2026, 10, 25, 6, 0)), "07:00 CET"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("22:00", None),
        (None, "07:00"),
        (None, None),
        ("22:00", "22:00"),
        ("00:00", "00:00"),
        ("24:00", "07:00"),
        ("7:00", "08:00"),
        ("22:00", "07:00\n"),
        ("22:60", "07:00"),
    ],
)
def test_quiet_hours_fail_open(start: str | None, end: str | None) -> None:
    """Half set, start = end, or a bound that is no ``HH:MM``: no quiet hours, never a mute."""
    for hour in range(24):
        assert not in_quiet_hours(start, end, ZURICH, _utc(2026, 10, 13, hour, 30))


def test_now_is_aware() -> None:
    with pytest.raises(ValueError, match="aware"):
        in_quiet_hours("22:00", "07:00", ZURICH, datetime(2026, 10, 13, 23, 0))


# ── the digest ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "weekly", "monthly"),
    [
        (date(2026, 10, 12), "2026-W42", "2026-10"),
        (date(2026, 10, 18), "2026-W42", "2026-10"),
        (date(2026, 10, 31), "2026-W44", "2026-10"),
        (date(2026, 11, 1), "2026-W44", "2026-11"),
        # Across the ISO year's end: 2026 has 53 weeks, and 2027-01-01 is in its last.
        (date(2026, 12, 28), "2026-W53", "2026-12"),
        (date(2026, 12, 31), "2026-W53", "2026-12"),
        (date(2027, 1, 1), "2026-W53", "2027-01"),
        (date(2027, 1, 3), "2026-W53", "2027-01"),
        (date(2027, 1, 4), "2027-W01", "2027-01"),
    ],
)
def test_the_period_key(day: date, weekly: str, monthly: str) -> None:
    assert digest_period("weekly", day) == weekly
    assert digest_period("monthly", day) == monthly
    assert len(weekly) <= 16 and len(monthly) <= 16, "fits the marker's VARCHAR(16)"


def test_an_unknown_cadence_is_refused() -> None:
    with pytest.raises(ValueError, match="cadence"):
        digest_period("daily", date(2026, 10, 13))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cadence"):
        digest_due("daily", ZURICH, _utc(2026, 10, 12, 6), sent=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (_utc(2026, 10, 12, 6), "2026-W42"),  # Monday
        (_utc(2026, 10, 13, 6), "2026-W42"),  # a missed Monday: Tuesday is right
        (_utc(2026, 10, 15, 6), "2026-W42"),  # Thursday: 3 days in, the last
        (_utc(2026, 10, 16, 6), None),  # Friday: too late, wait for the next week
        (_utc(2026, 10, 18, 6), None),  # Sunday
        (_utc(2026, 10, 11, 23, 30), "2026-W42"),  # Sunday in UTC, Monday 01:30 in Zurich
        (_utc(2026, 12, 28, 6), "2026-W53"),
        (_utc(2027, 1, 1, 6), None),  # Friday of 2026-W53
        (_utc(2027, 1, 4, 6), "2027-W01"),
    ],
)
def test_the_weekly_digest_is_due_from_monday_to_thursday(now: datetime, due: str | None) -> None:
    assert digest_due("weekly", ZURICH, now, sent=None) == due


@pytest.mark.parametrize(
    ("now", "due"),
    [
        (_utc(2026, 11, 1, 6), "2026-11"),
        (_utc(2026, 11, 4, 6), "2026-11"),
        (_utc(2026, 11, 5, 6), None),
        (_utc(2026, 10, 31, 23, 30), "2026-11"),  # 00:30 on the 1st in Zurich
        (_utc(2026, 10, 31, 22, 30), None),  # 23:30 on the 31st in Zurich
        (_utc(2027, 1, 1, 6), "2027-01"),
        (_utc(2026, 2, 28, 6), None),
        (_utc(2026, 3, 1, 6), "2026-03"),
    ],
)
def test_the_monthly_digest_is_due_from_the_1st_to_the_4th(now: datetime, due: str | None) -> None:
    assert digest_due("monthly", ZURICH, now, sent=None) == due


def test_a_period_already_sent_is_not_due_again() -> None:
    """A doubled run repeats nothing; a missed run doesn't skip the period."""
    monday = _utc(2026, 10, 12, 6)
    assert digest_due("weekly", ZURICH, monday, sent="2026-W42") is None
    assert digest_due("weekly", ZURICH, monday + timedelta(days=2), sent="2026-W42") is None
    assert digest_due("weekly", ZURICH, monday, sent="2026-W41") == "2026-W42"
    assert digest_due("monthly", ZURICH, _utc(2026, 11, 2, 6), sent="2026-11") is None
    assert digest_due("monthly", ZURICH, _utc(2026, 11, 2, 6), sent="2026-W44") == "2026-11", "cadence changed"


def test_the_late_limit_is_the_callers_to_narrow() -> None:
    tuesday = _utc(2026, 10, 13, 6)
    assert digest_due("weekly", ZURICH, tuesday, sent=None, late_days=0) is None
    assert digest_due("weekly", ZURICH, tuesday, sent=None, late_days=1) == "2026-W42"
    with pytest.raises(ValueError, match="late_days"):
        digest_due("weekly", ZURICH, tuesday, sent=None, late_days=-1)
    with pytest.raises(ValueError, match="aware"):
        digest_due("weekly", ZURICH, datetime(2026, 10, 12, 6), sent=None)


def test_the_digest_tag_is_the_period_key() -> None:
    assert digest_tag("2026-W42") == "digest:2026-W42"
    assert digest_tag(digest_period("monthly", date(2026, 10, 1))) == "digest:2026-10"
    with pytest.raises(ValueError, match="names its period"):
        digest_tag("")
