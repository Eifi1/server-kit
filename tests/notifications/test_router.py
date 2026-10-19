"""The inbox's routes and the queue's jobs route on the fakes, over HTTP (§10.1, §10.4; 0.16).

No ``from __future__ import annotations``: the app's dependencies below are read by
FastAPI, as an app's would be.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.jobs import JOBS_TOKEN_HEADER, require_jobs_token
from eifi1_server_kit.notifications import INBOX_RETENTION, NewInboxItem, NotificationType, QueueService
from eifi1_server_kit.notifications.router import InboxDeps, inbox_router, notifications_jobs_router
from eifi1_server_kit.notifications.testing import (
    InMemoryInboxStore,
    InMemoryNotificationQueue,
    RecordingFlushPort,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
URL = "/api/v1/notifications/inbox"
PEOPLE = {"ada": 1, "bo": 2}
TOKEN = "s3cret-token"


async def current_user_id(x_user: Annotated[str | None, Header()] = None) -> int:
    if x_user not in PEOPLE:
        raise HTTPException(401, "Not authenticated")
    return PEOPLE[x_user]


async def refuse_closed(x_closed: Annotated[str | None, Header()] = None) -> None:
    if x_closed:
        raise HTTPException(403, "closed")


@pytest.fixture
async def store() -> InMemoryInboxStore:
    inbox = InMemoryInboxStore(NOW, companies={3: "Muster AG"})
    for number in range(1, 4):
        await inbox.add(NewInboxItem(user_id=1, notification_type="invoices_overdue", title=f"#{number}", body="b"))
        inbox.now += timedelta(minutes=1)
    await inbox.add(NewInboxItem(user_id=1, notification_type="invoices_overdue", title="AG", body="b", company_id=3))
    await inbox.add(NewInboxItem(user_id=2, notification_type="scan_done", title="Bo's", body="b", url="/scan"))
    return inbox


def _client(app: FastAPI, user: str = "ada", **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-User": user, **headers}
    )


def _inbox_app(store: InMemoryInboxStore) -> FastAPI:
    app = FastAPI()
    deps = InboxDeps(store=lambda: store, current_user_id=current_user_id, guards=(refuse_closed,))
    app.include_router(inbox_router(deps), prefix="/api/v1")
    install_contract_error_handlers(app)
    return app


async def test_a_page_of_the_inbox_newest_first_with_the_unread_count(store: InMemoryInboxStore) -> None:
    async with _client(_inbox_app(store)) as client:
        first = (await client.get(URL, params={"limit": 2})).json()
        second = (await client.get(URL, params={"limit": 2, "offset": 2})).json()
    assert [item["title"] for item in first["items"]] == ["AG", "#3"]
    assert first["items"][0]["company"] == "Muster AG" and first["items"][1]["company"] is None
    assert [item["title"] for item in second["items"]] == ["#2", "#1"]
    assert first["unread_count"] == second["unread_count"] == 4, "over every row, not the page"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
async def test_the_page_bounds_are_a_422(store: InMemoryInboxStore, params: dict[str, int]) -> None:
    async with _client(_inbox_app(store)) as client:
        assert (await client.get(URL, params=params)).status_code == 422


async def test_marking_one_then_all_read_touches_only_the_accounts_rows(store: InMemoryInboxStore) -> None:
    bos = next(row.id for row in store.rows if row.user_id == 2)
    async with _client(_inbox_app(store)) as client:
        one = await client.post(f"{URL}/read", json={"ids": [1, bos]})
        assert one.json() == {"updated": 1}, "another account's id changes nothing"
        rest = await client.post(f"{URL}/read", json={})
        assert rest.json() == {"updated": 3}
        assert (await client.get(URL)).json()["unread_count"] == 0
    assert next(row for row in store.rows if row.user_id == 2).read_at is None


async def test_clearing_deletes_the_accounts_rows_only(store: InMemoryInboxStore) -> None:
    async with _client(_inbox_app(store)) as client:
        assert (await client.delete(URL)).status_code == 204
        assert (await client.get(URL)).json() == {"items": [], "unread_count": 0}
    async with _client(_inbox_app(store), user="bo") as client:
        assert len((await client.get(URL)).json()["items"]) == 1


async def test_the_guards_and_the_session_answer_first(store: InMemoryInboxStore) -> None:
    async with _client(_inbox_app(store), user="nobody") as client:
        assert (await client.get(URL)).status_code == 401
    async with _client(_inbox_app(store), **{"X-Closed": "1"}) as client:
        assert (await client.delete(URL)).status_code == 403
    assert len(store.rows) == 5


async def test_the_prune_drops_rows_past_retention(store: InMemoryInboxStore) -> None:
    assert await store.prune(NOW + timedelta(minutes=2)) == 2
    assert await store.prune(NOW + INBOX_RETENTION) == 3
    assert store.rows == []


async def test_the_jobs_route_runs_the_tick_behind_the_token() -> None:
    queue = InMemoryNotificationQueue()
    port = RecordingFlushPort()
    service = QueueService(queue, port)
    comment = NotificationType("feedback_comment", app=None, email=True)
    await service.queue_mail(comment, 1, "comment:1", {"number": 1}, now=NOW - timedelta(minutes=1))
    queue.commit()
    app = FastAPI()
    app.include_router(
        notifications_jobs_router(lambda: service, require_jobs_token(lambda: TOKEN), now=lambda: NOW),
        prefix="/api/v1",
    )
    install_contract_error_handlers(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post("/api/v1/ops/notifications/flush", headers={JOBS_TOKEN_HEADER: "wrong"})
        assert refused.status_code == 401 and port.mails == []
        answer = await client.post("/api/v1/ops/notifications/flush", headers={JOBS_TOKEN_HEADER: TOKEN})
    assert answer.json() == {"sent": 1, "dropped": 0, "left": 0, "purged": 0}
    assert len(port.mails) == 1
