"""Test helpers for the inbox and the queue: in-memory fakes (§10.1, §10.4; 0.16).

In the wheel and NOT re-exported from :mod:`eifi1_server_kit.notifications`: import it as
``eifi1_server_kit.notifications.testing``. No FastAPI, no database.

* :class:`InMemoryInboxStore` — an :class:`~.inbox.InboxStore`;
* :class:`InMemoryNotificationQueue` — a :class:`~.queue.NotificationQueue` with the
  unique ``(user_id, type_id, channel, event)``, the claim's rules and a ``commits`` log
  (``enqueue`` writes into :attr:`InMemoryNotificationQueue.pending` until
  :meth:`~InMemoryNotificationQueue.commit`, so a test shows that a rollback queues nothing);
* :class:`RecordingFlushPort` — a :class:`~.queue.FlushPort` that records each send and
  answers what the test set.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from eifi1_server_kit.notifications.inbox import InboxItem, NewInboxItem
from eifi1_server_kit.notifications.push import PushPayload
from eifi1_server_kit.notifications.queue import NewQueueItem, QueueChannel, QueueItem

__all__ = ["InMemoryInboxStore", "InMemoryNotificationQueue", "RecordingFlushPort", "StoredInboxItem"]


@dataclass(slots=True)
class StoredInboxItem:
    """One row of :class:`InMemoryInboxStore`."""

    id: int
    user_id: int
    notification_type: str
    title: str
    body: str
    url: str | None
    created_at: datetime
    read_at: datetime | None = None
    company_id: int | None = None
    push_sent: bool = False


class InMemoryInboxStore:
    """An :class:`~.inbox.InboxStore` in a list. ``now`` stamps ``created_at`` and
    ``read_at``; ``companies`` names a row's ``company_id`` for :attr:`~.inbox.InboxItem.company`."""

    def __init__(self, now: datetime, companies: dict[int, str] | None = None) -> None:
        self.now = now
        self.companies = companies or {}
        self.rows: list[StoredInboxItem] = []

    async def add(self, item: NewInboxItem) -> None:
        self.rows.append(
            StoredInboxItem(
                id=len(self.rows) + 1,
                user_id=item.user_id,
                notification_type=item.notification_type,
                title=item.title,
                body=item.body,
                url=item.url,
                created_at=self.now,
                company_id=item.company_id,
                push_sent=item.push_sent,
            )
        )

    async def page(self, user_id: int, limit: int, offset: int) -> tuple[list[InboxItem], int]:
        mine = sorted(
            (row for row in self.rows if row.user_id == user_id), key=lambda row: (row.created_at, row.id), reverse=True
        )
        items = [
            InboxItem(
                id=row.id,
                notification_type=row.notification_type,
                title=row.title,
                body=row.body,
                url=row.url,
                created_at=row.created_at,
                read_at=row.read_at,
                company=self.companies.get(row.company_id) if row.company_id is not None else None,
            )
            for row in mine[offset : offset + limit]
        ]
        return items, sum(1 for row in mine if row.read_at is None)

    async def mark_read(self, user_id: int, ids: Sequence[UUID | int] | None) -> int:
        wanted = None if ids is None else set(ids)
        changed = 0
        for row in self.rows:
            if row.user_id == user_id and row.read_at is None and (wanted is None or row.id in wanted):
                row.read_at = self.now
                changed += 1
        return changed

    async def clear(self, user_id: int) -> None:
        self.rows = [row for row in self.rows if row.user_id != user_id]

    async def prune(self, before: datetime) -> int:
        kept = [row for row in self.rows if row.created_at >= before]
        pruned = len(self.rows) - len(kept)
        self.rows = kept
        return pruned


class InMemoryNotificationQueue:
    """A :class:`~.queue.NotificationQueue` in a dict. ``enqueue`` writes into
    :attr:`pending` (the caller's transaction); :meth:`commit` moves it into :attr:`rows`
    and :meth:`rollback` forgets it. The tick's methods commit, and each is logged in
    :attr:`commits`."""

    def __init__(self) -> None:
        self.rows: dict[int, QueueItem] = {}
        self.pending: list[QueueItem] = []
        self.commits: list[str] = []
        self._next = 1

    def _key(self, item: QueueItem | NewQueueItem) -> tuple[int, str, str, str]:
        return item.user_id, item.type_id, item.channel, item.event

    async def enqueue(self, item: NewQueueItem) -> bool:
        taken = {self._key(row) for row in (*self.rows.values(), *self.pending)}
        if self._key(item) in taken:
            return False
        self.pending.append(
            QueueItem(
                id=self._next,
                user_id=item.user_id,
                type_id=item.type_id,
                channel=item.channel,
                event=item.event,
                payload=dict(item.payload),
                not_before=item.not_before,
                created_at=item.created_at,
            )
        )
        self._next += 1
        return True

    def commit(self) -> None:
        """The caller's transaction commits: its enqueued rows become rows."""
        for row in self.pending:
            self.rows[row.id] = row
        self.pending = []

    def rollback(self) -> None:
        """The caller's transaction rolls back: nothing it enqueued stays."""
        self.pending = []

    async def open_bundle(self, user_id: int, type_id: str) -> datetime | None:
        opens = [
            row.not_before
            for row in (*self.rows.values(), *self.pending)
            if row.user_id == user_id
            and row.type_id == type_id
            and row.channel == "email"
            and row.sent_at is None
            and row.dropped is None
            and row.claimed_at is None
        ]
        return min(opens, default=None)

    async def claim(self, now: datetime, limit: int, reclaim_before: datetime) -> list[QueueItem]:
        due = sorted(
            (
                row
                for row in self.rows.values()
                if row.sent_at is None
                and row.dropped is None
                and row.not_before <= now
                and (row.claimed_at is None or row.claimed_at < reclaim_before)
            ),
            key=lambda row: (row.created_at, row.id),
        )[:limit]
        claimed = [dataclasses.replace(row, claimed_at=now, attempts=row.attempts + 1) for row in due]
        for row in claimed:
            self.rows[row.id] = row
        self.commits.append("claim")
        return claimed

    async def done(self, ids: Sequence[int], sent_at: datetime) -> None:
        for row_id in ids:
            self.rows[row_id] = dataclasses.replace(self.rows[row_id], sent_at=sent_at)
        self.commits.append("done")

    async def drop(self, ids: Sequence[int], reason: str) -> None:
        for row_id in ids:
            self.rows[row_id] = dataclasses.replace(self.rows[row_id], dropped=reason)
        self.commits.append("drop")

    async def defer(self, ids: Sequence[int], not_before: datetime) -> None:
        for row_id in ids:
            row = self.rows[row_id]
            self.rows[row_id] = dataclasses.replace(
                row, not_before=not_before, claimed_at=None, attempts=max(row.attempts - 1, 0)
            )
        self.commits.append("defer")

    async def purge(self, before: datetime) -> int:
        gone = [
            row_id
            for row_id, row in self.rows.items()
            if (row.sent_at is not None or row.dropped is not None) and row.created_at < before
        ]
        for row_id in gone:
            del self.rows[row_id]
        self.commits.append("purge")
        return len(gone)


@dataclass(slots=True)
class RecordingFlushPort:
    """A :class:`~.queue.FlushPort` that records. ``wanted`` answers
    :meth:`still_wanted` per ``(user_id, type_id, channel)`` (``True`` when absent);
    ``sends_ok`` is what every send answers."""

    wanted: dict[tuple[int, str, str], bool | datetime] = field(default_factory=dict)
    sends_ok: bool = True
    pushes: list[tuple[int, PushPayload]] = field(default_factory=list)
    mails: list[tuple[int, str, list[QueueItem], str]] = field(default_factory=list)

    async def still_wanted(self, user_id: int, type_id: str, channel: QueueChannel) -> bool | datetime:
        return self.wanted.get((user_id, type_id, channel), True)

    async def send_push(self, user_id: int, payload: PushPayload) -> bool:
        self.pushes.append((user_id, payload))
        return self.sends_ok

    async def send_mail(self, user_id: int, type_id: str, items: Sequence[QueueItem], idempotency_key: str) -> bool:
        self.mails.append((user_id, type_id, list(items), idempotency_key))
        return self.sends_ok
