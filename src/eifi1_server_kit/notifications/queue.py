"""The queue: a held push sent when quiet hours end, mails bundled into one (§10.4, 0.16).

Layer 2, opt-in per app (§10.4 Settled): an app turns it on with its first queued type and
runs ONE 15-minute tick job that calls :func:`notifications_tick` (flush, then purge)
beside its own small outboxes — keksdose's renamed product-metrics Cloud Run Job, kastlan's
``notifications-flush`` in ``/ops/jobs``. Kurvenschmiede runs none, and an app without a
flush ignores :attr:`~.registry.NotificationType.bundle`: each mail goes after its commit.

The table is the app's (:class:`NotificationQueue`), one row per item: ``id``,
``user_id`` (FK ``users`` ON DELETE CASCADE, so rows go with the account and its erasure),
``type_id``, ``channel`` (``push`` | ``email``), ``event`` (a
:data:`~.retry.MAIL_EVENT_PATTERN` key), ``payload`` (JSON — never sealed content),
``not_before``, ``created_at``, ``claimed_at``, ``attempts``, ``sent_at``, ``dropped``;
unique ``(user_id, type_id, channel, event)``. Not in the export: deliveries, not the
person's data.

* **Enqueue** in the event's own transaction (:meth:`QueueService.hold_push`,
  :meth:`QueueService.queue_mail`), so a rollback queues nothing; for a bundled type the
  queue is the outbox and ``send_after_commit`` is not needed.
* **A held push** waits until :func:`~.timing.quiet_hours_end`; a type's held pushes for one
  person go as ONE push — the newest's title, the body "+{n} more" when there are several,
  tag ``<type>:held``. One held longer than :data:`~.push.PUSH_TTL` is dropped.
* **A bundled mail** waits until the first item's time plus the type's ``bundle``; later
  items for the same person and type join it, and one mail goes per (person, type) with
  every item (:meth:`FlushPort.send_mail`). One still unsent :data:`~.retry.MAIL_LATE_DAYS`
  days after its ``not_before`` is dropped.
* **At the flush the plan runs again** (:meth:`FlushPort.still_wanted`): the switches,
  ``active``, the device's mutes, and quiet hours, in case the window moved.
* **Idempotency**: the claim commits before the send and ``done`` after; a mail carries
  ``Idempotency-Key = queue:<first row id>:<count>``, so a resend after a lost answer is a
  no-op at Resend, and a push sent twice replaces itself through its tag. A claim older
  than :data:`QUEUE_CLAIM_TIMEOUT` is claimable again; after :data:`QUEUE_MAX_ATTEMPTS`
  the rows are dropped ``"failed"``.
* **Retention**: sent and dropped rows are purged after :data:`QUEUE_RETENTION`.

Not queued: a request's ordinary mail, the digest (it has its marker), service notices,
the test push, kastlan's overdue mail (its markers).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from eifi1_server_kit.notifications.push import PUSH_TTL, PushData, PushPayload
from eifi1_server_kit.notifications.registry import NotificationType
from eifi1_server_kit.notifications.retry import MAIL_EVENT_PATTERN, MAIL_LATE_DAYS, MAIL_MARKER_RETENTION

__all__ = [
    "QUEUE_CLAIM_TIMEOUT",
    "QUEUE_FLUSH_LIMIT",
    "QUEUE_MAX_ATTEMPTS",
    "QUEUE_MORE",
    "QUEUE_RETENTION",
    "FlushPort",
    "FlushResult",
    "NewQueueItem",
    "NotificationQueue",
    "QueueChannel",
    "QueueItem",
    "QueueService",
    "notifications_tick",
]

#: A queued item's channel.
QueueChannel = Literal["push", "email"]
#: A claim older than this is claimable again: the run that took it died between the claim
#: and ``done``.
QUEUE_CLAIM_TIMEOUT = timedelta(minutes=10)
#: Claims after which an item that never went is dropped ``"failed"``.
QUEUE_MAX_ATTEMPTS = 3
#: Sent and dropped rows are purged after this (``MAIL_MARKER_RETENTION``, 30 days).
QUEUE_RETENTION = MAIL_MARKER_RETENTION
#: How many rows one flush claims at most; the next tick takes the rest.
QUEUE_FLUSH_LIMIT = 500
#: A collapsed held push's body when the item gave no words of its own: ``{n}`` is the
#: number of OTHER held items. The app passes the recipient's language at enqueue
#: (:meth:`QueueService.hold_push`'s ``more``).
QUEUE_MORE = "+{n} more"

_EVENT = re.compile(MAIL_EVENT_PATTERN)
_HELD_TAG = "{type}:held"


@dataclass(frozen=True, slots=True)
class NewQueueItem:
    """A row to insert (:meth:`NotificationQueue.enqueue`)."""

    user_id: int
    type_id: str
    channel: QueueChannel
    #: The event's key: the row's identity with the user, type and channel.
    event: str
    #: JSON: a push's ``{title, body, tag, url, inbox, more}``, a mail's words' fields.
    payload: Mapping[str, Any]
    not_before: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One queued row as the store answers it."""

    id: int
    user_id: int
    type_id: str
    channel: QueueChannel
    event: str
    payload: Mapping[str, Any]
    not_before: datetime
    created_at: datetime
    claimed_at: datetime | None = None
    #: How many times it was claimed, this claim included.
    attempts: int = 0
    sent_at: datetime | None = None
    #: Why it was dropped: ``"unwanted"``, ``"expired"``, ``"late"``, ``"failed"``.
    dropped: str | None = None


class NotificationQueue(Protocol):
    """The app's queue table. ``enqueue`` and ``open_bundle`` run in the caller's
    transaction (flush, never commit); the tick's methods — ``claim``, ``done``, ``drop``,
    ``defer``, ``purge`` — each COMMIT, so a claim holds before the send and ``done``
    after it."""

    async def enqueue(self, item: NewQueueItem) -> bool:
        """Insert ``ON CONFLICT (user_id, type_id, channel, event) DO NOTHING``; whether a
        row was added."""

    async def open_bundle(self, user_id: int, type_id: str) -> datetime | None:
        """The ``not_before`` of this person's unsent, unclaimed, undropped mail rows of
        this type — the open bundle — or ``None`` when there is none."""

    async def claim(self, now: datetime, limit: int, reclaim_before: datetime) -> Sequence[QueueItem]:
        """Up to ``limit`` rows not sent, not dropped, ``not_before <= now``, unclaimed or
        claimed before ``reclaim_before`` — ``FOR UPDATE SKIP LOCKED`` — oldest first;
        set ``claimed_at = now`` and ``attempts += 1``, commit, and answer them updated."""

    async def done(self, ids: Sequence[int], sent_at: datetime) -> None:
        """Stamp ``sent_at``; commit."""

    async def drop(self, ids: Sequence[int], reason: str) -> None:
        """Set ``dropped = reason``; commit."""

    async def defer(self, ids: Sequence[int], not_before: datetime) -> None:
        """Still held (the window moved): set ``not_before``, clear ``claimed_at`` and take
        back this claim's attempt; commit."""

    async def purge(self, before: datetime) -> int:
        """Delete the sent or dropped rows created before ``before``; how many; commit."""


class FlushPort(Protocol):
    """The app's delivery at the flush."""

    async def still_wanted(self, user_id: int, type_id: str, channel: QueueChannel) -> bool | datetime:
        """Re-run the plan (:func:`~.delivery.plan_delivery`) for this person, type and
        channel: ``True`` sends, ``False`` drops (``"unwanted"``: the switch went off, the
        account was deactivated), and a datetime holds the push again until then (the
        quiet hours moved: :func:`~.timing.quiet_hours_end`).

        0.16.1: a port whose ``still_wanted`` also takes a keyword ``now: datetime | None =
        None`` gets the flush's own ``now``, to judge quiet hours at the moment the flush
        claimed for (a retried or late tick, a test) instead of the wall clock; a port
        without it is called as before."""

    async def send_push(self, user_id: int, payload: PushPayload) -> bool:
        """Push to this person's devices that don't mute the type
        (:func:`~.delivery.device_receives`); ``True`` once it was handed to the push
        services (a device's own answer is the app's, §2.7), ``False`` to try again."""

    async def send_mail(self, user_id: int, type_id: str, items: Sequence[QueueItem], idempotency_key: str) -> bool:
        """One mail with every item, oldest first — "{n} new comments on reports you
        follow", one line each; one item sends the ordinary mail. Pass
        ``idempotency_key`` as :attr:`~eifi1_server_kit.mail.MailMessage.idempotency_key`.
        Whether the send answered ``True``."""


class FlushResult(BaseModel):
    """What one flush (or tick) did, in rows: ``POST /ops/notifications/flush``'s answer."""

    #: Rows sent.
    sent: int = 0
    #: Rows dropped this run: unwanted, expired, late or failed.
    dropped: int = 0
    #: Rows claimed and not finished: a failed send to try again, a push held again.
    left: int = 0
    #: Finished rows deleted by the tick's purge.
    purged: int = 0


@dataclass(slots=True)
class _Tally:
    sent: int = 0
    dropped: int = 0
    left: int = 0
    groups: dict[tuple[int, str, QueueChannel], list[QueueItem]] = field(default_factory=dict)


def _check_event(event: str) -> str:
    if not _EVENT.fullmatch(event):
        raise ValueError(f"a queue event matches {MAIL_EVENT_PATTERN}: {event!r}")
    return event


class QueueService:
    """The queue's flows over the app's :class:`NotificationQueue` and :class:`FlushPort`::

    service = QueueService(queue, port)
    await service.hold_push(user.id, "scan_done", event, payload, not_before=end, now=now)
    await notifications_tick(service, now)  # every 15 minutes
    """

    def __init__(self, queue: NotificationQueue, port: FlushPort, *, limit: int = QUEUE_FLUSH_LIMIT) -> None:
        if limit < 1:
            raise ValueError(f"a flush claims 1 row or more: {limit}")
        self.queue = queue
        self.port = port
        self.limit = limit

    async def hold_push(
        self,
        user_id: int,
        type_id: str,
        event: str,
        payload: PushPayload,
        *,
        not_before: datetime,
        now: datetime,
        more: str = QUEUE_MORE,
    ) -> bool:
        """Queue a ``quiet`` type's push the plan held (§10.4 rule 2) until ``not_before``
        (:func:`~.timing.quiet_hours_end`), in the event's transaction. ``more`` is the
        collapsed body in the recipient's language, ``{n}`` the number of others. Whether
        a row was added (``False``: this event was already held)."""
        if "{n}" not in more:
            raise ValueError(f"more names the count as {{n}}: {more!r}")
        content = payload.model_dump(mode="json")
        stored = {
            "title": content["title"],
            "body": content["body"],
            "tag": content["tag"],
            "url": content["data"]["url"],
            "inbox": content["data"]["inbox"],
            "more": more,
        }
        return await self.queue.enqueue(
            NewQueueItem(
                user_id=user_id,
                type_id=type_id,
                channel="push",
                event=_check_event(event),
                payload=stored,
                not_before=not_before,
                created_at=now,
            )
        )

    async def queue_mail(
        self,
        notification_type: NotificationType,
        user_id: int,
        event: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> bool:
        """Queue a mail of a bundled type (§10.4 rule 3), in the event's transaction: it
        joins this person's open bundle of the type, or opens one that goes at ``now`` plus
        the type's ``bundle`` (at the next flush for a type with none). ``payload`` is
        what the mail's words need — never sealed content. Whether a row was added."""
        not_before = await self.queue.open_bundle(user_id, notification_type.id)
        if not_before is None:
            not_before = now + (notification_type.bundle or timedelta(0))
        return await self.queue.enqueue(
            NewQueueItem(
                user_id=user_id,
                type_id=notification_type.id,
                channel="email",
                event=_check_event(event),
                payload=dict(payload),
                not_before=not_before,
                created_at=now,
            )
        )

    async def flush(self, now: datetime) -> FlushResult:
        """Send what is due (§10.4): claim, drop what is spent, ask the plan again, send one
        push or one mail per person, type and channel, and mark it."""
        claimed = await self.queue.claim(now, self.limit, now - QUEUE_CLAIM_TIMEOUT)
        tally = _Tally()
        spent: dict[str, list[int]] = {}
        for item in claimed:
            reason = _spent(item, now)
            if reason is not None:
                spent.setdefault(reason, []).append(item.id)
                continue
            tally.groups.setdefault((item.user_id, item.type_id, item.channel), []).append(item)
        for reason, ids in spent.items():
            await self.queue.drop(ids, reason)
            tally.dropped += len(ids)
        for (user_id, type_id, channel), items in tally.groups.items():
            await self._deliver(tally, user_id, type_id, channel, sorted(items, key=_order), now)
        return FlushResult(sent=tally.sent, dropped=tally.dropped, left=tally.left)

    async def _deliver(
        self,
        tally: _Tally,
        user_id: int,
        type_id: str,
        channel: QueueChannel,
        items: list[QueueItem],
        now: datetime,
    ) -> None:
        ids = [item.id for item in items]
        if _takes_now(self.port.still_wanted):
            wanted = await self.port.still_wanted(user_id, type_id, channel, now=now)  # type: ignore[call-arg]
        else:
            wanted = await self.port.still_wanted(user_id, type_id, channel)
        if wanted is False:
            await self.queue.drop(ids, "unwanted")
            tally.dropped += len(ids)
            return
        if isinstance(wanted, datetime):
            await self.queue.defer(ids, wanted)
            tally.left += len(ids)
            return
        if channel == "push":
            sent = await self.port.send_push(user_id, _collapsed(type_id, items))
        else:
            sent = await self.port.send_mail(user_id, type_id, items, f"queue:{items[0].id}:{len(items)}")
        if sent:
            await self.queue.done(ids, now)
            tally.sent += len(ids)
        elif max(item.attempts for item in items) >= QUEUE_MAX_ATTEMPTS:
            await self.queue.drop(ids, "failed")
            tally.dropped += len(ids)
        else:
            tally.left += len(ids)

    async def purge(self, now: datetime) -> int:
        """Delete the sent and dropped rows older than :data:`QUEUE_RETENTION`."""
        return await self.queue.purge(now - QUEUE_RETENTION)


def _takes_now(method: Callable[..., object]) -> bool:
    """Whether a port's ``still_wanted`` takes ``now`` (0.16.1), by keyword or ``**``."""
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (parameter.name == "now" and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for parameter in inspect.signature(method).parameters.values()
    )


def _order(item: QueueItem) -> tuple[datetime, int]:
    return item.created_at, item.id


def _spent(item: QueueItem, now: datetime) -> str | None:
    """Why a claimed item goes no more: claimed too often, a push held past its TTL, a
    mail owed past its window."""
    if item.attempts > QUEUE_MAX_ATTEMPTS:
        return "failed"
    if item.channel == "push" and now - item.created_at > timedelta(seconds=PUSH_TTL):
        return "expired"
    if item.channel == "email" and now - item.not_before > timedelta(days=MAIL_LATE_DAYS):
        return "late"
    return None


def _collapsed(type_id: str, items: list[QueueItem]) -> PushPayload:
    """One push for a type's held items, oldest first: the newest's title and link; its
    body alone, or "+{n} more" when there are several; tag ``<type>:held``."""
    newest = items[-1].payload
    others = len(items) - 1
    body = str(newest["body"]) if others == 0 else str(newest.get("more", QUEUE_MORE)).replace("{n}", str(others))
    return PushPayload(
        title=str(newest["title"]),
        body=body,
        tag=_HELD_TAG.format(type=type_id),
        data=PushData(url=str(newest["url"]), type=type_id, inbox=bool(newest.get("inbox", True))),
    )


async def notifications_tick(service: QueueService, now: datetime) -> FlushResult:
    """The app's 15-minute tick (§10.4 Settled): flush, then purge. Call it from the app's
    own job path — keksdose's tick job, kastlan's ``/ops/jobs/notifications-flush`` — or
    mount ``eifi1_server_kit.notifications.router.notifications_jobs_router``.

    Idempotent, so a job runner may retry it (keksdose's product-metrics job,
    ``--max-retries=1``): a sent row is done, a claimed one waits out
    :data:`QUEUE_CLAIM_TIMEOUT`, and a resend carries the same ``Idempotency-Key``."""
    result = await service.flush(now)
    return result.model_copy(update={"purged": await service.purge(now)})
