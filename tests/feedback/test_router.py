"""The feedback router on the fakes, over HTTP (server-services-harmonization §2.8–§2.10, §4.1).

A FastAPI app built as an app builds it — its own actor dependency, demo guard factory and
admin guard, ``install_contract_error_handlers``, the router mounted at ``/api/v1`` and
the limiters on ``app.state`` — so §3's behaviour is tested once, here.

No ``from __future__ import annotations``: the app's dependencies below are read by
FastAPI, as an app's would be.
"""

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.routing import APIRoute
from pydantic import Field

from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.feedback import (
    SHA12_KEY_PATTERN,
    THROTTLED_DETAIL,
    AttachmentUrlPolicy,
    FeedbackActor,
    FeedbackCreate,
    FeedbackResponse,
    FeedbackStatus,
    FeedbackUpdate,
)
from eifi1_server_kit.feedback.router import FeedbackDeps, app_state_limiters, feedback_router
from eifi1_server_kit.feedback.testing import (
    InMemoryAttachmentStorage,
    MemoryFeedbackRow,
    check_round_trip,
    fixed_clock,
)
from eifi1_server_kit.limiter import FeedbackLimiters
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO, EMAILS, NOW, PDF, POLICY, STRANGER, SpyRepository, png

PEOPLE = {person.id: person for person in (AUTHOR, ADMIN, STRANGER, DEMO)}
BASE = "/api/v1/feedback"


# --- the app's own pieces, as each app writes them --------------------------------------


async def current_actor(x_actor: Annotated[int | None, Header()] = None) -> FeedbackActor:
    if x_actor is None or x_actor not in PEOPLE:
        raise HTTPException(401, "Not authenticated")
    return PEOPLE[x_actor]


def not_in_the_demo(what: str) -> Callable[..., Any]:
    """Kurvenschmiede's four lines around the kit's ``refuse_demo``."""

    async def _check(actor: Annotated[FeedbackActor, Depends(current_actor)]) -> FeedbackActor:
        refuse_demo(actor.is_demo, what)
        return actor

    return _check


async def require_admin(actor: Annotated[FeedbackActor, Depends(current_actor)]) -> FeedbackActor:
    if not actor.is_admin:
        raise HTTPException(403, "Insufficient role")
    return actor


class AppFeedbackCreate(FeedbackCreate):
    attachment_url_policy = POLICY


class AppFeedbackUpdate(FeedbackUpdate):
    outcome: str | None = Field(default=None, max_length=4000)


class AppFeedbackResponse(FeedbackResponse):
    reworks: int = 0


class World:
    """One app: its repository, storage and limiters, and a client signed in as anyone."""

    def __init__(self, **options: Any) -> None:
        self.repository = SpyRepository(emails=EMAILS, now=fixed_clock(NOW), policy=POLICY)
        self.storage = InMemoryAttachmentStorage()
        values: dict[str, Any] = {
            "actor": current_actor,
            "repository": lambda: self.repository,
            "storage": lambda: self.storage,
            "demo_guard": not_in_the_demo,
            "admin_guard": require_admin,
            "policy": POLICY,
            "create_model": AppFeedbackCreate,
            "update_model": AppFeedbackUpdate,
            "response_model": AppFeedbackResponse,
            "now": fixed_clock(NOW),
        }
        values.update(options)
        self.deps: FeedbackDeps[MemoryFeedbackRow] = FeedbackDeps(**values)
        self.app = FastAPI()
        install_contract_error_handlers(self.app)
        self.router = feedback_router(self.deps)
        self.app.include_router(self.router, prefix="/api/v1")
        self.app.state.feedback_limiters = FeedbackLimiters()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    @staticmethod
    def signed_in(person: FeedbackActor) -> dict[str, str]:
        return {"X-Actor": str(person.id)}

    async def request(self, method: str, url: str, person: FeedbackActor, **options: Any) -> httpx.Response:
        self.repository.calls.clear()
        response = await self.client.request(method, url, headers=self.signed_in(person), **options)
        assert self.repository.one_write_last(), self.repository.calls
        return response

    async def upload(self, person: FeedbackActor, data: bytes, kind: str = "image/png") -> httpx.Response:
        return await self.request("POST", f"{BASE}/attachments", person, files={"file": ("shot.png", data, kind)})

    async def file(self, person: FeedbackActor = AUTHOR, **body: Any) -> dict[str, Any]:
        response = await self.request("POST", BASE, person, json={"title": "Sync is slow", **body})
        assert response.status_code == 201, response.text
        answer: dict[str, Any] = response.json()
        return answer


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = World()
    async with built.client:
        yield built


# --- the attachments ---------------------------------------------------------------------


async def test_an_upload_answers_201_with_its_url_and_the_download_serves_it(world: World) -> None:
    picture = png()
    uploaded = await world.upload(AUTHOR, picture)
    assert uploaded.status_code == 201
    url = uploaded.json()["url"]
    assert POLICY.is_own_url(url)
    assert world.repository.calls == []  # an upload is no repository write

    downloaded = await world.request("GET", url, STRANGER)
    assert downloaded.status_code == 200 and downloaded.content == picture
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["content-disposition"].startswith('inline; filename="shot.png"')
    assert downloaded.headers["x-content-type-options"] == "nosniff"


async def test_a_text_file_downloads_as_an_attachment(world: World) -> None:
    uploaded = await world.upload(AUTHOR, b"a log line\n", "text/plain")
    downloaded = await world.request("GET", uploaded.json()["url"], AUTHOR)
    assert downloaded.headers["content-type"] == "text/plain; charset=utf-8"
    assert downloaded.headers["content-disposition"].startswith("attachment;")


async def test_a_missing_or_malformed_key_is_the_kits_404(world: World) -> None:
    for key in ("ffffffffffff.png", "not-a-key.exe", "0123456789ab.png%0A"):
        missing = await world.request("GET", f"{BASE}/attachments/{key}", AUTHOR)
        assert missing.status_code == 404 and missing.json() == {"detail": "Attachment not found"}


async def test_the_upload_refusals_are_the_contracts(world: World) -> None:
    empty = await world.upload(AUTHOR, b"")
    assert empty.status_code == 400
    svg = await world.upload(AUTHOR, b"<svg/>", "image/svg+xml")
    assert svg.status_code == 415
    assert (await world.upload(AUTHOR, png()[:40])).status_code == 201  # the signature passes…


async def test_the_image_guard_is_the_apps_choice() -> None:
    world = World(decode_images=True)
    async with world.client:
        refused = await world.upload(AUTHOR, png((64, 64))[:40])  # …and the parse does not
        assert refused.status_code == 400 and "renamed" in refused.json()["detail"]


async def test_a_spent_upload_window_is_a_429_with_retry_after(world: World) -> None:
    world.app.state.feedback_limiters = FeedbackLimiters(uploads_per_hour=1)
    assert (await world.upload(AUTHOR, png())).status_code == 201
    spent = await world.upload(AUTHOR, PDF, "application/pdf")
    assert spent.status_code == 429
    assert spent.json() == {"detail": THROTTLED_DETAIL}
    assert int(spent.headers["retry-after"]) > 3500


# --- create and crash --------------------------------------------------------------------


async def test_create_answers_201_in_the_servers_status(world: World) -> None:
    shot = (await world.upload(AUTHOR, png())).json()["url"]
    answer = await world.file(AUTHOR, body="Opening the register takes 4s", screenshot_url=shot)
    assert answer["status"] == "OPEN" and answer["user_id"] == AUTHOR.id and answer["user_email"] is None
    assert answer["screenshot_url"] == shot and answer["attachment_urls"] is None
    assert answer["context"] == {"user_id": 1, "user_email": AUTHOR.email, "user_display_name": AUTHOR.display_name}
    assert answer["reworks"] == 0  # the app's response model
    assert (await world.file(ADMIN))["status"] == "READY"


async def test_create_refuses_a_foreign_url_and_crash_by_hand(world: World) -> None:
    foreign = await world.request("POST", BASE, AUTHOR, json={"title": "x", "screenshot_url": "https://evil/x.png"})
    assert foreign.status_code == 422
    crash = await world.request("POST", BASE, ADMIN, json={"title": "x", "category": "CRASH"})
    assert crash.status_code == 422
    assert crash.json() == {"detail": "CRASH is filed automatically by the crash reporter and cannot be set by hand"}


async def test_a_crash_is_always_a_202(world: World) -> None:
    crash = {"name": "TypeError", "message": "x is undefined", "route": "/accounts"}
    first = await world.request("POST", f"{BASE}/crash", AUTHOR, json=crash)
    assert first.status_code == 202
    assert first.json()["stored"] is True and first.json()["duplicate"] is False
    again = await world.request("POST", f"{BASE}/crash", AUTHOR, json=crash)
    assert again.json()["duplicate"] is True and again.json()["feedback_id"] == first.json()["feedback_id"]
    demo = await world.request("POST", f"{BASE}/crash", DEMO, json=crash)
    assert demo.status_code == 202 and demo.json() == {
        "stored": False,
        "duplicate": False,
        "feedback_id": None,
        "reference": None,
    }
    world.app.state.feedback_limiters = FeedbackLimiters(crashes_per_hour=0)  # disabled: every hit allowed
    assert (await world.request("POST", f"{BASE}/crash", STRANGER, json=crash)).json()["stored"] is True
    world.app.state.feedback_limiters = FeedbackLimiters(crashes_per_hour=1)
    await world.request("POST", f"{BASE}/crash", STRANGER, json=crash)
    spent = await world.request("POST", f"{BASE}/crash", STRANGER, json=crash)
    assert spent.status_code == 202 and spent.json()["stored"] is False


# --- the lists ---------------------------------------------------------------------------


async def test_my_list_and_the_inbox(world: World) -> None:
    mine = await world.file(AUTHOR)
    theirs = await world.file(STRANGER)
    listed = await world.request("GET", f"{BASE}/my", AUTHOR)
    assert [row["id"] for row in listed.json()] == [mine["id"]]
    assert listed.json()[0]["user_email"] is None  # the address is the inbox's only

    inbox = await world.request("GET", BASE, ADMIN)
    assert [(row["id"], row["user_email"]) for row in inbox.json()] == [
        (theirs["id"], STRANGER.email),
        (mine["id"], AUTHOR.email),
    ]
    world.repository.rows[theirs["id"]].status = FeedbackStatus.DONE
    done = await world.request("GET", BASE, ADMIN, params={"status_filter": "DONE"})
    assert [row["id"] for row in done.json()] == [theirs["id"]]


async def test_the_inbox_refusal_is_the_apps_guards(world: World) -> None:
    refused = await world.request("GET", BASE, AUTHOR)
    assert refused.status_code == 403 and refused.json() == {"detail": "Insufficient role"}
    assert world.repository.calls == []


# --- update ------------------------------------------------------------------------------


async def test_a_patch_in_keksdoses_order(world: World) -> None:
    row = await world.file(AUTHOR)
    url = f"{BASE}/{row['id']}"
    edited = await world.request("PATCH", url, AUTHOR, json={"title": "Sync is very slow"})
    assert edited.status_code == 200 and edited.json()["title"] == "Sync is very slow"
    assert world.repository.calls == ["get", "apply"]

    foreign = await world.request("PATCH", url, STRANGER, json={"title": "mine"})
    assert foreign.status_code == 403 and foreign.json() == {"detail": "Not the author"}
    missing = await world.request("PATCH", f"{BASE}/999", ADMIN, json={"title": "x"})
    assert missing.status_code == 404 and missing.json() == {"detail": "Feedback not found"}
    crash = await world.request("PATCH", f"{BASE}/999", ADMIN, json={"category": "CRASH"})
    assert crash.status_code == 422  # before the lookup
    null = await world.request("PATCH", url, AUTHOR, json={"title": None})
    assert null.status_code == 422
    long = await world.request("PATCH", url, ADMIN, json={"outcome": "x" * 4001})
    assert long.status_code == 422  # the app's update model
    done = await world.request("PATCH", url, ADMIN, json={"status": "DONE"})
    assert done.json()["resolved_at"] == NOW.isoformat().replace("+00:00", "Z")


async def test_a_foreign_row_is_a_404_where_the_app_chose_it() -> None:
    world = World(foreign_row="not_found")
    async with world.client:
        row = await world.file(AUTHOR)
        foreign = await world.request("PATCH", f"{BASE}/{row['id']}", STRANGER, json={"title": "mine"})
        assert foreign.status_code == 404 and foreign.json() == {"detail": "Feedback not found"}


# --- the guards --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "what"),
    [
        ("POST", f"{BASE}/attachments", "uploads"),
        ("POST", BASE, "feedback"),
        ("PATCH", f"{BASE}/1", "feedback"),
    ],
)
async def test_the_demo_guard_answers_before_the_body_is_validated(
    world: World, method: str, path: str, what: str
) -> None:
    """KS's walk (``test_demo.py:284-302``): ``json={}`` from a demo is a 403
    ``demo_refused``, never a 422 — the guard is a route dependency."""
    refused = await world.request(method, path, DEMO, json={})
    assert refused.status_code == 403
    assert refused.json() == {"detail": f"Not possible for a demo account: {what}", "code": "demo_refused"}
    assert world.repository.calls == []


async def test_the_demo_may_read(world: World) -> None:
    assert (await world.request("GET", f"{BASE}/my", DEMO)).status_code == 200


def _depends_on(route: APIRoute) -> set[str]:
    """Kurvenschmiede's walk (``route_table.py:38-62``): every dependency's qualname."""
    names: set[str] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        for sub in dependant.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__qualname__", repr(sub.call)))
            stack.append(sub)
    return names


def test_a_route_walk_finds_the_apps_own_guards() -> None:
    routes = {
        f"{method} /api/v1{route.path}": route
        for route in World().router.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set()) - {"HEAD"}
    }
    demo = {name for name, route in routes.items() if any(n.startswith("not_in_the_demo.") for n in _depends_on(route))}
    assert demo == {f"POST {BASE}/attachments", f"POST {BASE}", f"PATCH {BASE}/{{feedback_id}}"}
    admin = {name for name, route in routes.items() if "require_admin" in _depends_on(route)}
    assert admin == {f"GET {BASE}"}
    assert all("current_actor" in _depends_on(route) for route in routes.values())
    # The guard is the route's own first dependency, so it runs before anything else.
    for name in demo:
        assert getattr(routes[name].dependencies[0].dependency, "__qualname__", "").startswith("not_in_the_demo.")
    assert routes[f"GET {BASE}"].dependencies[0].dependency is require_admin


# --- the OpenAPI document ----------------------------------------------------------------


def test_keksdoses_operation_ids_and_components_stay() -> None:
    """kk ``frontend/src/shared/api/openapi-schema.d.ts:2401-2513``."""
    document = World().app.openapi()
    operations = {
        (path, method): operation["operationId"]
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }
    assert operations == {
        (f"{BASE}/attachments", "post"): "upload_attachment_api_v1_feedback_attachments_post",
        (f"{BASE}/attachments/{{key}}", "get"): "download_attachment_api_v1_feedback_attachments__key__get",
        (BASE, "post"): "create_api_v1_feedback_post",
        (f"{BASE}/crash", "post"): "report_crash_api_v1_feedback_crash_post",
        (f"{BASE}/my", "get"): "list_my_api_v1_feedback_my_get",
        (BASE, "get"): "list_all_api_v1_feedback_get",
        (f"{BASE}/{{feedback_id}}", "patch"): "update_api_v1_feedback__feedback_id__patch",
    }
    components = document["components"]["schemas"]
    body = components["Body_upload_attachment_api_v1_feedback_attachments_post"]
    assert list(body["properties"]) == ["file"] and body["required"] == ["file"]
    assert {"AppFeedbackCreate", "AppFeedbackResponse", "AppFeedbackUpdate", "CrashReportCreate"} <= set(components)
    inbox = document["paths"][BASE]["get"]
    assert [parameter["name"] for parameter in inbox["parameters"] if parameter["in"] == "query"] == ["status_filter"]
    assert inbox["tags"] == ["feedback"]
    assert document["paths"][f"{BASE}/attachments"]["post"]["responses"].keys() >= {"201", "422"}


def test_the_routes_are_in_the_contracts_order() -> None:
    order = [
        (sorted(route.methods or ())[0], f"/api/v1{route.path}")
        for route in World().router.routes
        if isinstance(route, APIRoute)
    ]
    assert order == [
        ("POST", f"{BASE}/attachments"),
        ("GET", f"{BASE}/attachments/{{key}}"),
        ("POST", BASE),
        ("POST", f"{BASE}/crash"),
        ("GET", f"{BASE}/my"),
        ("GET", BASE),
        ("PATCH", f"{BASE}/{{feedback_id}}"),
    ]


# --- the factory -------------------------------------------------------------------------


def test_two_policies_are_refused() -> None:
    """A report could name a key the download refuses."""
    with pytest.raises(ValueError, match=r"attachment_url_policy is not deps\.policy"):
        World(create_model=FeedbackCreate)
    # The same pattern and prefix is the same policy.
    World(policy=AttachmentUrlPolicy(SHA12_KEY_PATTERN))


async def test_the_app_chooses_the_prefix_the_tags_and_the_answer() -> None:
    seen: list[str | None] = []

    def to_response(row: MemoryFeedbackRow, address: str | None) -> FeedbackResponse:
        seen.append(address)
        return AppFeedbackResponse.model_validate(row).model_copy(update={"reworks": 7})

    deps = World(to_response=to_response, response_model=AppFeedbackResponse).deps
    app = FastAPI()
    app.include_router(feedback_router(deps, prefix="/reports", tags=("reports", "kit")), prefix="/api/v2")
    app.state.feedback_limiters = FeedbackLimiters()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        made = await client.post("/api/v2/reports", json={"title": "x"}, headers=World.signed_in(AUTHOR))
        assert made.status_code == 201 and made.json()["reworks"] == 7
        await client.get("/api/v2/reports", headers=World.signed_in(ADMIN))
    assert seen == [None, AUTHOR.email]
    assert app.openapi()["paths"]["/api/v2/reports"]["post"]["tags"] == ["reports", "kit"]


def test_the_default_limiters_live_on_app_state() -> None:
    world = World()
    assert world.deps.limiters is app_state_limiters
    assert world.deps.foreign_row == "forbidden" and world.deps.decode_images is False


async def test_check_round_trip_passes_on_the_fakes() -> None:
    world = World(decode_images=True)
    async with world.client:
        await check_round_trip(world.client, policy=POLICY, headers=World.signed_in(AUTHOR))
