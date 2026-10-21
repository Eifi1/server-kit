"""Attachments on admin replies and public comments (feedback-harmonization §12, server-kit 0.18).

keksdose run 82 R1 (live #447): an admin answers with a reply, and the reply composer
took no file, so a pasted screenshot was gone. A reply now carries one file in the
existing ``feedback_replies.attachment_url``, a comment one in the new
``feedback_comments.attachment_url``: the same policy and limits as the rework note. A
reply's file stays with the admin and the author (never the public detail); a comment's
is shown with the comment to whoever reads its body, and goes with it when it is hidden.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from eifi1_server_kit.feedback import (
    PUBLIC_COMMENT_FIELDS,
    UUID32_SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    FeedbackCommentCreate,
    FeedbackCommentFilePurge,
    FeedbackCommentHide,
    FeedbackEntryKind,
    FeedbackReplyCreate,
    FeedbackReplyResponse,
    FeedbackStatus,
    NewFeedbackComment,
    UnknownAttachmentUrlError,
)
from eifi1_server_kit.feedback.testing import (
    InMemoryCommentStore,
    MemoryCommentRow,
    assert_public_view,
    check_comment_store,
)
from tests.feedback._layer2 import ADMIN, AUTHOR, POLICY, STRANGER
from tests.feedback.test_service_public import MEMBER, World

FILE = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"
OTHER_FILE = "/api/v1/feedback/attachments/bbbbbbbbbbbb.pdf"
FOREIGN = "https://evil.example/x.png"
KASTLAN = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)


@pytest.fixture
def world() -> World:
    return World()


# --- the bodies ---------------------------------------------------------------------------------


@pytest.mark.parametrize("model", [FeedbackReplyCreate, FeedbackCommentCreate])
def test_a_reply_and_a_comment_take_one_file_of_this_app(model: type[Any]) -> None:
    assert model(body=" x ").attachment_url is None, "a file is optional"
    assert model(body="x", attachment_url=FILE).attachment_url == FILE
    with pytest.raises(ValidationError, match="not a feedback attachment URL"):
        model(body="x", attachment_url=FOREIGN)
    with pytest.raises(ValidationError):
        model(body="x", attachment_url="/api/v1/feedback/attachments/" + "a" * 500 + ".png")
    with pytest.raises(ValidationError):
        model(body="x", attachment_urls=[FILE])  # one file, and no other field
    with pytest.raises(ValidationError):
        model(body=" ", attachment_url=FILE)  # a file does not stand in for the words


@pytest.mark.parametrize("model", [FeedbackReplyCreate, FeedbackCommentCreate])
def test_an_app_narrows_the_files_policy_in_its_subclass(model: type[Any]) -> None:
    class Narrowed(model):  # type: ignore[misc]
        attachment_url_policy: ClassVar[AttachmentUrlPolicy] = KASTLAN

    with pytest.raises(ValidationError, match="not a feedback attachment URL"):
        Narrowed(body="x", attachment_url=FILE)


def test_a_comments_file_is_one_of_its_public_fields() -> None:
    assert "attachment_url" in PUBLIC_COMMENT_FIELDS


# --- the admin's reply --------------------------------------------------------------------------


async def test_an_admins_reply_keeps_its_file_for_the_admin_and_the_author(world: World) -> None:
    row = await world.file()
    await world.service.reply(
        ADMIN, row.id, FeedbackReplyCreate(body="Like this?", status=FeedbackStatus.REWORK, attachment_url=FILE)
    )
    [(stored, by)] = world.replies.added
    assert (stored.kind, stored.attachment_url, stored.status_to, by) == (
        FeedbackEntryKind.REPLY,
        FILE,
        FeedbackStatus.REWORK,
        ADMIN,
    )
    [admin_view] = await world.service.admin_view(ADMIN, [(row, None)])
    [author_view] = await world.service.author_view(AUTHOR, [row])
    for view in (admin_view, author_view):
        [reply] = view.replies or []
        assert isinstance(reply, FeedbackReplyResponse) and reply.attachment_url == FILE
    detail = await world.service.public_view(MEMBER, await world.service.get_public(MEMBER, row.id))
    assert "attachment_url" not in detail.replies[0].model_dump(), "never in the public detail"
    assert_public_view(detail, policy=POLICY)


async def test_the_service_checks_a_replys_file_against_its_own_policy() -> None:
    world = World(policy=KASTLAN)
    row = await world.file()
    with pytest.raises(UnknownAttachmentUrlError):
        await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="x", attachment_url=FILE))
    assert not world.replies.added, "refused before anything is written"


# --- a comment ----------------------------------------------------------------------------------


async def test_a_comments_file_is_shown_with_its_body(world: World) -> None:
    row = await world.file()
    mine = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="Here too", attachment_url=FILE))
    assert mine.attachment_url == FILE
    assert world.comments.rows[mine.id].attachment_url == FILE
    other = dataclasses.replace(STRANGER, id=6, role="MEMBER")
    seen = await world.service.comments(other, row.id)
    assert seen[0].attachment_url == FILE, "a commenter shows the file to whoever reads the comment"
    assert_public_view(seen, policy=POLICY)

    await world.service.hide_comment(ADMIN, row.id, mine.id, FeedbackCommentHide(hidden=True))
    hidden = await world.service.comments(other, row.id)
    assert (hidden[0].body, hidden[0].attachment_url) == (None, None), "hidden: the file goes with the words"
    for person in (MEMBER, ADMIN):
        assert (await world.service.comments(person, row.id))[0].attachment_url == FILE
    plain = await world.service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="Thanks"))
    assert plain.attachment_url is None


async def test_the_service_checks_a_comments_file_against_its_own_policy() -> None:
    world = World(policy=KASTLAN)
    row = await world.file()
    with pytest.raises(UnknownAttachmentUrlError):
        await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x", attachment_url=FILE))
    assert not world.comments.rows


def test_assert_public_view_lets_a_comment_carry_its_own_file_only() -> None:
    comment = {"id": 1, "author_name": "Sam", "mine": False, "staff": False, "body": "b", "hidden": False}
    assert_public_view({**comment, "attachment_url": FILE}, policy=POLICY)
    assert_public_view({**comment, "attachment_url": None}, policy=POLICY)
    with pytest.raises(AssertionError, match="names an attachment URL"):
        assert_public_view({**comment, "attachment_url": FOREIGN}, policy=POLICY)
    with pytest.raises(AssertionError, match="names an attachment URL"):
        assert_public_view({**comment, "body": f"see {FILE}", "attachment_url": FILE}, policy=POLICY)
    reply = {"id": 1, "author_name": None, "body": "ok", "status_to": None, "created_at": "x"}
    with pytest.raises(AssertionError, match="a public reply carries"):
        assert_public_view({**reply, "attachment_url": FILE}, policy=POLICY)


# --- the erasure --------------------------------------------------------------------------------


async def test_an_erased_commenters_file_is_taken_off_and_purged(world: World) -> None:
    world.repository.replies = world.replies
    row = await world.file(STRANGER)
    await world.service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="mine", attachment_url=FILE))
    await world.service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="shared", attachment_url=OTHER_FILE))
    await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="the same", attachment_url=OTHER_FILE))
    erased = await world.service.erase_author(AUTHOR.id, [AUTHOR.email])
    assert erased.comments == 2
    assert erased.keys == ("aaaaaaaaaaaa.png",), "a file another person's comment names is kept"
    theirs = [comment for comment in world.comments.rows.values() if comment.user_id is None]
    assert len(theirs) == 2 and all(comment.attachment_url is None for comment in theirs)


async def test_a_file_an_admin_replied_with_on_the_erased_authors_report_is_kept(world: World) -> None:
    """The entries and comments are cleared first, so every one still naming a file counts,
    on any report — the erased author's own included."""
    world.repository.replies = world.replies
    row = await world.file()
    row.screenshot_url = FILE
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="This one?", attachment_url=FILE))
    await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="same", attachment_url=OTHER_FILE))
    row.attachment_urls = [OTHER_FILE]
    erased = await world.service.erase_author(AUTHOR.id, [AUTHOR.email])
    assert erased.keys == (), "the admin's reply and the member's comment still name both"


# --- a deleted comment's file: the app decides (§12.6 (2)) --------------------------------------


async def test_deleting_a_comment_answers_its_files_key_unless_another_entry_names_it(world: World) -> None:
    """keksdose and Kurvenschmiede build reviews (round 0.47): the delete answers the keys
    nothing names any more, by the erasure's shared-key check; keeping the file is the
    app's choice too."""
    world.repository.replies = world.replies
    row = await world.file()
    alone = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x", attachment_url=FILE))
    shared = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="y", attachment_url=OTHER_FILE))
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="same file", attachment_url=OTHER_FILE))
    plain = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="text"))
    assert await world.service.delete_comment(MEMBER, row.id, alone.id) == ("aaaaaaaaaaaa.png",)
    assert await world.service.delete_comment(MEMBER, row.id, shared.id) == (), "the admin's reply still names it"
    assert await world.service.delete_comment(MEMBER, row.id, plain.id) == ()
    assert world.comments.rows == {}


async def test_a_comment_store_with_purge_files_is_handed_the_keys(world: World) -> None:
    purged: list[tuple[str, ...]] = []

    class Purging(InMemoryCommentStore):
        async def purge_files(self, keys: Sequence[str]) -> None:
            assert all(comment.attachment_url != FILE for comment in self.rows.values()), "after the delete"
            purged.append(tuple(keys))

    store = Purging()
    assert isinstance(store, FeedbackCommentFilePurge)
    assert not isinstance(InMemoryCommentStore(), FeedbackCommentFilePurge), "keeping the file is the default"
    world.comments = world.repository.comments = world.service.comment_store = store
    row = await world.file()
    first = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x", attachment_url=FILE))
    twice = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="y", attachment_url=OTHER_FILE))
    await world.service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="z", attachment_url=OTHER_FILE))
    plain = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="text"))
    await world.service.delete_comment(MEMBER, row.id, first.id)
    await world.service.delete_comment(MEMBER, row.id, twice.id)
    await world.service.delete_comment(MEMBER, row.id, plain.id)
    assert purged == [("aaaaaaaaaaaa.png",)], "only a key nothing names; never an empty call"


async def test_the_delete_route_hands_the_purge_the_key() -> None:
    from tests.feedback.test_router_public import BASE
    from tests.feedback.test_router_public import World as RouterWorld

    world = RouterWorld()
    purged: list[tuple[str, ...]] = []

    async def purge_files(keys: Sequence[str]) -> None:
        purged.append(tuple(keys))

    world.comments.purge_files = purge_files  # type: ignore[attr-defined]
    async with world.client:
        row = await world.file()
        made = await world.request(
            "POST", f"{BASE}/{row['id']}/comments", MEMBER, json={"body": "This?", "attachment_url": OTHER_FILE}
        )
        assert made.status_code == 201, made.text
        gone = await world.request("DELETE", f"{BASE}/{row['id']}/comments/{made.json()['id']}", MEMBER)
        assert gone.status_code == 204
    assert purged == [("bbbbbbbbbbbb.pdf",)], "the report's body names its own file, not this one"


# --- the fakes and the conformance check --------------------------------------------------------


async def test_the_comment_fake_passes_its_check_with_a_file() -> None:
    await check_comment_store(InMemoryCommentStore(), feedback_id=1, author=AUTHOR, other=STRANGER, admin=ADMIN)
    urls: list[str | None] = []

    class Recording(InMemoryCommentStore):
        async def add(self, new: NewFeedbackComment, *, actor: Any) -> MemoryCommentRow:
            urls.append(new.attachment_url)
            return await super().add(new, actor=actor)

    await check_comment_store(
        Recording(), feedback_id=1, author=AUTHOR, other=STRANGER, admin=ADMIN, policy=AttachmentUrlPolicy(prefix="/x/")
    )
    assert any((url or "").startswith("/x/check") for url in urls), "the app's own prefix"
    stored = "/api/v1/app-files/abc.png"
    await check_comment_store(
        Recording(), feedback_id=1, author=AUTHOR, other=STRANGER, admin=ADMIN, attachment_url=stored
    )
    assert stored in urls


async def test_a_comment_store_that_loses_the_file_fails_its_check() -> None:
    class Dropping(InMemoryCommentStore):
        async def add(self, new: NewFeedbackComment, *, actor: Any) -> MemoryCommentRow:
            return await super().add(dataclasses.replace(new, attachment_url=None), actor=actor)

    with pytest.raises(AssertionError, match="add: attachment_url reads back"):
        await check_comment_store(Dropping(), feedback_id=1, author=AUTHOR, other=STRANGER, admin=ADMIN)

    class Sticky(InMemoryCommentStore):
        async def apply(self, comment: MemoryCommentRow, changes: Any, *, actor: Any) -> MemoryCommentRow:
            return await super().apply(
                comment, {k: v for k, v in changes.items() if k != "attachment_url"}, actor=actor
            )

    with pytest.raises(AssertionError, match="apply: attachment_url reads back"):
        await check_comment_store(Sticky(), feedback_id=1, author=AUTHOR, other=STRANGER, admin=ADMIN)


# --- over HTTP ----------------------------------------------------------------------------------


async def test_the_routes_take_a_file_and_answer_it() -> None:
    from tests.feedback.test_router_public import BASE, SHOT
    from tests.feedback.test_router_public import World as RouterWorld

    world = RouterWorld()
    async with world.client:
        row = await world.file()
        replied = await world.request(
            "POST", f"{BASE}/{row['id']}/replies", ADMIN, json={"body": "This?", "attachment_url": SHOT}
        )
        assert replied.status_code == 201, replied.text
        assert replied.json()["replies"][-1]["attachment_url"] == SHOT
        commented = await world.request(
            "POST", f"{BASE}/{row['id']}/comments", MEMBER, json={"body": "Here too", "attachment_url": SHOT}
        )
        assert commented.status_code == 201, commented.text
        assert commented.json()["attachment_url"] == SHOT
        listed = await world.request("GET", f"{BASE}/{row['id']}/comments", MEMBER)
        assert listed.json()[0]["attachment_url"] == SHOT
        for path, person in (("replies", ADMIN), ("comments", MEMBER)):
            bad = await world.request(
                "POST", f"{BASE}/{row['id']}/{path}", person, json={"body": "x", "attachment_url": FOREIGN}
            )
            assert bad.status_code == 422, (path, bad.text)


async def test_the_app_may_bring_its_own_reply_and_comment_models() -> None:
    from tests.feedback.test_router_public import BASE, SHOT
    from tests.feedback.test_router_public import World as RouterWorld

    class KastlanReply(FeedbackReplyCreate):
        attachment_url_policy: ClassVar[AttachmentUrlPolicy] = KASTLAN

    class KastlanComment(FeedbackCommentCreate):
        attachment_url_policy: ClassVar[AttachmentUrlPolicy] = KASTLAN

    world = RouterWorld(reply_model=KastlanReply, comment_model=KastlanComment)
    async with world.client:
        row = await world.file()
        for path, person in (("replies", ADMIN), ("comments", MEMBER)):
            refused = await world.request(
                "POST", f"{BASE}/{row['id']}/{path}", person, json={"body": "x", "attachment_url": SHOT}
            )
            assert refused.status_code == 422 and "not a feedback attachment URL" in refused.text, path
