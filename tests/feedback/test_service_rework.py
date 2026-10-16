"""Rework as a conversation entry (feedback-harmonization §10.2, §10.3; server-kit 0.13).

The author's ``rework`` flow, who may send a report back and where it lands, an admin's
REWORK reply, the one-release PATCH conversion both ways, no notification, the views'
``kind`` and ``attachment_url``, the ``outcome`` shim, and the erasure of an entry's file.
"""

from __future__ import annotations

import logging

import pytest

from eifi1_server_kit.demo import DemoError
from eifi1_server_kit.feedback import (
    ERASED_MARK,
    REWORK_AUTHOR_ONLY,
    FeedbackActor,
    FeedbackEntryKind,
    FeedbackForbiddenError,
    FeedbackNotFoundError,
    FeedbackReplyCreate,
    FeedbackReworkCreate,
    FeedbackService,
    FeedbackStatus,
    FeedbackUpdate,
    UnknownAttachmentUrlError,
    append_rework,
)
from eifi1_server_kit.feedback.testing import InMemoryFeedbackRepository, MemoryFeedbackRow, assert_public_view
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO, NOW, POLICY
from tests.feedback.test_service_public import MEMBER, World

S = FeedbackStatus
REWORK = FeedbackEntryKind.REWORK
FILE = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"
OTHER_FILE = "/api/v1/feedback/attachments/bbbbbbbbbbbb.pdf"


@pytest.fixture
def world() -> World:
    return World()


async def _answered(world: World, person: FeedbackActor = AUTHOR, status: FeedbackStatus = S.DONE) -> MemoryFeedbackRow:
    row = await world.file(person)
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Fixed.", status=status))
    world.clear()
    world.replies.added.clear()
    world.replies.notified.clear()
    return row


# --- the author's flow ------------------------------------------------------------------------


async def test_a_members_rework_is_an_entry_and_goes_back_to_open(world: World) -> None:
    row = await _answered(world)
    assert row.resolved_at is not None
    sent = await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body=" Still slow. ", attachment_url=FILE))
    assert world.repository.calls == ["get", "apply"] and world.replies.calls == ["add"]
    assert sent.status is S.OPEN and sent.resolved_at is None
    assert sent.body == "b", "nothing is appended to the body any more"
    [(entry, by)] = world.replies.added
    assert (entry.kind, entry.author_id, entry.body, entry.attachment_url, by) == (
        REWORK,
        AUTHOR.id,
        "Still slow.",
        FILE,
        AUTHOR,
    )
    assert (entry.status_from, entry.status_to) == (S.DONE, S.OPEN)
    assert world.replies.notified == [], "a rework entry fires no feedback_answered"


async def test_an_admins_own_rework_lands_rework_and_a_second_one_adds_an_entry(world: World) -> None:
    row = await _answered(world, ADMIN, S.NEEDS_LIVE_TEST)
    first = await world.service.rework(ADMIN, row.id, FeedbackReworkCreate(body="Not on the iPad."))
    assert first.status is S.REWORK
    second = await world.service.rework(ADMIN, row.id, FeedbackReworkCreate(body="Nor the phone."))
    assert second.status is S.REWORK
    assert [(entry.status_from, entry.status_to) for entry, _ in world.replies.added] == [
        (S.NEEDS_LIVE_TEST, S.REWORK),
        (S.REWORK, S.REWORK),
    ]


async def test_only_the_author_sends_a_report_back(world: World) -> None:
    row = await _answered(world)
    payload = FeedbackReworkCreate(body="x")
    for someone in (ADMIN, MEMBER):
        with pytest.raises(FeedbackForbiddenError, match=REWORK_AUTHOR_ONLY):
            await world.service.rework(someone, row.id, payload)
    with pytest.raises(DemoError):
        await world.service.rework(DEMO, row.id, payload)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.rework(AUTHOR, 999, payload)
    with pytest.raises(UnknownAttachmentUrlError):
        await world.service.rework(
            AUTHOR, row.id, FeedbackReworkCreate(body="x", attachment_url="/api/v1/feedback/attachments/x.png")
        )
    fresh = await world.file()
    with pytest.raises(FeedbackForbiddenError, match="Entry is in OPEN"):
        await world.service.rework(AUTHOR, fresh.id, payload)
    assert world.replies.added == [], "nothing written by a refusal"
    # Kurvenschmiede: a foreign row is a 404 to a non-admin; an admin still gets the 403.
    ks = World(foreign_row="not_found")
    other = await _answered(ks)
    with pytest.raises(FeedbackNotFoundError):
        await ks.service.rework(MEMBER, other.id, payload)
    with pytest.raises(FeedbackForbiddenError, match=REWORK_AUTHOR_ONLY):
        await ks.service.rework(ADMIN, other.id, payload)


async def test_a_rework_without_a_reply_store_says_so() -> None:
    bare = FeedbackService(InMemoryFeedbackRepository(), policy=POLICY)
    with pytest.raises(RuntimeError, match="no reply store"):
        await bare.rework(AUTHOR, 1, FeedbackReworkCreate(body="x"))


async def test_an_admins_rework_reply_is_a_reply_and_notifies(world: World) -> None:
    row = await _answered(world)
    sent = await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Also the iPad, please.", status=S.REWORK))
    assert sent.status is S.REWORK and sent.resolved_at is None
    [(entry, _)] = world.replies.notified
    assert (entry.kind, entry.status_to, entry.attachment_url) == (FeedbackEntryKind.REPLY, S.REWORK, None)


# --- the legacy PATCH append (0.13 only) ------------------------------------------------------


async def test_an_old_clients_append_is_written_as_the_authors_entry(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    row = await _answered(world)
    appended = append_rework(row.body, "still broken", FILE, now=NOW)
    assert appended is not None
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.feedback"):
        sent = await world.service.update(AUTHOR, row.id, FeedbackUpdate(body=appended))
    assert "feedback.legacy_rework_append" in caplog.text
    assert sent.body == "b" and sent.status is S.OPEN and sent.resolved_at is None
    assert world.repository.calls == ["get", "apply"] and world.replies.calls == ["add"]
    [(entry, _)] = world.replies.added
    assert (entry.kind, entry.body, entry.attachment_url, entry.status_from, entry.status_to) == (
        REWORK,
        "still broken",
        FILE,
        S.DONE,
        S.OPEN,
    )
    assert world.replies.notified == []


async def test_an_admins_append_to_someone_elses_report_is_a_rework_reply(world: World) -> None:
    row = await _answered(world)
    appended = append_rework(row.body, "please look again", now=NOW)
    assert appended is not None
    sent = await world.service.update(ADMIN, row.id, FeedbackUpdate(body=appended))
    assert sent.body == "b" and sent.status is S.REWORK
    [(entry, by)] = world.replies.notified
    assert (entry.kind, entry.author_id, entry.body, entry.status_to, by) == (
        FeedbackEntryKind.REPLY,
        ADMIN.id,
        "please look again",
        S.REWORK,
        ADMIN,
    )
    # An admin's append to their own report is their rework entry.
    own = await _answered(world, ADMIN)
    again = append_rework(own.body, "mine", now=NOW)
    assert again is not None
    assert (await world.service.update(ADMIN, own.id, FeedbackUpdate(body=again))).status is S.REWORK
    assert [entry.kind for entry, _ in world.replies.added] == [REWORK]


# --- the views ---------------------------------------------------------------------------------


async def test_the_views_carry_the_kind_and_the_file_only_for_admin_and_author(world: World) -> None:
    row = await _answered(world)
    await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body=f"Again, see {FILE}", attachment_url=FILE))
    [admin] = await world.service.admin_view(ADMIN, [(row, None)])
    [author] = await world.service.author_view(AUTHOR, [row])
    assert admin.replies is not None and author.replies is not None
    assert [(entry.kind, entry.attachment_url) for entry in admin.replies] == [
        (FeedbackEntryKind.REPLY, None),
        (REWORK, FILE),
    ]
    assert (admin.replies[1].author_name, admin.replies[1].status_from) == ("Ada Author", S.DONE)
    assert (author.replies[1].kind, author.replies[1].attachment_url, author.replies[1].status_from) == (
        REWORK,
        FILE,
        None,
    )
    # The outcome shim reads the newest reply, never a rework entry (§10.3).
    assert admin.outcome == "Fixed." and author.outcome == "Fixed."
    detail = await world.service.public_view(MEMBER, await world.service.get_public(MEMBER, row.id))
    assert [entry.kind for entry in detail.replies] == [FeedbackEntryKind.REPLY, REWORK]
    assert detail.replies[1].author_name == "Ada"
    assert_public_view(detail, policy=POLICY)
    assert "attachment_url" not in detail.model_dump()["replies"][1]


async def test_an_erased_authors_rework_entry_is_kept_and_its_file_purged(world: World) -> None:
    world.repository.replies = world.replies
    row = await _answered(world)
    await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body=f"Still, {AUTHOR.email}", attachment_url=FILE))
    shared = await _answered(world, ADMIN)
    await world.service.rework(ADMIN, shared.id, FeedbackReworkCreate(body="mine too", attachment_url=OTHER_FILE))
    row.status = S.DONE
    await world.service.rework(
        AUTHOR, row.id, FeedbackReworkCreate(body="the same file as the admin's", attachment_url=OTHER_FILE)
    )
    erased = await world.service.erase_author(AUTHOR.id, [AUTHOR.email])
    assert erased.keys == ("aaaaaaaaaaaa.png",), "a file another report's entry names is kept"
    assert erased.replies == 2
    mine = [entry for entry in world.replies.rows.values() if entry.feedback_id == row.id and entry.kind is REWORK]
    assert all(entry.author_id is None and entry.attachment_url is None for entry in mine)
    assert mine[0].body == f"Still, {ERASED_MARK}"
    kept = next(
        entry for entry in world.replies.rows.values() if entry.feedback_id == shared.id and entry.kind is REWORK
    )
    assert (kept.author_id, kept.attachment_url) == (ADMIN.id, OTHER_FILE)


async def test_an_erased_admin_with_entries_but_no_reports_still_purges_their_files() -> None:
    world = World()
    row = await _answered(world, ADMIN)
    await world.service.rework(ADMIN, row.id, FeedbackReworkCreate(body="x", attachment_url=FILE))
    row.user_id = MEMBER.id  # the report changed hands
    erased = await world.service.erase_author(ADMIN.id, [ADMIN.email])
    assert (erased.reports, erased.replies, erased.keys) == (0, 2, ("aaaaaaaaaaaa.png",))
