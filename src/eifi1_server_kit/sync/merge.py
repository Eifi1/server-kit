"""The merge rule of ``/sync`` (``docs/local-first-harmonization.md`` §1.5 D2 in
``Eifi1/ui-kit``), as pure functions over instants.

* **Upserts** are whole-row Last-Write-Wins on the device's edit time: the server keeps its
  copy only when it is **strictly newer** (equal keeps the incoming row), and echoes the
  loser in the same round (keksdose ``sync_service._merge_lww``).
* A stamp **from the future** is clamped to the server's now (tablets' clocks drift), and a
  change **without** a stamp (an older client, or a REST write) counts as server-now.
* A **naive** stamp is read as UTC, as keksdose's tz-normalising does: SQLite hands back naive
  values even from ``DateTime(timezone=True)`` columns, and kastlan's columns are naive.
* **The device clock's skew is corrected** (0.16.0, the D2 amendment of round 0.46): a push
  that carries ``client_now`` (the device's clock when it sent it) has every stamp shifted by
  ``server_now - client_now`` (:func:`clock_skew`, :func:`correct_stamp`) BEFORE the clamp, so
  a SLOW clock's edits no longer count as older than they were (a colleague's later-arriving
  edit silently replaced them) and a FAST clock's no longer win. This assumes the device's
  clock did not change between the edit and the push. Skew within
  :data:`CLOCK_SKEW_TOLERANCE` is network latency and ignored; the correction is capped at
  :data:`MAX_CLOCK_SKEW`. Without ``client_now`` (an older client) nothing is shifted: the
  clamp alone, as in 0.15.0.
* **Deletes are not LWW**: a pushed delete tombstones unconditionally, stamped server-now,
  so an older offline delete beats a newer edit (:func:`merge_delete`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = [
    "CLOCK_SKEW_TOLERANCE",
    "MAX_CLOCK_SKEW",
    "as_utc",
    "clamp_stamp",
    "clock_skew",
    "correct_stamp",
    "merge_delete",
    "merge_lww",
]

#: Skew up to this much is the push's network latency, not a wrong clock: not corrected, so a
#: correct device's stamps are stored exactly as it sent them.
CLOCK_SKEW_TOLERANCE: Final = timedelta(seconds=2)
#: The largest skew corrected, either way. A clock off by more (reset to 1970, a year
#: ahead) has likely changed between the edit and the push, so the full shift would be a
#: guess: the correction stops here, and the clamp still keeps any stamp out of the future.
#: A day covers a device set to the wrong zone (up to 14 hours) and a week of drift.
MAX_CLOCK_SKEW: Final = timedelta(hours=24)

_NO_SKEW: Final = timedelta(0)
_EARLIEST: Final = datetime.min.replace(tzinfo=UTC)
_LATEST: Final = datetime.max.replace(tzinfo=UTC)


def as_utc(value: datetime) -> datetime:
    """Aware and in UTC: a naive value is read as UTC, an aware one converted."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def clock_skew(client_now: datetime | None, *, now: datetime) -> timedelta:
    """How far the device's clock is behind the server's, once per push: ``now - client_now``
    (positive for a SLOW clock, negative for a FAST one), the shift :func:`correct_stamp`
    adds to every stamp of that push. ``timedelta(0)`` without ``client_now`` (an older
    client: 0.15.0's behaviour) or within :data:`CLOCK_SKEW_TOLERANCE`; capped at
    :data:`MAX_CLOCK_SKEW` either way. ``client_now`` is
    :attr:`~eifi1_server_kit.sync.SyncRequest.client_now`, ``now`` the server's time when it
    handles the push; naive values are read as UTC."""
    if client_now is None:
        return _NO_SKEW
    skew = as_utc(now) - as_utc(client_now)
    if abs(skew) <= CLOCK_SKEW_TOLERANCE:
        return _NO_SKEW
    return max(-MAX_CLOCK_SKEW, min(skew, MAX_CLOCK_SKEW))


def correct_stamp(stamp: datetime, *, skew: timedelta) -> datetime:
    """A device's edit time on the server's clock, aware and in UTC: ``stamp + skew``
    (:func:`clock_skew`). A stamp at the calendar's edge (year 1 or 9999, valid on the wire)
    stops there instead of raising."""
    moment = as_utc(stamp)
    try:
        return moment + skew
    except OverflowError:
        return _EARLIEST if skew < _NO_SKEW else _LATEST


def clamp_stamp(stamp: datetime | None, *, now: datetime, skew: timedelta = _NO_SKEW) -> datetime:
    """The edit time the server stores for an incoming change, aware and in UTC: ``stamp``
    corrected by the push's ``skew`` (:func:`clock_skew`; none by default, as in 0.15.0), or
    ``now`` when there is none (an older client falls back to server-now) or when it lies in
    the future (a drifting clock never wins every later conflict)."""
    moment = as_utc(now)
    if stamp is None:
        return moment
    return min(correct_stamp(stamp, skew=skew), moment)


def merge_lww(
    existing_updated: datetime | None,
    incoming_updated: datetime | None,
    *,
    now: datetime,
    skew: timedelta = _NO_SKEW,
) -> bool:
    """Whether an incoming upsert **wins** over the server's row: ``False`` only when the
    server's ``updated_at`` is strictly newer than the incoming stamp (after
    :func:`clamp_stamp` with the push's ``skew``). A row without a stamp on the server always
    loses. The winner is written whole, with
    ``updated_at = clamp_stamp(incoming_updated, now=now, skew=skew)``; a loser is echoed back
    to the device as the server's row in the same round. The server's own ``updated_at`` was
    corrected when it was stored, so it is not shifted::

        skew = clock_skew(request.client_now, now=now)  # once per push
        for change in request.changes:
            if merge_lww(row.updated_at, change.updated_at, now=now, skew=skew): ...
    """
    if existing_updated is None:
        return True
    return not as_utc(existing_updated) > clamp_stamp(incoming_updated, now=now, skew=skew)


def merge_delete(existing_updated: datetime | None, incoming_updated: datetime | None, *, now: datetime) -> bool:
    """Whether a pushed delete tombstones the server's row: **always** (D2, keksdose
    ``sync_service.py:1129-1180``). No stamp is compared, so an older offline delete beats a
    newer edit, which is accepted. The tombstone is stamped ``now`` and moves ``updated_at``
    with ``deleted_at`` (:func:`~eifi1_server_kit.sync.mixin.tombstone`), so an older upsert
    cannot revive the row. The arguments are taken so a caller reads both rules alike."""
    del existing_updated, incoming_updated, now
    return True
