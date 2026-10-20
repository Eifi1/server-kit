"""The tour progress router on the fake, over HTTP (tours §10.2.2, §10.3).

A FastAPI app built as an app builds it — its own account dependency, a demo guard,
``install_contract_error_handlers``, the router at ``/api/v1``.

No ``from __future__ import annotations``: the app's dependencies below are read by
FastAPI, as an app's would be.
"""

import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.tours import TourProgressService
from eifi1_server_kit.tours.router import TourProgressDeps, tour_progress_router
from eifi1_server_kit.tours.testing import InMemoryTourProgressStore

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
URL = "/api/v1/tours/progress"
#: People by the header that signs them in; 9 is the demo.
PEOPLE = {"ada": 1, "bo": 2, "demo": 9}
#: A client signed in as a person, on the default app or a given one.
Sign = Callable[..., httpx.AsyncClient]


async def current_user_id(x_user: Annotated[str | None, Header()] = None) -> int:
    if x_user not in PEOPLE:
        raise HTTPException(401, "Not authenticated")
    return PEOPLE[x_user]


async def refuse_demo(x_user: Annotated[str | None, Header()] = None) -> None:
    """kastlan's DEMO_WRITES gate, as a guard: 403 ``demo_read_only``."""
    if x_user == "demo":
        raise HTTPException(403, {"code": "demo_read_only"})


class SpyStore(InMemoryTourProgressStore):
    """The fake, counting a commit the kit must never ask for."""

    commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def store() -> SpyStore:
    return SpyStore({1: {"intro": datetime(2026, 10, 1, 8, 0, tzinfo=UTC), "old": None}, 2: {"secret": None}})


def app_for(spy: SpyStore, **options: Any) -> FastAPI:
    values: dict[str, Any] = {"store": lambda: spy, "current_user_id": current_user_id, "now": lambda: NOW}
    values.update(options)
    app = FastAPI()
    app.include_router(tour_progress_router(TourProgressDeps(**values)), prefix="/api/v1")
    install_contract_error_handlers(app)
    return app


@pytest.fixture
async def client(store: SpyStore) -> AsyncIterator[Sign]:
    clients: list[httpx.AsyncClient] = []

    def signed_in(person: str, app: FastAPI | None = None) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app or app_for(store, guards=(refuse_demo,)))
        made = httpx.AsyncClient(transport=transport, base_url="http://test", headers={"X-User": person})
        clients.append(made)
        return made

    yield signed_in
    for made in clients:
        await made.aclose()


async def test_get_and_patch_round_trip(client: Sign, store: SpyStore) -> None:
    ada = client("ada")
    first = await ada.get(URL)
    assert first.status_code == 200
    assert first.json() == {"completed": {"intro": "2026-10-01T08:00:00Z", "old": ""}}

    sent = {"completed": {"old": "2026-10-18T09:30:00+02:00", "addBuilding": ""}, "forget": ["intro"]}
    answer = await ada.patch(URL, json=sent)
    assert answer.status_code == 200
    assert answer.json() == {"completed": {"addBuilding": "", "old": "2026-10-18T07:30:00Z"}}
    assert (await ada.get(URL)).json() == answer.json()
    assert store.writes == [
        (1, {"old": datetime(2026, 10, 18, 7, 30, tzinfo=UTC), "addBuilding": None}, ("intro",)),
    ]
    assert store.commits == 0, "the kit never commits"


async def test_a_patch_that_changes_nothing_writes_nothing(client: Sign, store: SpyStore) -> None:
    answer = await client("ada").patch(URL, json={"completed": {"old": ""}})
    assert answer.status_code == 200 and store.writes == []


async def test_a_future_completion_is_stored_as_now(client: Sign, store: SpyStore) -> None:
    future = (NOW + timedelta(days=1)).isoformat()
    answer = await client("bo").patch(URL, json={"completed": {"secret": future}})
    assert answer.json() == {"completed": {"secret": "2026-10-19T12:00:00Z"}}


async def test_another_accounts_rows_are_never_read(client: Sign, store: SpyStore) -> None:
    bo = client("bo")
    assert (await bo.get(URL)).json() == {"completed": {"secret": ""}}
    await bo.patch(URL, json={"forget": ["intro"]})
    assert store.reads == [2, 2] and store.writes == []
    assert "intro" in store.rows[1]


@pytest.mark.parametrize(
    "body",
    [
        {"completed": {}, "reset": True},
        {"completed": {"a": "2026-10-19T12:00:00"}},
        {"completed": {"bad id": ""}},
        {"completed": {"a": None}},
        {"forget": "a"},
    ],
)
async def test_a_malformed_body_is_a_422_and_writes_nothing(
    client: Sign, store: SpyStore, body: dict[str, Any]
) -> None:
    response = await client("ada").patch(URL, json=body)
    assert response.status_code == 422 and store.writes == []


async def test_the_coded_refusals(client: Sign, store: SpyStore) -> None:
    ada = client("ada")
    conflict = await ada.patch(URL, json={"completed": {"a": ""}, "forget": ["a"]})
    assert conflict.status_code == 422
    assert conflict.json() == {
        "detail": "A tour cannot be both completed and forgotten in one request",
        "code": "tour_progress_conflict",
    }
    full = await ada.patch(URL, json={"completed": {f"t{index}": "" for index in range(501)}})
    assert full.status_code == 422 and full.json()["code"] == "tour_progress_full"
    assert store.writes == []


async def test_a_guards_403_passes_through_first(client: Sign, store: SpyStore) -> None:
    demo = client("demo")
    for response in (
        await demo.get(URL),
        await demo.patch(URL, json={"completed": {"a": ""}}),
        await demo.patch(URL, json={"nonsense": 1}),
    ):
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "demo_read_only"}}
    assert store.reads == [] and store.writes == []


async def test_signed_out_is_the_apps_401(client: Sign, store: SpyStore) -> None:
    assert (await client("nobody").get(URL)).status_code == 401
    assert store.reads == []


async def test_without_guards_prefix_and_tags(client: Sign, store: SpyStore) -> None:
    router = tour_progress_router(
        TourProgressDeps(store=lambda: store, current_user_id=current_user_id), prefix="/me/tours", tags=("me",)
    )
    assert router.prefix == "/me/tours" and router.tags == ["me"] and router.dependencies == []
    app = FastAPI()
    app.include_router(router)
    demo = client("demo", app)
    assert (await demo.get("/me/tours/progress")).json() == {"completed": {}}


async def test_the_service_exports_the_accounts_rows(store: SpyStore) -> None:
    """§10.2.9: the export's ``tour_progress``, by id, ``completed_at`` null for ""."""
    service = TourProgressService(store)
    assert await service.export(1) == [
        {"tour_id": "intro", "completed_at": "2026-10-01T08:00:00Z"},
        {"tour_id": "old", "completed_at": None},
    ]
    assert await service.export(5) == []


async def test_the_export_spells_an_instant_as_the_wire_does(client: Sign) -> None:
    """One record, one spelling: the export's ``completed_at`` is the ``GET``'s string
    (``Z``, not ``+00:00``), fractions and other zones included (0.16.0, KS's adoption)."""
    rows = {
        "a": datetime(2026, 10, 18, 7, 0, 0, 120000, tzinfo=UTC),
        "b": datetime(2026, 10, 18, 9, 30, tzinfo=timezone(timedelta(hours=2))),
    }
    store = SpyStore({1: rows})
    wire = (await client("ada", app_for(store)).get(URL)).json()["completed"]
    exported = {row["tour_id"]: row["completed_at"] for row in await TourProgressService(store).export(1)}
    assert exported == wire == {"a": "2026-10-18T07:00:00.120000Z", "b": "2026-10-18T07:30:00Z"}


def test_the_service_defaults_to_the_kits_clock(store: SpyStore) -> None:
    from eifi1_server_kit.clock import utc_now

    assert TourProgressService(store).now is utc_now
    assert TourProgressDeps(store=lambda: store, current_user_id=current_user_id).guards == ()


async def test_the_store_is_resolved_per_request(store: SpyStore) -> None:
    """The app's store dependency runs on each request, as its session does."""
    seen: list[int] = []

    def per_request() -> SpyStore:
        seen.append(1)
        return store

    app = app_for(store, store=per_request)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as made:
        await made.get(URL, headers={"X-User": "ada"})
        await made.get(URL, headers={"X-User": "ada"})
    assert seen == [1, 1]


async def test_a_naive_store_exports_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A naive ``completed_at`` is UTC, never the server's local zone (S1)."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    service = TourProgressService(InMemoryTourProgressStore({1: {"intro": datetime(2026, 10, 1, 8, 0)}}))
    assert await service.export(1) == [{"tour_id": "intro", "completed_at": "2026-10-01T08:00:00Z"}]
    assert (await service.progress(1)).completed == {"intro": datetime(2026, 10, 1, 8, 0, tzinfo=UTC)}
    monkeypatch.undo()
    time.tzset()
