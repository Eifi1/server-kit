"""The ``/sync`` wire shapes (local-first §1.5 D1, §1.6): keksdose's, which are canon."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import ValidationError

from eifi1_server_kit import billing
from eifi1_server_kit.billing.errors import BILLING_ERROR_DETAIL, BillingErrorCode
from eifi1_server_kit.sync import (
    SYNC_PLAN_LIMIT,
    SYNC_REJECTED,
    SyncChange,
    SyncPageCursor,
    SyncRefusal,
    SyncRejection,
    SyncRequest,
    SyncResponse,
)

ROW = "6f1c2a52-0d7e-4c43-9a51-5d1f1f3b3a10"


def test_an_old_request_without_refusals_or_edit_times_parses() -> None:
    """D1, D8: the shape keksdose and kastlan send today stays accepted for one release."""
    request = SyncRequest.model_validate(
        {"last_knowledge": 7, "changes": [{"entity": "accounts", "id": ROW, "op": "upsert", "data": {"name": "A"}}]}
    )
    assert request.refusals is False and request.paged is False and request.page_cursor is None
    assert request.scope is None
    (change,) = request.changes
    assert change.updated_at is None and change.knowledge is None
    assert SyncRequest().last_knowledge == 0 and list(SyncRequest().changes) == []
    with pytest.raises(ValidationError):
        SyncRequest(last_knowledge=-1)


def test_a_change_carries_the_devices_edit_time_in_utc() -> None:
    """D2: an instant with a zone, stored in UTC; a naive one is refused."""
    change = SyncChange.model_validate(
        {"entity": "handover_room", "id": "r-1", "op": "upsert", "updated_at": "2026-10-19T10:00:00+02:00"}
    )
    assert change.updated_at == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert change.updated_at is not None and change.updated_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        SyncChange.model_validate({"entity": "e", "id": "r", "op": "upsert", "updated_at": "2026-10-19T10:00:00"})
    with pytest.raises(ValidationError):
        SyncChange.model_validate({"entity": "e", "id": "r", "op": "patch"})
    delete = SyncChange(entity="accounts", id=uuid.UUID(ROW), op="delete")
    assert delete.data is None and delete.id == uuid.UUID(ROW)
    assert SyncChange(entity="e", id=ROW, op="delete").model_dump(mode="json")["id"] == ROW


def test_rejections_default_to_none_and_refused_stays_one_for_the_push() -> None:
    reply = SyncResponse(server_knowledge=3, changes=[])
    assert reply.rejections == [] and reply.refused is None and reply.page_cursor is None
    assert reply.model_dump(mode="json") == {
        "server_knowledge": 3,
        "changes": [],
        "refused": None,
        "rejections": [],
        "page_cursor": None,
    }
    rejected = SyncResponse(
        server_knowledge=4,
        changes=[],
        rejections=[
            SyncRejection(change_id=uuid.UUID(ROW), code=SYNC_REJECTED, detail="The meter number is fixed"),
            SyncRejection(change_id="photo-9", code=SYNC_PLAN_LIMIT, detail="Over the plan's photos"),
        ],
    )
    assert [r.change_id for r in rejected.rejections] == [ROW, "photo-9"]
    assert (SYNC_REJECTED, SYNC_PLAN_LIMIT) == ("rejected", "plan_limit")
    with pytest.raises(ValidationError):
        SyncRejection(change_id="", code=SYNC_REJECTED, detail="")
    with pytest.raises(ValidationError):
        SyncRejection(change_id="x", code="", detail="")


def test_sync_refusal_is_billings_and_keeps_its_defaults() -> None:
    """Moved from billing (§1.6), the same object; its defaults are billing's code and text."""
    assert billing.SyncRefusal is SyncRefusal
    assert billing.schemas.SyncRefusal is SyncRefusal
    refusal = SyncRefusal(change_ids=[uuid.UUID(ROW)])
    assert refusal.code == BillingErrorCode.BILLING_READ_ONLY.value
    assert refusal.detail == BILLING_ERROR_DETAIL[BillingErrorCode.BILLING_READ_ONLY]
    assert refusal.change_ids == [ROW]
    assert SyncRefusal.__doc__ is not None and "A 402 is not a refusal of one change" in SyncRefusal.__doc__


def test_the_page_cursor_is_keksdoses() -> None:
    cursor = SyncPageCursor(entity="transactions", knowledge=12, id=ROW, window_end=40)
    assert SyncPageCursor.model_validate(cursor.model_dump(mode="json")) == cursor
    assert SyncPageCursor(entity="accounts", window_end=0).knowledge is None
    with pytest.raises(ValidationError):
        SyncPageCursor.model_validate({"entity": "accounts"})
    with pytest.raises(ValidationError):
        SyncPageCursor(entity="accounts", knowledge=-1, window_end=1)


class KkChange(SyncChange):
    id: uuid.UUID


class KaChange(SyncChange):
    id: str
    pk: int | None = None


class KaRequest(SyncRequest):
    scope: Literal["handover"]
    changes: Sequence[KaChange] = []


class KkResponse(SyncResponse):
    changes: list[KkChange]
    fx_cursor: int = 0
    tenant_sealed: bool = False


def test_an_app_subclasses_and_narrows_without_ignores() -> None:
    """Open to subclassing: keksdose's UUID ids and extras, kastlan's ``pk`` and scope."""
    kk = KkResponse.model_validate(
        {"server_knowledge": 1, "changes": [{"entity": "a", "id": ROW, "op": "delete"}], "tenant_sealed": True}
    )
    assert kk.changes[0].id == uuid.UUID(ROW) and kk.tenant_sealed and kk.rejections == []
    ka = KaRequest.model_validate(
        {"scope": "handover", "changes": [{"entity": "handover_room", "id": ROW, "op": "upsert", "pk": 4}]}
    )
    assert ka.changes[0].id == ROW and ka.changes[0].pk == 4
    with pytest.raises(ValidationError):
        KaRequest.model_validate({"scope": "maintenance"})
    # Unknown fields are ignored, not refused: an older or newer client still parses.
    assert SyncRequest.model_validate({"fx_cursor": 3}).last_knowledge == 0


def test_a_naive_offset_is_converted_not_kept() -> None:
    plus_two = timezone(timedelta(hours=2))
    change = SyncChange(entity="e", id="r", op="upsert", updated_at=datetime(2026, 1, 1, 2, tzinfo=plus_two))
    assert change.updated_at == datetime(2026, 1, 1, tzinfo=UTC)
