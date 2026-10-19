"""The merge rule (local-first §1.5 D2): LWW on the edit time, deletes always tombstone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from eifi1_server_kit.sync import as_utc, clamp_stamp, merge_delete, merge_lww

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
