"""Public feedback's routes over HTTP (feedback-harmonization §9.9, §9.14).

The routes mounted or not, the app's guards first, 404 against 403, the 429s, the
operation ids, and :func:`~eifi1_server_kit.feedback.testing.assert_public_view` over every
public answer.

No ``from __future__ import annotations``: the app's dependencies below are read by
FastAPI, as an app's would be.
"""

import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.routing import APIRoute

from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.feedback import FeedbackActor, FeedbackCreate, FeedbackResponse
from eifi1_server_kit.feedback.router import FeedbackDeps, feedback_router
from eifi1_server_kit.feedback.testing import (
    InMemoryAttachmentStorage,
    InMemoryCommentStore,
    InMemoryPeopleLookup,
    InMemoryReplyStore,
    InMemoryVoteStore,
    MemoryFeedbackRow,
    actor,
    assert_public_view,
    fixed_clock,
)
from eifi1_server_kit.limiter import FeedbackLimiters
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO, EMAILS, NOW, POLICY, STRANGER, SpyRepository

MEMBER = dataclasses.replace(STRANGER, role="MEMBER")
CUSTOMER = actor(id=5, email="cleo@example.test", display_name="Cleo", role="CUSTOMER")
PEOPLE = {person.id: person for person in (AUTHOR, ADMIN, MEMBER, DEMO, CUSTOMER)}
NAMES = {AUTHOR.id: ("Ada", "Author"), ADMIN.id: ("Ann", "Admin"), MEMBER.id: ("Sam", "Stranger")}
BASE = "/api/v1/feedback"
SHOT = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"


async def current_actor(x_actor: Annotated[int | None, Header()] = None) -> FeedbackActor:
    if x_actor is None or x_actor not in PEOPLE:
        raise HTTPException(401, "Not authenticated")
    return PEOPLE[x_actor]


def not_in_the_demo(what: str) -> Callable[..., Any]:
    async def _check(person: Annotated[FeedbackActor, Depends(current_actor)]) -> FeedbackActor:
        refuse_demo(person.is_demo, what)
        return person

    return _check


async def require_admin(person: Annotated[FeedbackActor, Depends(current_actor)]) -> FeedbackActor:
    if not person.is_admin:
        raise HTTPException(403, "Insufficient role")
    return person


def kurvenschmiede(viewer: FeedbackActor) -> bool:
    return viewer.role != "CUSTOMER"


class AppFeedbackCreate(FeedbackCreate):
    attachment_url_policy = POLICY


class World:
    """One app with public feedback, as keksdose or Kurvenschmiede mounts it."""

    def __init__(self, *, limiters: FeedbackLimiters | None = None, **options: Any) -> None:
        clock = fixed_clock(NOW)
        self.votes = InMemoryVoteStore(now=clock)
        self.comments = InMemoryCommentStore(now=clock)
        self.replies = InMemoryReplyStore(now=clock)
        self.people = InMemoryPeopleLookup(NAMES)
        self.repository = SpyRepository(
            emails=EMAILS, now=clock, policy=POLICY, votes=self.votes, comments=self.comments
        )
        values: dict[str, Any] = {
            "actor": current_actor,
            "repository": lambda: self.repository,
            "storage": InMemoryAttachmentStorage,
            "demo_guard": not_in_the_demo,
            "admin_guard": require_admin,
            "policy": POLICY,
            "create_model": AppFeedbackCreate,
            "now": clock,
            "replies": lambda: self.replies,
            "comments": lambda: self.comments,
            "votes": lambda: self.votes,
            "people": lambda: self.people,
            "public_viewer": kurvenschmiede,
        }
        values.update(options)
        self.deps: FeedbackDeps[MemoryFeedbackRow] = FeedbackDeps(**values)
        self.app = FastAPI()
        install_contract_error_handlers(self.app)
        self.router = feedback_router(self.deps)
        self.app.include_router(self.router, prefix="/api/v1")
        self.app.state.feedback_limiters = limiters or FeedbackLimiters()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def request(self, method: str, url: str, person: FeedbackActor, **options: Any) -> httpx.Response:
        self.repository.calls.clear()
        response = await self.client.request(method, url, headers={"X-Actor": str(person.id)}, **options)
        assert self.repository.one_write_last(), self.repository.calls
        return response

    async def file(self, person: FeedbackActor = AUTHOR, title: str = "Sync is slow") -> dict[str, Any]:
        created = await self.request("POST", BASE, person, json={"title": title, "body": f"Slow.\nsee {SHOT}"})
        assert created.status_code == 201, created.text
        found: dict[str, Any] = created.json()
        return found


@pytest.fixture
async def world() -> AsyncIterator[World]:
    world = World()
    async with world.client:
        yield world


def _routes(router: Any) -> list[tuple[str, str]]:
    return [
        (sorted(route.methods or ())[0], f"/api/v1{route.path}")
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


# --- mounted or not ------------------------------------------------------------------------


def test_the_public_routes_go_before_the_patch_in_the_contracts_order() -> None:
    assert _routes(World().router) == [
        ("POST", f"{BASE}/attachments"),
        ("GET", f"{BASE}/attachments/{{key}}"),
        ("POST", BASE),
        ("POST", f"{BASE}/crash"),
        ("GET", f"{BASE}/my"),
        ("GET", BASE),
        ("GET", f"{BASE}/public"),
        ("GET", f"{BASE}/public/{{feedback_id}}"),
        ("GET", f"{BASE}/{{feedback_id}}/comments"),
        ("POST", f"{BASE}/{{feedback_id}}/comments"),
        ("PATCH", f"{BASE}/{{feedback_id}}/comments/{{comment_id}}"),
        ("DELETE", f"{BASE}/{{feedback_id}}/comments/{{comment_id}}"),
        ("PUT", f"{BASE}/{{feedback_id}}/vote"),
        ("DELETE", f"{BASE}/{{feedback_id}}/vote"),
        ("POST", f"{BASE}/{{feedback_id}}/replies"),
        ("POST", f"{BASE}/{{feedback_id}}/rework"),
        ("PATCH", f"{BASE}/{{feedback_id}}"),
    ]


def test_kastlan_mounts_the_replies_only_and_an_app_without_stores_none() -> None:
    kastlan = _routes(World(public_viewer=None, comments=None, votes=None, people=None).router)
    assert ("POST", f"{BASE}/{{feedback_id}}/replies") in kastlan and len(kastlan) == 9
    assert ("POST", f"{BASE}/{{feedback_id}}/rework") in kastlan
    assert len(_routes(World(public_viewer=None, replies=None).router)) == 7


@pytest.mark.parametrize("missing", ["comments", "votes", "people"])
def test_public_viewer_without_a_store_is_refused_at_build_time(missing: str) -> None:
    with pytest.raises(ValueError, match=f"public_viewer needs {missing}"):
        World(**{missing: None})


def test_the_new_operation_ids_are_keksdoses_names() -> None:
    document = World().app.openapi()
    operations = {operation["operationId"] for methods in document["paths"].values() for operation in methods.values()}
    for name, path, method in (
        ("list_public", "public", "get"),
        ("get_public", "public__feedback_id_", "get"),
        ("list_comments", "_feedback_id__comments", "get"),
        ("create_comment", "_feedback_id__comments", "post"),
        ("hide_comment", "_feedback_id__comments__comment_id_", "patch"),
        ("delete_comment", "_feedback_id__comments__comment_id_", "delete"),
        ("vote", "_feedback_id__vote", "put"),
        ("unvote", "_feedback_id__vote", "delete"),
        ("create_reply", "_feedback_id__replies", "post"),
    ):
        assert f"{name}_api_v1_feedback_{path}_{method}" in operations
    listed = document["paths"][f"{BASE}/public"]["get"]
    assert [parameter["name"] for parameter in listed["parameters"] if parameter["in"] == "query"] == [
        "sort",
        "status_filter",
        "category",
        "q",
        "limit",
        "offset",
    ]


def _depends_on(route: APIRoute) -> list[str]:
    return [getattr(dependency.dependency, "__qualname__", "") for dependency in route.dependencies]


def test_the_apps_guards_come_first() -> None:
    routes = {
        (method, f"/api/v1{route.path}"): route
        for route in World().router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    demo_first = [
        ("POST", f"{BASE}/{{feedback_id}}/comments"),
        ("DELETE", f"{BASE}/{{feedback_id}}/comments/{{comment_id}}"),
        ("PUT", f"{BASE}/{{feedback_id}}/vote"),
        ("DELETE", f"{BASE}/{{feedback_id}}/vote"),
    ]
    for key in demo_first:
        assert _depends_on(routes[key])[0].startswith("not_in_the_demo."), key
    # 0.11.1 (Kurvenschmiede): the admin's two writes too, the demo before the role, so a
    # demo meets `demo_refused` on every write.
    for key in (
        ("PATCH", f"{BASE}/{{feedback_id}}/comments/{{comment_id}}"),
        ("POST", f"{BASE}/{{feedback_id}}/replies"),
    ):
        names = _depends_on(routes[key])
        assert names[0].startswith("not_in_the_demo.") and names[1] == "require_admin", key
    for key in (("GET", f"{BASE}/public"), ("GET", f"{BASE}/{{feedback_id}}/comments")):
        assert _depends_on(routes[key]) == [], "a read refuses a demo with a 404, in the service"


# --- the flows ---------------------------------------------------------------------------------


async def test_a_report_comes_back_with_its_vote_and_replies(world: World) -> None:
    row = await world.file()
    assert (row["votes"], row["voted"], row["mine"], row["replies"], row["author_name"]) == (
        1,
        True,
        True,
        [],
        "Ada Author",
    )
    replied = await world.request(
        "POST", f"{BASE}/{row['id']}/replies", ADMIN, json={"body": "Fixed.", "status": "DONE"}
    )
    assert replied.status_code == 201, replied.text
    answer = replied.json()
    assert answer["status"] == "DONE" and answer["replies"][0]["status_from"] == "OPEN"
    assert answer["replies"][0]["author_name"] == "Ann Admin"
    mine = (await world.request("GET", f"{BASE}/my", AUTHOR)).json()
    assert mine[0]["replies"][0] == {**answer["replies"][0], "author_name": "Ann", "status_from": None}
    inbox = (await world.request("GET", BASE, ADMIN)).json()
    assert inbox[0]["votes"] == 1 and inbox[0]["user_email"] == AUTHOR.email
    refused = await world.request("POST", f"{BASE}/{row['id']}/replies", MEMBER, json={"body": "x"})
    assert refused.status_code == 403
    switched = await world.request("PATCH", f"{BASE}/{row['id']}", ADMIN, json={"public": False})
    assert switched.status_code == 200 and switched.json()["public"] is False
    frozen = await world.request("PATCH", f"{BASE}/{row['id']}", ADMIN, json={"outcome": "x"})
    assert frozen.status_code == 422


async def test_every_public_answer_says_only_what_the_public_may_read(world: World) -> None:
    row = await world.file()
    await world.request("POST", f"{BASE}/{row['id']}/replies", ADMIN, json={"body": f"See {SHOT}"})
    created = await world.request("POST", f"{BASE}/{row['id']}/comments", MEMBER, json={"body": f"Me too {SHOT}"})
    assert created.status_code == 201
    answers = [
        await world.request("GET", f"{BASE}/public", MEMBER),
        await world.request("GET", f"{BASE}/public?sort=votes&q=sync&limit=10&offset=0", MEMBER),
        await world.request("GET", f"{BASE}/public/{row['id']}", MEMBER),
        await world.request("GET", f"{BASE}/{row['id']}/comments", AUTHOR),
        await world.request("PUT", f"{BASE}/{row['id']}/vote", MEMBER),
        await world.request("DELETE", f"{BASE}/{row['id']}/vote", MEMBER),
    ]
    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert_public_view(answer.json(), policy=POLICY)
    listed, _, detail, comments, voted, unvoted = (answer.json() for answer in answers)
    assert [item["id"] for item in listed] == [row["id"]] and "body" not in listed[0]
    assert detail["author_name"] == "Ada" and detail["replies"][0]["author_name"] == "Ann"
    assert comments[0]["body"] == "Me too […]"
    assert (voted, unvoted) == ({"votes": 2, "voted": True}, {"votes": 1, "voted": False})


async def test_hide_delete_and_the_authors_vote(world: World) -> None:
    row = await world.file()
    comment = (await world.request("POST", f"{BASE}/{row['id']}/comments", MEMBER, json={"body": "x"})).json()
    url = f"{BASE}/{row['id']}/comments/{comment['id']}"
    hidden = await world.request("PATCH", url, ADMIN, json={"hidden": True})
    assert hidden.status_code == 200 and hidden.json()["hidden"] is True
    assert (await world.request("PATCH", url, MEMBER, json={"hidden": False})).status_code == 403
    assert (await world.request("DELETE", url, AUTHOR)).status_code == 404
    deleted = await world.request("DELETE", url, MEMBER)
    assert deleted.status_code == 204 and deleted.content == b""
    conflict = await world.request("DELETE", f"{BASE}/{row['id']}/vote", AUTHOR)
    assert conflict.status_code == 409 and conflict.json() == {"detail": "Your report counts as your vote"}


# --- 404 against 403, the demo -----------------------------------------------------------------


async def test_outsiders_get_404_and_a_demo_403_on_writes(world: World) -> None:
    row = await world.file()
    for person in (CUSTOMER, DEMO):
        assert (await world.request("GET", f"{BASE}/public", person)).status_code == 404
        assert (await world.request("GET", f"{BASE}/public/{row['id']}", person)).status_code == 404
        assert (await world.request("GET", f"{BASE}/{row['id']}/comments", person)).status_code == 404
    assert (await world.request("PUT", f"{BASE}/{row['id']}/vote", CUSTOMER)).status_code == 404
    for method, path, body in (
        ("POST", f"{BASE}/{row['id']}/comments", {}),
        ("PUT", f"{BASE}/{row['id']}/vote", None),
        ("DELETE", f"{BASE}/{row['id']}/vote", None),
        ("DELETE", f"{BASE}/{row['id']}/comments/1", None),
        # 0.11.1 (Kurvenschmiede): the admin's two writes refuse a demo as every write does,
        # `demo_refused` before the role's 403.
        ("POST", f"{BASE}/{row['id']}/replies", {"body": "x"}),
        ("PATCH", f"{BASE}/{row['id']}/comments/1", {"hidden": True}),
    ):
        refused = await world.request(method, path, DEMO, json=body)
        assert refused.status_code == 403 and refused.json()["code"] == "demo_refused", path
    assert (await world.request("GET", f"{BASE}/public?limit=101", MEMBER)).status_code == 422


async def test_where_titles_are_sealed_q_is_a_422() -> None:
    world = World(public_search=False)
    async with world.client:
        await world.file()
        assert (await world.request("GET", f"{BASE}/public?q=sync", MEMBER)).status_code == 422
        assert (await world.request("GET", f"{BASE}/public", MEMBER)).status_code == 200


# --- the windows -------------------------------------------------------------------------------


async def test_a_spent_comment_or_vote_window_is_a_429_with_retry_after() -> None:
    world = World(limiters=FeedbackLimiters(comments_per_hour=1, votes_per_hour=1))
    async with world.client:
        row = await world.file()
        assert (
            await world.request("POST", f"{BASE}/{row['id']}/comments", MEMBER, json={"body": "x"})
        ).status_code == 201
        assert (await world.request("PUT", f"{BASE}/{row['id']}/vote", MEMBER)).status_code == 200
        for method, path, body in (
            ("POST", f"{BASE}/{row['id']}/comments", {"body": "y"}),
            ("DELETE", f"{BASE}/{row['id']}/vote", None),
        ):
            spent = await world.request(method, path, MEMBER, json=body)
            assert spent.status_code == 429 and spent.headers["retry-after"], path


# --- the app's own shapes ------------------------------------------------------------------------


async def test_the_app_may_bring_its_own_response_models() -> None:
    from eifi1_server_kit.feedback import (
        FeedbackCommentResponse,
        FeedbackPublicListItem,
        FeedbackPublicResponse,
        FeedbackReplyResponse,
    )

    class Item(FeedbackPublicListItem):
        pass

    class Detail(FeedbackPublicResponse):
        pass

    class Comment(FeedbackCommentResponse):
        pass

    class Reply(FeedbackReplyResponse):
        pass

    class Answer(FeedbackResponse):
        pass

    world = World(
        public_item_model=Item,
        public_response_model=Detail,
        comment_response_model=Comment,
        reply_response_model=Reply,
        response_model=Answer,
    )
    async with world.client:
        row = await world.file()
        await world.request("POST", f"{BASE}/{row['id']}/replies", ADMIN, json={"body": "ok"})
        components = world.app.openapi()["components"]["schemas"]
        assert {"Item", "Detail", "Comment", "Answer"} <= set(components)
        assert (await world.request("GET", f"{BASE}/public/{row['id']}", MEMBER)).status_code == 200
