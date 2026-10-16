"""``POST /feedback/{feedback_id}/rework`` over HTTP (feedback-harmonization §10.3).

201 with the report and its replies, the demo guard first and no admin guard, the
author-only 403, the app's own body model, and the legacy PATCH append answered as an entry.

No ``from __future__ import annotations``: FastAPI reads the app's models below.
"""

from collections.abc import AsyncIterator
from typing import ClassVar

import pytest
from fastapi.routing import APIRoute

from eifi1_server_kit.feedback import (
    UUID32_SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    FeedbackReworkCreate,
    append_rework,
)
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO, NOW
from tests.feedback.test_router_public import BASE, MEMBER, SHOT, World, _depends_on


@pytest.fixture
async def world() -> AsyncIterator[World]:
    world = World()
    async with world.client:
        yield world


async def _done(world: World) -> int:
    row = await world.file()
    replied = await world.request(
        "POST", f"{BASE}/{row['id']}/replies", ADMIN, json={"body": "Fixed.", "status": "DONE"}
    )
    assert replied.status_code == 201, replied.text
    found: int = row["id"]
    return found


def test_the_rework_route_takes_the_demo_guard_only() -> None:
    [route] = [
        route
        for route in World().router.routes
        if isinstance(route, APIRoute) and route.path == "/feedback/{feedback_id}/rework"
    ]
    names = _depends_on(route)
    assert len(names) == 1 and names[0].startswith("not_in_the_demo."), "no admin guard: the author sends it"
    assert route.name == "create_rework" and route.status_code == 201


async def test_the_author_sends_a_report_back(world: World) -> None:
    feedback_id = await _done(world)
    sent = await world.request(
        "POST", f"{BASE}/{feedback_id}/rework", AUTHOR, json={"body": "  Still cut off.  ", "attachment_url": SHOT}
    )
    assert sent.status_code == 201, sent.text
    answer = sent.json()
    assert answer["status"] == "OPEN" and answer["body"] == "Slow.\nsee " + SHOT
    rework = answer["replies"][-1]
    assert (rework["kind"], rework["body"], rework["attachment_url"], rework["status_to"]) == (
        "rework",
        "Still cut off.",
        SHOT,
        "OPEN",
    )
    assert answer["replies"][0]["kind"] == "reply"
    inbox = (await world.request("GET", BASE, ADMIN)).json()
    assert inbox[0]["replies"][-1]["status_from"] == "DONE" and inbox[0]["outcome"] == "Fixed."


async def test_anyone_else_is_refused(world: World) -> None:
    feedback_id = await _done(world)
    for person, code in ((ADMIN, 403), (MEMBER, 403), (DEMO, 403)):
        refused = await world.request("POST", f"{BASE}/{feedback_id}/rework", person, json={"body": "x"})
        assert refused.status_code == code, (person, refused.text)
    demo = await world.request("POST", f"{BASE}/{feedback_id}/rework", DEMO, json={})
    assert demo.status_code == 403, "the demo guard answers before the body is validated"
    admin = await world.request("POST", f"{BASE}/{feedback_id}/rework", ADMIN, json={"body": "x"})
    assert "Only the author sends a report back" in admin.text
    for bad in (
        {"body": " "},
        {"body": "x", "attachment_url": "https://evil.example/x.png"},
        {"body": "x", "kind": "reply"},
    ):
        assert (await world.request("POST", f"{BASE}/{feedback_id}/rework", AUTHOR, json=bad)).status_code == 422
    missing = await world.request("POST", f"{BASE}/999/rework", AUTHOR, json={"body": "x"})
    assert missing.status_code == 404


async def test_the_app_may_bring_its_own_rework_model() -> None:
    class KastlanRework(FeedbackReworkCreate):
        attachment_url_policy: ClassVar[AttachmentUrlPolicy] = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)

    world = World(rework_model=KastlanRework)
    async with world.client:
        feedback_id = await _done(world)
        refused = await world.request(
            "POST", f"{BASE}/{feedback_id}/rework", AUTHOR, json={"body": "x", "attachment_url": SHOT}
        )
        assert refused.status_code == 422 and "not a feedback attachment URL" in refused.text


async def test_the_legacy_append_comes_back_as_an_entry(world: World) -> None:
    feedback_id = await _done(world)
    body = "Slow.\nsee " + SHOT
    appended = append_rework(body, "again", now=NOW)
    patched = await world.request("PATCH", f"{BASE}/{feedback_id}", AUTHOR, json={"body": appended})
    assert patched.status_code == 200, patched.text
    answer = patched.json()
    assert answer["body"] == body and answer["status"] == "OPEN"
    assert answer["replies"][-1]["kind"] == "rework" and answer["replies"][-1]["body"] == "again"
