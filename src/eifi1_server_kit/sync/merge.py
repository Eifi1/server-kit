"""The merge rule of ``/sync`` (``docs/local-first-harmonization.md`` §1.5 D2 in
``Eifi1/ui-kit``), as pure functions over instants.

* **Upserts** are whole-row Last-Write-Wins on the device's edit time: the server keeps its
  copy only when it is **strictly newer** (equal keeps the incoming row), and echoes the
  loser in the same round (keksdose ``sync_service._merge_lww``).
* A stamp **from the future** is clamped to the server's now (tablets' clocks drift), and a
  change **without** a stamp (an older client, or a REST write) counts as server-now.
* A **naive** stamp is read as UTC, as keksdose's tz-normalising does: SQLite hands back naive
  values even from ``DateTime(timezone=True)`` columns, and kastlan's columns are naive.
* **Deletes are not LWW**: a pushed delete tombstones unconditionally, stamped server-now,
  so an older offline delete beats a newer edit (:func:`merge_delete`).
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["as_utc", "clamp_stamp", "merge_delete", "merge_lww"]


def as_utc(value: datetime) -> datetime:
    """Aware and in UTC: a naive value is read as UTC, an aware one converted."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def clamp_stamp(stamp: datetime | None, *, now: datetime) -> datetime:
    """The edit time the server stores for an incoming change, aware and in UTC: ``stamp``,
    or ``now`` when there is none (an older client falls back to server-now) or when it
    lies in the future (a drifting clock never wins every later conflict)."""
    moment = as_utc(now)
    if stamp is None:
        return moment
    return min(as_utc(stamp), moment)


def merge_lww(existing_updated: datetime | None, incoming_updated: datetime | None, *, now: datetime) -> bool:
    """Whether an incoming upsert **wins** over the server's row: ``False`` only when the
    server's ``updated_at`` is strictly newer than the incoming stamp (after
    :func:`clamp_stamp`). A row without a stamp on the server always loses. The winner is
    written whole, with ``updated_at = clamp_stamp(incoming_updated, now=now)``; a loser is
    echoed back to the device as the server's row in the same round."""
    if existing_updated is None:
        return True
    return not as_utc(existing_updated) > clamp_stamp(incoming_updated, now=now)


def merge_delete(existing_updated: datetime | None, incoming_updated: datetime | None, *, now: datetime) -> bool:
    """Whether a pushed delete tombstones the server's row: **always** (D2, keksdose
    ``sync_service.py:1129-1180``). No stamp is compared, so an older offline delete beats a
    newer edit, which is accepted. The tombstone is stamped ``now`` and moves ``updated_at``
    with ``deleted_at`` (:func:`~eifi1_server_kit.sync.mixin.tombstone`), so an older upsert
    cannot revive the row. The arguments are taken so a caller reads both rules alike."""
    del existing_updated, incoming_updated, now
    return True
