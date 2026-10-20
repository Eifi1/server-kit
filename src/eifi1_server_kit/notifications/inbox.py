"""The inbox, for an app that keeps one (§3.7, §10.1): keksdose's, and kastlan's from 0.16.

One row per recipient per delivered notification (``notification_type``, ``title``,
``body``, ``url``, ``push_sent``, ``created_at``, ``read_at``; kastlan's ``company_id``),
kept :data:`INBOX_RETENTION` — a recent-events feed, not an audit log (keksdose
``domain/services/notification_log_service.py:25-27``). The table and the daily prune
stay in the app; these are the wire shapes (keksdose ``domain/schemas/push.py:142-167``):

* ``GET /notifications/inbox?limit&offset`` → :class:`InboxResponse`;
* ``POST /notifications/inbox/read {ids?}`` (``null`` = all) → :class:`InboxMarkReadResponse`;
* ``DELETE /notifications/inbox``.

Layer 2 (0.16, §10.1 rule 5): the app implements :class:`InboxStore` over its table, and
``eifi1_server_kit.notifications.router.inbox_router`` (the ``fastapi`` extra) answers the
three routes, so a second app keeps an inbox without writing them. keksdose may keep its
own routes: the wire is the same. The rows are the ACCOUNT's, never a company's: no RLS
(kastlan's ``company_id`` names the company, and opening a row in another switches first:
:attr:`InboxItem.company_id` carries it to the bell, 0.17.0).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "INBOX_PAGE_DEFAULT",
    "INBOX_PAGE_MAX",
    "INBOX_RETENTION",
    "InboxItem",
    "InboxMarkRead",
    "InboxMarkReadResponse",
    "InboxResponse",
    "InboxStore",
    "NewInboxItem",
]

#: How long an inbox row is kept (keksdose ``RETENTION_DAYS = 90``).
INBOX_RETENTION = timedelta(days=90)
#: ``GET /notifications/inbox``'s page when no ``limit`` is sent (keksdose's 50).
INBOX_PAGE_DEFAULT = 50
#: The largest ``limit`` the route takes (keksdose's 200).
INBOX_PAGE_MAX = 200


class InboxItem(BaseModel):
    """One inbox row as the bell lists it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | int
    #: The type id, so the page can pick an icon.
    notification_type: str
    title: str
    body: str
    #: A path in the app, or ``None``.
    url: str | None
    created_at: datetime
    read_at: datetime | None
    #: The company the row is about, by name, where the app's rows carry one (kastlan,
    #: 0.16); ``None`` elsewhere. The list shows it beside the row.
    company: str | None = None
    #: The same company by its id (0.17.0), so the bell switches to it exactly before
    #: opening the row (kastlan: two companies may share a name); ``None`` elsewhere.
    company_id: int | str | None = None


class InboxResponse(BaseModel):
    """A page of the inbox, newest first."""

    items: list[InboxItem]
    #: Every unread row of the account (the badge), not just this page's.
    unread_count: int


class InboxMarkRead(BaseModel):
    """``POST /notifications/inbox/read``: these rows, or with ``ids`` ``null`` or left out,
    the whole inbox."""

    ids: list[UUID | int] | None = Field(default=None, max_length=INBOX_PAGE_MAX)


class InboxMarkReadResponse(BaseModel):
    """How many rows the call marked read."""

    updated: int


class NewInboxItem(BaseModel):
    """A row to write (:meth:`InboxStore.add`): the delivery's words, never sealed content."""

    user_id: int
    notification_type: str
    title: str
    body: str
    url: str | None = None
    #: kastlan's: the company the event belongs to.
    company_id: int | None = None
    #: Whether a push went with it (keksdose's ``push_sent``).
    push_sent: bool = False


class InboxStore(Protocol):
    """The app's inbox table (§10.1 rule 5, 0.16), on the request's session. Every method
    is the account's own: no id of another account's row is ever read, marked or deleted.
    It flushes and never commits; the app commits (the router's request, the job's run)."""

    async def add(self, item: NewInboxItem) -> None:
        """Insert one row, in the delivery's transaction."""

    async def page(self, user_id: int, limit: int, offset: int) -> tuple[Sequence[InboxItem], int]:
        """The account's rows newest first, ``limit`` from ``offset``, and its unread count
        over ALL its rows (the badge), not just this page's."""

    async def mark_read(self, user_id: int, ids: Sequence[UUID | int] | None) -> int:
        """Stamp ``read_at`` on these unread rows of the account (``None``: all of them);
        how many changed. Another account's ids change nothing."""

    async def clear(self, user_id: int) -> None:
        """Delete every row of the account."""

    async def prune(self, before: datetime) -> int:
        """Delete every account's rows created before ``before`` (the daily job:
        ``now - INBOX_RETENTION``); how many."""
