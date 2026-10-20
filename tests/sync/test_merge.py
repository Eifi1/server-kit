"""The merge rule (local-first §1.5 D2): LWW on the edit time, deletes always tombstone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from eifi1_server_kit.sync import (
    CLOCK_SKEW_TOLERANCE,
    MAX_CLOCK_SKEW,
    as_utc,
    clamp_stamp,
    clock_skew,
    correct_stamp,
    merge_delete,
    merge_lww,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
NOON = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
MORNING = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


def test_the_newer_edit_wins_and_the_older_loses() -> None:
    assert merge_lww(MORNING, NOON, now=NOW) is True
    # The morning's offline edit synced at night does not overwrite the noon edit.
    assert merge_lww(NOON, MORNING, now=NOW) is False


def test_an_equal_stamp_keeps_the_incoming_row() -> None:
    """The server's copy is kept only when STRICTLY newer (keksdose ``_merge_lww``)."""
    assert merge_lww(NOON, NOON, now=NOW) is True


def test_a_future_stamp_is_clamped_to_now() -> None:
    future = NOW + timedelta(days=3)
    assert clamp_stamp(future, now=NOW) == NOW
    assert clamp_stamp(MORNING, now=NOW) == MORNING
    # A server row stamped in the future (a clock that drifted before) beats a clamped write.
    assert merge_lww(NOW + timedelta(hours=1), future, now=NOW) is False


def test_no_stamp_counts_as_now() -> None:
    """An older client sends none: server-now, for one release (D1, D2)."""
    assert clamp_stamp(None, now=NOW) == NOW
    assert merge_lww(NOON, None, now=NOW) is True
    assert merge_lww(None, MORNING, now=NOW) is True
    assert merge_lww(None, None, now=NOW) is True


def test_naive_reads_as_utc_against_aware() -> None:
    """SQLite and kastlan's columns hand back naive values."""
    naive_noon = datetime(2026, 10, 19, 11, 0)
    assert as_utc(naive_noon) == NOON and as_utc(naive_noon).tzinfo is UTC
    assert merge_lww(naive_noon, MORNING, now=NOW) is False
    assert merge_lww(naive_noon, NOW, now=datetime(2026, 10, 19, 12, 0)) is True
    plus_two = timezone(timedelta(hours=2))
    assert as_utc(datetime(2026, 10, 19, 13, 0, tzinfo=plus_two)) == NOON
    assert clamp_stamp(datetime(2026, 10, 19, 13, 0, tzinfo=plus_two), now=NOW).tzinfo is UTC


def test_a_delete_always_tombstones() -> None:
    """Not LWW: an older offline delete beats a newer edit (D2)."""
    assert merge_delete(NOON, MORNING, now=NOW) is True
    assert merge_delete(None, None, now=NOW) is True


# ---- The device clock's skew (0.16.0, local-first §1.5 D2 amendment, round 0.46) ----------


def test_a_slow_clocks_edit_made_before_a_colleagues_wins_over_it() -> None:
    """kastlan's handover tablets: tablet A runs 20 minutes SLOW. Its edit at 11:30 (server
    time) reads 11:10 on the tablet; colleague B edits the same row at 11:20 on a correct
    clock and syncs first. A syncs at 12:00 server time, its clock reading 11:40. In 0.15.0
    B's 11:20 beat A's 11:10: A's later edit was replaced. Corrected, A's edit is 11:30."""
    slow_edit = datetime(2026, 10, 19, 11, 10, tzinfo=UTC)
    colleague = datetime(2026, 10, 19, 11, 20, tzinfo=UTC)
    client_now = datetime(2026, 10, 19, 11, 40, tzinfo=UTC)
    skew = clock_skew(client_now, now=NOW)
    assert skew == timedelta(minutes=20)
    assert merge_lww(colleague, slow_edit, now=NOW) is False  # 0.15.0: the bug, still the default
    assert merge_lww(colleague, slow_edit, now=NOW, skew=skew) is True
    assert clamp_stamp(slow_edit, now=NOW, skew=skew) == datetime(2026, 10, 19, 11, 30, tzinfo=UTC)


def test_a_slow_clocks_edit_made_after_a_colleagues_still_loses() -> None:
    """The correction moves A's stamp to the true moment, not past every other edit."""
    slow_edit = datetime(2026, 10, 19, 10, 50, tzinfo=UTC)  # 11:10 server time
    colleague = datetime(2026, 10, 19, 11, 20, tzinfo=UTC)
    skew = clock_skew(datetime(2026, 10, 19, 11, 40, tzinfo=UTC), now=NOW)
    assert merge_lww(colleague, slow_edit, now=NOW, skew=skew) is False


def test_a_fast_clock_no_longer_wins() -> None:
    """Tablet A runs 30 minutes FAST: its edit at 11:10 server time reads 11:40, inside the
    clamp, and beat B's real 11:20 edit. Corrected, it is 11:10 and loses."""
    fast_edit = datetime(2026, 10, 19, 11, 40, tzinfo=UTC)
    colleague = datetime(2026, 10, 19, 11, 20, tzinfo=UTC)
    skew = clock_skew(NOW + timedelta(minutes=30), now=NOW)
    assert skew == -timedelta(minutes=30)
    assert merge_lww(colleague, fast_edit, now=NOW) is True  # 0.15.0
    assert merge_lww(colleague, fast_edit, now=NOW, skew=skew) is False
    assert clamp_stamp(fast_edit, now=NOW, skew=skew) == datetime(2026, 10, 19, 11, 10, tzinfo=UTC)


def test_without_client_now_it_is_the_old_behaviour() -> None:
    """An older client sends no ``client_now``: no correction, the clamp alone (0.15.0)."""
    assert clock_skew(None, now=NOW) == timedelta(0)
    future = NOW + timedelta(days=3)
    assert clamp_stamp(future, now=NOW, skew=clock_skew(None, now=NOW)) == NOW
    assert merge_lww(NOON, MORNING, now=NOW, skew=clock_skew(None, now=NOW)) is False


def test_the_clamp_still_applies_after_the_correction() -> None:
    """A stamp shifted past the server's now is clamped to it; no stamp is still now."""
    skew = clock_skew(NOW - timedelta(hours=2), now=NOW)
    assert clamp_stamp(NOW - timedelta(minutes=30), now=NOW, skew=skew) == NOW
    assert clamp_stamp(None, now=NOW, skew=skew) == NOW
    assert merge_lww(None, MORNING, now=NOW, skew=skew) is True


def test_latency_sized_skew_is_ignored() -> None:
    """A correct clock reads a little behind the server by the time the push lands: under
    :data:`CLOCK_SKEW_TOLERANCE` nothing moves, so a correct device's stamps are stored as sent."""
    assert clock_skew(NOW - CLOCK_SKEW_TOLERANCE, now=NOW) == timedelta(0)
    assert clock_skew(NOW + timedelta(milliseconds=300), now=NOW) == timedelta(0)
    assert clock_skew(NOW - CLOCK_SKEW_TOLERANCE - timedelta(seconds=1), now=NOW) == timedelta(seconds=3)


def test_an_absurd_skew_is_capped() -> None:
    """A clock reset to 1970, or years ahead, is corrected by :data:`MAX_CLOCK_SKEW` at most:
    past that the device's clock likely changed between the edit and the push."""
    assert timedelta(hours=24) == MAX_CLOCK_SKEW
    assert clock_skew(datetime(1970, 1, 1, tzinfo=UTC), now=NOW) == MAX_CLOCK_SKEW
    assert clock_skew(NOW + timedelta(days=400), now=NOW) == -MAX_CLOCK_SKEW
    assert clock_skew(NOW - timedelta(hours=23), now=NOW) == timedelta(hours=23)


def test_naive_and_offset_values_are_read_as_the_module_does() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert clock_skew(datetime(2026, 10, 19, 13, 40, tzinfo=plus_two), now=NOW) == timedelta(minutes=20)
    assert clock_skew(datetime(2026, 10, 19, 11, 40), now=datetime(2026, 10, 19, 12, 0)) == timedelta(minutes=20)
    corrected = correct_stamp(datetime(2026, 10, 19, 13, 0, tzinfo=plus_two), skew=timedelta(minutes=5))
    assert corrected == datetime(2026, 10, 19, 11, 5, tzinfo=UTC) and corrected.tzinfo is UTC
    assert correct_stamp(datetime(2026, 10, 19, 11, 0), skew=timedelta(0)) == NOON


@pytest.mark.parametrize(
    ("stamp", "skew", "edge"),
    [
        (datetime.max.replace(tzinfo=UTC), MAX_CLOCK_SKEW, datetime.max.replace(tzinfo=UTC)),
        (datetime.min.replace(tzinfo=UTC), -MAX_CLOCK_SKEW, datetime.min.replace(tzinfo=UTC)),
    ],
)
def test_a_stamp_at_the_calendars_edge_saturates(stamp: datetime, skew: timedelta, edge: datetime) -> None:
    """A wire-valid stamp in year 1 or 9999 does not raise: the shift stops at the edge."""
    assert correct_stamp(stamp, skew=skew) == edge
    assert clamp_stamp(stamp, now=NOW, skew=skew) == min(edge, NOW)
