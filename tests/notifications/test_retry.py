"""A notification mail that failed to send: the window, the event key, the marker (§9.4)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest

from eifi1_server_kit.mail import MailMessage
from eifi1_server_kit.notifications import (
    MAIL_EVENT_PATTERN,
    MAIL_LATE_DAYS,
    MAIL_MARKER_RETENTION,
    MailMarker,
    mail_due,
    mail_event,
)

#: kastlan's event: company 42's invoices that became overdue on 13.10.
DAY = date(2026, 10, 13)
NEXT = date(2026, 10, 14)
UUID = "0b9f3c1e-5d2a-4f7b-9c8d-1e2f3a4b5c6d"


def _days(count: int) -> date:
    return DAY + timedelta(days=count)


def test_the_constants() -> None:
    assert MAIL_LATE_DAYS == 3
    assert timedelta(days=30) == MAIL_MARKER_RETENTION
    assert MAIL_EVENT_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$"


# ── the window ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("offset", "due"), [(-1, False), (0, True), (1, True), (3, True), (4, False), (30, False)])
def test_a_jobs_mail_is_owed_from_its_day_until_three_days_after(offset: int, due: bool) -> None:
    """Day 0 to day 3, so four scheduled runs at most; a future event is not due yet."""
    assert mail_due(DAY, _days(offset), sent=False) is due


@pytest.mark.parametrize("offset", [-1, 0, 1, 3, 4])
def test_a_sent_mail_is_never_due(offset: int) -> None:
    assert mail_due(DAY, _days(offset), sent=True) is False


def test_the_window_is_the_callers_to_set() -> None:
    assert mail_due(DAY, DAY, sent=False, late_days=0)
    assert not mail_due(DAY, _days(1), sent=False, late_days=0)
    assert mail_due(DAY, _days(7), sent=False, late_days=7)
    with pytest.raises(ValueError, match="late_days is 0 or more: -1"):
        mail_due(DAY, DAY, sent=False, late_days=-1)


def test_the_window_takes_days_not_datetimes() -> None:
    """A datetime's time would shift the window by a run."""
    now = datetime(2026, 10, 14, 0, 10, tzinfo=UTC)
    with pytest.raises(TypeError, match="today is a date, not a datetime"):
        mail_due(DAY, now, sent=False)
    with pytest.raises(TypeError, match="the event's day is a date, not a datetime"):
        mail_due(now, NEXT, sent=False)


# ── the event key ────────────────────────────────────────────────────────────


def test_mail_event_joins_the_parts_with_a_date_as_iso() -> None:
    assert mail_event(42, DAY) == "42:2026-10-13", "kastlan: <company_id>:<first overdue day>"
    assert mail_event("company", 42, "2026-W42") == "company:42:2026-W42"
    assert mail_event(UUID) == UUID
    assert mail_event("a.b_c-d:e") == "a.b_c-d:e"
    assert mail_event("x" * 64) == "x" * 64


@pytest.mark.parametrize(
    "parts",
    [(), ("a b",), (42, "Zürich"), ("x" * 65,), (40 * "x", 24 * "y"), (-1,), ("_a",), ("a\n",), ("a/b",)],
)
def test_mail_event_refuses_a_key_outside_the_pattern(parts: tuple[str | int, ...]) -> None:
    with pytest.raises(ValueError, match="an event key matches"):
        mail_event(*parts)


@pytest.mark.parametrize("parts", [("",), (42, ""), (42, "", DAY)])
def test_mail_event_refuses_an_empty_part(parts: tuple[str | int | date, ...]) -> None:
    """``42::2026-10-13`` would match the pattern: an empty part is refused on its own."""
    with pytest.raises(ValueError, match="never empty"):
        mail_event(*parts)


def test_mail_event_takes_a_day_not_a_datetime() -> None:
    with pytest.raises(TypeError, match="an event key's day is a date, not a datetime"):
        mail_event(42, datetime(2026, 10, 13, 0, 10, tzinfo=UTC))


# ── the marker and its key ───────────────────────────────────────────────────


def test_the_idempotency_key_is_per_recipient_event_and_day() -> None:
    marker = MailMarker(17, "invoices_overdue", mail_event(42, DAY))
    assert marker.idempotency_key(NEXT) == "invoices_overdue:42:2026-10-13:17:2026-10-14"
    assert marker.idempotency_key(NEXT) == marker.idempotency_key(NEXT), "two runs on one day: one key"
    assert marker.idempotency_key(_days(2)) != marker.idempotency_key(NEXT), "the next day's try is a new key"
    assert MailMarker(18, "invoices_overdue", "42:2026-10-13").idempotency_key(NEXT) != marker.idempotency_key(NEXT)
    assert MailMarker(17, "invoices_overdue", "43:2026-10-13").idempotency_key(NEXT) != marker.idempotency_key(NEXT)
    assert MailMarker(UUID, "share_granted", "p.7").idempotency_key(NEXT) == f"share_granted:p.7:{UUID}:2026-10-14"


def test_the_longest_key_is_one_resend_takes() -> None:
    longest = MailMarker("u" * 64, "t" * 40, "e" * 64).idempotency_key(NEXT)
    assert len(longest) == 181
    assert MailMessage(subject="s", text="t", idempotency_key=longest).idempotency_key == longest


def test_the_marker_is_a_frozen_hashable_row() -> None:
    marker = MailMarker(17, "invoices_overdue", "42:2026-10-13")
    assert marker in {MailMarker(17, "invoices_overdue", "42:2026-10-13")}
    assert marker != MailMarker("17", "invoices_overdue", "42:2026-10-13"), "the app's key type, as stored"
    with pytest.raises(FrozenInstanceError):
        marker.event = "43:2026-10-13"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("user_id", "type_id", "event", "words"),
    [
        (17, "Invoices", "42", "type id"),
        (17, "x", "42", "type id"),
        (17, "invoices_overdue\n", "42", "type id"),
        (17, "invoices_overdue", "", "event key"),
        (17, "invoices_overdue", "a b", "event key"),
        (17, "invoices_overdue", "x" * 65, "event key"),
        ("", "invoices_overdue", "42", "user id"),
        ("1:42", "invoices_overdue", "42", "user id"),
        ("a b", "invoices_overdue", "42", "user id"),
        ("u" * 65, "invoices_overdue", "42", "user id"),
    ],
)
def test_the_marker_refuses_what_would_break_its_key(user_id: int | str, type_id: str, event: str, words: str) -> None:
    """A colon in a user id would let two (event, user) pairs share one key — and Resend
    would answer the second person's mail from the first's."""
    with pytest.raises(ValueError, match=words):
        MailMarker(user_id, type_id, event)


def test_the_key_takes_a_day_not_a_datetime() -> None:
    with pytest.raises(TypeError, match="the day of the try is a date, not a datetime"):
        MailMarker(17, "invoices_overdue", "42:2026-10-13").idempotency_key(datetime(2026, 10, 14, tzinfo=UTC))
