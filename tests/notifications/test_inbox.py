"""The inbox's shapes (notifications §3.7): keksdose's, unchanged."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eifi1_server_kit.notifications import (
    INBOX_RETENTION,
    InboxItem,
    InboxMarkRead,
    InboxMarkReadResponse,
    InboxResponse,
)

ROW_ID = uuid.UUID("5f0c1d2e-3b4a-4c5d-8e9f-0a1b2c3d4e5f")


@dataclass
class _LogRow:
    """keksdose's ``notification_log`` row, ``push_sent`` included and not answered."""

    id: uuid.UUID
    notification_type: str
    title: str
    body: str
    url: str | None
    push_sent: bool
    created_at: datetime
    read_at: datetime | None


def test_rows_are_kept_ninety_days() -> None:
    assert timedelta(days=90) == INBOX_RETENTION


def test_a_page_of_the_inbox() -> None:
    created = datetime(2026, 10, 13, 8, tzinfo=UTC)
    row = _LogRow(ROW_ID, "support_chat", "Keksdose", "Neue Antwort", "/support", True, created, None)
    page = InboxResponse(items=[InboxItem.model_validate(row)], unread_count=7)
    assert page.model_dump(mode="json") == {
        "items": [
            {
                "id": str(ROW_ID),
                "notification_type": "support_chat",
                "title": "Keksdose",
                "body": "Neue Antwort",
                "url": "/support",
                "created_at": "2026-10-13T08:00:00Z",
                "read_at": None,
                "company": None,
                "company_id": None,
            }
        ],
        "unread_count": 7,
    }


def test_marking_read_takes_some_ids_or_all() -> None:
    assert InboxMarkRead().ids is None, "left out: the whole inbox"
    assert InboxMarkRead.model_validate({"ids": None}).ids is None
    assert InboxMarkRead.model_validate({"ids": [str(ROW_ID), 12]}).ids == [ROW_ID, 12]
    assert InboxMarkReadResponse(updated=3).model_dump() == {"updated": 3}


def test_a_row_names_its_company_by_id_too() -> None:
    """0.16.1 (kastlan N7): the bell switches to the row's company by id, not by name."""
    item = InboxItem.model_validate(
        {
            "id": 5,
            "notification_type": "invoices_overdue",
            "title": "t",
            "body": "b",
            "url": None,
            "created_at": "2026-10-13T08:00:00Z",
            "read_at": None,
            "company": "Muster AG",
            "company_id": 3,
        }
    )
    assert (item.company, item.company_id) == ("Muster AG", 3)
    assert InboxItem.model_validate({**item.model_dump(), "company_id": "c-uuid"}).company_id == "c-uuid"
