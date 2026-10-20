"""Quiet hours, the zone and the digest's period (notifications §2.4–§2.6, §3.4).

* **Quiet hours** are a window of local ``HH:MM`` times in the account's zone, which may
  cross midnight (22:00–07:00). They **fail open**: a half-set window, a time that is no
  ``HH:MM``, or start = end is no quiet hours at all, never a silently muted account
  (keksdose ``domain/services/notification_preferences_service.py:154-181``,
  ``should_push_now``, which answers the opposite question).
* **The zone** is the account's (§10.3, 0.16, reversing decision 6; amended 2026-10-20,
  Marcel: automatic only, no pick): the zone the last device reported, else the app's,
  ``Europe/Zurich`` in all three (:func:`effective_zone`). A name ``zoneinfo`` no longer
  knows (tzdata dropped it) counts as unset. keksdose fell back to UTC (``notification_preferences_service.py:169-174``,
  ``push_notifications.py:1097-1102``); the contract's fallback replaces it. A person's
  or a company's "today" is :func:`local_today`, never the server's ``date.today()``.
* **A held push** waits until :func:`quiet_hours_end` where the app runs the queue
  (§10.4, :mod:`~.queue`).
* **The digest** goes once a period — the ISO week (``2026-W42``, on the ISO year) or the
  month (``2026-10``) of the local date — from the period's first local day until
  :data:`DIGEST_LATE_DAYS` days after it, so a missed run doesn't skip it and a doubled
  run doesn't repeat it (decision 5). The app stores the key it sent (``digest_sent
  VARCHAR(16)`` on its settings row) in the transaction that writes the digest's inbox
  row; an app with no inbox, once the mail's send answered ``True`` (§9.2 decision 9), as
  a job's notification mail writes its marker (:mod:`~.retry`). keksdose sent on the
  Monday or the 1st only, with no marker (``push_notifications.py:1094-1119``).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal, cast
from zoneinfo import ZoneInfo

__all__ = [
    "DEFAULT_ZONE",
    "DIGEST_CADENCES",
    "DIGEST_LATE_DAYS",
    "QUIET_HOURS_PATTERN",
    "DigestCadence",
    "digest_due",
    "digest_period",
    "digest_tag",
    "effective_zone",
    "in_quiet_hours",
    "is_known_zone",
    "local_today",
    "quiet_hours_end",
    "resolve_zone",
]

#: A quiet-hours bound: local ``HH:MM``, 00:00–23:59 (keksdose ``schemas/push.py:106-107``).
QUIET_HOURS_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
#: The zone for an account that has none, or one ``zoneinfo`` no longer knows (decision 6):
#: the zone both billing notices are already written in.
DEFAULT_ZONE = "Europe/Zurich"
#: How often the digest goes: on Monday, or on the 1st, in the account's zone.
DigestCadence = Literal["weekly", "monthly"]
#: Every :data:`DigestCadence`, the default first.
DIGEST_CADENCES: tuple[DigestCadence, ...] = ("weekly", "monthly")
#: How many days after the period's first local day a digest is still due (decision 5): a
#: Tuesday digest after a missed Monday is right, its content is the view as of that day.
DIGEST_LATE_DAYS = 3

_HHMM = re.compile(QUIET_HOURS_PATTERN)
_DIGEST_PREFIX = "digest:"


def is_known_zone(name: str | None) -> bool:
    """Whether ``zoneinfo`` knows ``name`` — the update's check on ``timezone``.

    ``Europe/Zurich`` and ``UTC`` are; an empty name, a region alone (``Europe``), a path
    (``../etc/passwd``) and ``None`` are not.
    """
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except ValueError, KeyError, OSError:
        # ZoneInfoNotFoundError is a KeyError; a path or a name ending in "/" is a ValueError.
        return False
    return True


def resolve_zone(name: str | None, fallback: str = DEFAULT_ZONE) -> ZoneInfo:
    """The zone quiet hours and the digest run in: ``name``, or ``fallback`` when it is
    unset or no longer resolves (§3.4) — never UTC, never "no quiet hours".

    ``fallback`` is the app's zone and must resolve; a typo there is a
    ``ZoneInfoNotFoundError`` on the first call.
    """
    return ZoneInfo(name) if name and is_known_zone(name) else ZoneInfo(fallback)


def effective_zone(device_timezone: str | None, fallback: str = DEFAULT_ZONE) -> ZoneInfo:
    """The account's zone (§10.3, 0.16; amended 2026-10-20, Marcel: automatic only): the
    zone the last device reported (``device_timezone``), else ``fallback`` — the app's
    zone, ``Europe/Zurich``. There is no pick: the zone always follows the device last
    used. A name ``zoneinfo`` no longer knows counts as unset.

    It is the zone of everything the server writes for ONE person: quiet hours, the digest
    day, the dates in a push, a notification or account mail, a billing notice to a
    person, and "today" of a person's own reminders. A company's documents and the jobs'
    run times keep the app's zone (:func:`local_today` with it)::

        zone = effective_zone(user.device_timezone)
    """
    return resolve_zone(device_timezone, fallback)


def local_today(zone: tzinfo, now: datetime) -> date:
    """The local date of ``now`` in ``zone`` (§10.3, 0.16): a person's "today" with
    :func:`effective_zone`, a company's with the app's zone (``resolve_zone(None)``). At
    22:30 UTC in summer it is already tomorrow in Zurich, where ``date.today()`` on a UTC
    server still answers today. ``now`` is aware."""
    _require_aware(now)
    return now.astimezone(zone).date()


def _minutes(value: str | None) -> int | None:
    """``"HH:MM"`` → minutes since local midnight; ``None`` for anything else."""
    if value is None or not _HHMM.fullmatch(value):
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _require_aware(now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError("now is an aware datetime (datetime.now(UTC)), not a naive one")


def in_quiet_hours(start: str | None, end: str | None, zone: tzinfo, now: datetime) -> bool:
    """Whether ``now`` falls inside the window ``start``–``end`` in ``zone`` (§2.4).

    ``start`` is inside, ``end`` is not; a window with ``start`` after ``end`` crosses
    midnight. **Fails open:** a half-set window, a bound that is no ``HH:MM`` or
    ``start == end`` answer ``False``. ``now`` is aware (a naive one is a ``ValueError``);
    ``zone`` is :func:`resolve_zone`'s answer. On a day the clocks change, the window is
    read off the local wall clock: a window inside the skipped hour never happens that
    night, and one inside the repeated hour holds both times round.

    Only a ``quiet`` type's push is held: the inbox row is written and e-mail goes as
    usual (:func:`~.delivery.plan_delivery`). Where the app runs the queue (0.16), the
    push waits until :func:`quiet_hours_end` (:meth:`~.queue.QueueService.hold_push`);
    elsewhere it is dropped.
    """
    _require_aware(now)
    first, last = _minutes(start), _minutes(end)
    if first is None or last is None or first == last:
        return False
    local = now.astimezone(zone)
    minute = local.hour * 60 + local.minute
    if first < last:
        return first <= minute < last
    return minute >= first or minute < last


def _wall(instant: datetime, zone: tzinfo) -> datetime:
    """``instant``'s local wall clock in ``zone``, naive."""
    return instant.astimezone(zone).replace(tzinfo=None)


def _first_reaching(wall: datetime, zone: tzinfo, now: datetime) -> datetime:
    """The first instant after ``now``, in UTC, whose wall clock in ``zone`` is ``wall``
    or later.

    An existing time is itself: on the day the clocks go back, its first occurrence, or
    its second when ``now`` is already past the first. A time the clocks skip (02:30 on
    the spring day) is the moment they jump, found to the minute between its two readings.
    """
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if _wall(candidate, zone) == wall and candidate > now:
            return candidate
    # In a gap fold=0 reads the wall clock with the offset before the jump (later in UTC)
    # and fold=1 with the one after (earlier): the jump lies between them.
    low = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    high = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    while high - low > timedelta(minutes=1):
        middle = low + (high - low) / 2
        if _wall(middle, zone) >= wall:
            high = middle
        else:
            low = middle
    return high


def quiet_hours_end(start: str | None, end: str | None, zone: tzinfo, now: datetime) -> datetime:
    """When the quiet hours ``now`` falls in end, in UTC (§10.4, 0.16): a held push's
    ``not_before``. ``now`` itself when it falls in none (:func:`in_quiet_hours`, which
    fails open), so a push is never held for a window that isn't there.

    The end is the next local ``end`` on the wall clock — tomorrow's for a window that
    crosses midnight and started tonight. On the day the clocks skip an hour, an ``end``
    inside it is the moment they jump (03:00 for 02:30 in Zurich); on the day they go
    back, an ``end`` inside the repeated hour is its next occurrence, and a window quiet
    again the second time round is held again at the flush, which re-runs the plan.
    ``now`` is aware; ``zone`` is :func:`effective_zone`'s answer.
    """
    if not in_quiet_hours(start, end, zone, now):
        return now
    last = cast(int, _minutes(end))
    local = now.astimezone(zone)
    day = local.date() if local.hour * 60 + local.minute < last else local.date() + timedelta(days=1)
    return _first_reaching(datetime.combine(day, time(last // 60, last % 60)), zone, now)


def _check_cadence(cadence: str) -> None:
    if cadence not in DIGEST_CADENCES:
        raise ValueError(f"a digest cadence is one of {', '.join(DIGEST_CADENCES)}: {cadence!r}")


def digest_period(cadence: DigestCadence, day: date) -> str:
    """The period ``day`` belongs to: its ISO week, ``2026-W42`` (on the ISO year, so
    2027-01-01 is ``2026-W53``), or its month, ``2026-10``. At most 8 characters, so the
    marker fits ``VARCHAR(16)``."""
    _check_cadence(cadence)
    if cadence == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year:04d}-W{week:02d}"
    return f"{day.year:04d}-{day.month:02d}"


def digest_due(
    cadence: DigestCadence,
    zone: tzinfo,
    now: datetime,
    *,
    sent: str | None,
    late_days: int = DIGEST_LATE_DAYS,
) -> str | None:
    """The period key while the digest is due, else ``None`` (§3.4).

    Due while ``sent`` — the key the app stored when it last sent one — is not this
    period's, and the local date (``now`` in ``zone``) is no more than ``late_days`` days
    into the period: Monday to Thursday for a week, the 1st to the 4th for a month. Store
    the answer as the new ``sent`` in the transaction that writes the digest's inbox row,
    whether or not quiet hours let the push go: a marker set on the push would write a new
    row every day of the grace. An app with no inbox stores it once the mail's send
    answered ``True``, so a failed send is tried again the next day of the grace (§9.2
    decision 9). ``now`` is aware.
    """
    _require_aware(now)
    if late_days < 0:
        raise ValueError(f"late_days is 0 or more: {late_days}")
    today = now.astimezone(zone).date()
    period = digest_period(cadence, today)
    days_in = today.isoweekday() - 1 if cadence == "weekly" else today.day - 1
    if period == sent or days_in > late_days:
        return None
    return period


def digest_tag(period: str) -> str:
    """The digest push's ``tag``: ``digest:2026-W42``, so a late copy of the same period
    replaces the earlier notification on the device, and the next period's doesn't."""
    if not period:
        raise ValueError("a digest tag names its period: digest_tag(digest_period(…))")
    return _DIGEST_PREFIX + period
