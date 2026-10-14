"""Quiet hours, the zone and the digest's period (notifications §2.4–§2.6, §3.4).

* **Quiet hours** are a window of local ``HH:MM`` times in the account's zone, which may
  cross midnight (22:00–07:00). They **fail open**: a half-set window, a time that is no
  ``HH:MM``, or start = end is no quiet hours at all, never a silently muted account
  (keksdose ``domain/services/notification_preferences_service.py:154-181``,
  ``should_push_now``, which answers the opposite question).
* **The zone** is the notification settings' ``timezone``, an IANA name. Unset, or a name
  ``zoneinfo`` no longer knows (tzdata dropped it), counts as unset: the app's zone,
  ``Europe/Zurich`` in all three (decision 6). keksdose fell back to UTC
  (``notification_preferences_service.py:169-174``, ``push_notifications.py:1097-1102``);
  the contract's fallback replaces it.
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
from datetime import date, datetime, tzinfo
from typing import Literal
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
    "in_quiet_hours",
    "is_known_zone",
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

    Only a ``quiet`` type's push is held, and a held push is dropped, not delayed: the
    inbox row is written and e-mail goes as usual (:func:`~.delivery.plan_delivery`).
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
