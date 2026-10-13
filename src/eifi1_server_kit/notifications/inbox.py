"""The inbox, for an app that keeps one (§3.7): keksdose's, unchanged.

One row per recipient per delivered notification (``notification_type``, ``title``,
``body``, ``url``, ``push_sent``, ``created_at``, ``read_at``), kept :data:`INBOX_RETENTION`
— a recent-events feed, not an audit log (keksdose
``domain/services/notification_log_service.py:25-27``). The table, the queries and the
daily prune stay in the app; these are the wire shapes (keksdose
``domain/schemas/push.py:142-167``):

* ``GET /notifications/inbox?limit&offset`` → :class:`InboxResponse`;
* ``POST /notifications/inbox/read {ids?}`` (``null`` = all) → :class:`InboxMarkReadResponse`;
* ``DELETE /notifications/inbox``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["INBOX_RETENTION", "InboxItem", "InboxMarkRead", "InboxMarkReadResponse", "InboxResponse"]

#: How long an inbox row is kept (keksdose ``RETENTION_DAYS = 90``).
INBOX_RETENTION = timedelta(days=90)


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


class InboxResponse(BaseModel):
    """A page of the inbox, newest first."""

    items: list[InboxItem]
    #: Every unread row of the account (the badge), not just this page's.
    unread_count: int


class InboxMarkRead(BaseModel):
    """``POST /notifications/inbox/read``: these rows, or with ``ids`` ``null`` or left out,
    the whole inbox."""

    ids: list[UUID | int] | None = None


class InboxMarkReadResponse(BaseModel):
    """How many rows the call marked read."""

    updated: int
