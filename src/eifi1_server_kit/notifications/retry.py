"""A notification mail that failed to send: owed for 3 days, one marker per recipient (§9.4).

``docs/notifications-harmonization.md`` §9.2 decisions 5–7 in ``Eifi1/ui-kit``. Billing's
notices (§14.7) and the digest (§3.4) already work this way: the job reads what is owed
from the state, sends, writes a marker only when the send answered ``True``, and commits
per row; a window bounds the retries.

* **A job's mail is owed, a request's is tried once** (decision 5). A job's event happens
  with nobody watching, and the mail may be the only word of it (kastlan's overdue
  invoices): it is owed per recipient per event from the event's day until
  :data:`MAIL_LATE_DAYS` days after (:func:`mail_due`). A request's mail goes once, after
  the commit, with the transport's own retry, and a failure is logged. Push is never
  retried: its inbox row stays.
* **The event key is the app's** (:func:`mail_event`): stable, with no personal data and
  no sealed content — ids and days (kastlan: ``<company_id>:<first overdue day>``).
* **The marker** (:class:`MailMarker`) is per recipient, type and event, written ``ON
  CONFLICT DO NOTHING`` only when the send answered ``True``. No count of tries is
  stored: only the scheduled run sends, once a run, so the window holds it to four tries.
  A failed try writes nothing. Markers are kept :data:`MAIL_MARKER_RETENTION`, so an
  operator can tell whether a person was sent a mail; the logs never name the recipient.
* **One ``Idempotency-Key`` per recipient, event and day**
  (:meth:`MailMarker.idempotency_key`, on :attr:`~eifi1_server_kit.mail.MailMessage.idempotency_key`):
  two runs on one day send one mail, as Resend answers the second from the first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from eifi1_server_kit.notifications.registry import TYPE_ID_PATTERN

__all__ = ["MAIL_EVENT_PATTERN", "MAIL_LATE_DAYS", "MAIL_MARKER_RETENTION", "MailMarker", "mail_due", "mail_event"]

#: How many days after its event's day a job's notification mail is still owed (decision
#: 5): billing's and the digest's grace (``NOTICE_LATE_LIMIT``, ``DIGEST_LATE_DAYS``).
MAIL_LATE_DAYS = 3
#: How long the app keeps a marker (decision 6); the job that sends deletes older ones.
MAIL_MARKER_RETENTION = timedelta(days=30)
#: An event key: 1–64 letters, digits and ``:._-``, a letter or digit first. It fits the
#: marker's ``event VARCHAR(64)``.
MAIL_EVENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$"

_EVENT = re.compile(MAIL_EVENT_PATTERN)
_TYPE_ID = re.compile(TYPE_ID_PATTERN)
#: A user id given as a string (a UUID): the event's characters but the colon, so the
#: key's parts stay apart — ``type`` and ``day`` have none either, and the event is what
#: is left between them.
_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _day(value: date, what: str) -> date:
    """``value``, refused when it is a ``datetime``: its time would make every run a new
    event, and every try a new key."""
    if isinstance(value, datetime):
        raise TypeError(f"{what} is a date, not a datetime: pass .date() ({value!r})")
    return value


def mail_event(*parts: str | int | date) -> str:
    """An event key from the app's parts, joined with ``:``, a date as ISO (decision 6)::

        mail_event(company.id, day)  # kastlan: "42:2026-10-13"

    The key names the event, never a person or what happened: ids and days, no name, no
    title, no amount. A :class:`ValueError` for an empty part, or a key outside
    :data:`MAIL_EVENT_PATTERN` (a space, an umlaut, over 64 characters, no part at all); a
    :class:`TypeError` for a ``datetime``.
    """
    texts: list[str] = []
    for part in parts:
        text = _day(part, "an event key's day").isoformat() if isinstance(part, date) else str(part)
        if not text:
            raise ValueError("an event key's part is never empty")
        texts.append(text)
    event = ":".join(texts)
    if not _EVENT.fullmatch(event):
        raise ValueError(f"an event key matches {MAIL_EVENT_PATTERN}: {event[:80]!r}")
    return event


@dataclass(frozen=True, slots=True)
class MailMarker:
    """One recipient's mail for one event, sent (decision 6): the row the app writes, and
    the source of the send's ``Idempotency-Key`` (§9.4)::

        marker = MailMarker(user.id, "invoices_overdue", mail_event(company.id, day))
        if not mail_due(day, today, sent=marker in markers):
            continue
        message = replace(message, idempotency_key=marker.idempotency_key(today))
        if await mailer.send(user.email, message, kind=marker.type_id):
            ...  # INSERT (user_id, type_id, event, sent_at) ON CONFLICT DO NOTHING; commit

    A :class:`ValueError` for a ``type_id`` outside
    :data:`~eifi1_server_kit.notifications.TYPE_ID_PATTERN`, an ``event`` outside
    :data:`MAIL_EVENT_PATTERN`, or a string ``user_id`` with a colon, a space or nothing
    in it (a number or a UUID is right).
    """

    user_id: int | str
    type_id: str
    event: str

    def __post_init__(self) -> None:
        if not _TYPE_ID.fullmatch(self.type_id):
            raise ValueError(f"a notification type id matches {TYPE_ID_PATTERN}: {self.type_id!r}")
        if not _EVENT.fullmatch(self.event):
            raise ValueError(f"an event key matches {MAIL_EVENT_PATTERN}: {self.event[:80]!r}")
        if isinstance(self.user_id, str) and not _USER_ID.fullmatch(self.user_id):
            raise ValueError(f"a user id is a number or a key with no colon or space: {self.user_id[:80]!r}")

    def idempotency_key(self, day: date) -> str:
        """``<type>:<event>:<user_id>:<YYYY-MM-DD>`` for the try on ``day`` (decision 7)::

            MailMarker(17, "invoices_overdue", "42:2026-10-13").idempotency_key(date(2026, 10, 14))
            # "invoices_overdue:42:2026-10-13:17:2026-10-14"

        Two runs on one day send one mail: Resend answers the second from the first (it
        keeps a key 24 hours). The next day's try is a new key, so a count that changed in
        between is no conflict at Resend. At most 181 characters, within Resend's 256. A
        :class:`TypeError` for a ``datetime``.
        """
        return f"{self.type_id}:{self.event}:{self.user_id}:{_day(day, 'the day of the try').isoformat()}"


def mail_due(event_day: date, today: date, *, sent: bool, late_days: int = MAIL_LATE_DAYS) -> bool:
    """Whether a job's notification mail for an event is still owed to one recipient
    (decision 5): not ``sent`` (no marker), and ``today`` is 0 to ``late_days`` days after
    ``event_day`` — the event's day and the 3 after it. A future ``event_day`` is not due.

    Both are days in the job's own zone. A ``late_days`` below 0 is a :class:`ValueError`,
    a ``datetime`` a :class:`TypeError`.
    """
    if late_days < 0:
        raise ValueError(f"late_days is 0 or more: {late_days}")
    late = (_day(today, "today") - _day(event_day, "the event's day")).days
    return not sent and 0 <= late <= late_days
