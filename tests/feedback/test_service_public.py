"""Public feedback's flows on the fakes (feedback-harmonization §9.3, §9.6–§9.9, §9.14).

Each new method once; the author's vote; the 409; the demo and the predicate; a CRASH
never public; a switched-off report; hidden comments per viewer; one call per store for a
page; one write per store, the report last.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from eifi1_server_kit.demo import DemoError
from eifi1_server_kit.feedback import (
    COMMENTS_THROTTLED_DETAIL,
    REMOVED_URL,
    VOTES_THROTTLED_DETAIL,
    CrashReportCreate,
    FeedbackActor,
    FeedbackCategory,
    FeedbackCommentCreate,
    FeedbackCommentHide,
    FeedbackConflictError,
    FeedbackCreate,
    FeedbackForbiddenError,
    FeedbackNotFoundError,
    FeedbackPublicReplyResponse,
    FeedbackReplyCreate,
    FeedbackResponse,
    FeedbackService,
    FeedbackStatus,
    FeedbackThrottledError,
    FeedbackUpdate,
    FeedbackValidationError,
    NewFeedbackReply,
)
from eifi1_server_kit.feedback.testing import (
    InMemoryCommentStore,
    InMemoryFeedbackRepository,
    InMemoryPeopleLookup,
    InMemoryReplyStore,
    InMemoryVoteStore,
    MemoryFeedbackRow,
    MemoryReplyRow,
    actor,
    assert_public_view,
)
from eifi1_server_kit.limiter import SlidingWindowRateLimiter
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO, EMAILS, NOW, POLICY, STRANGER, SpyRepository

CUSTOMER = actor(id=5, email="cleo@example.test", display_name="Cleo", role="CUSTOMER")
MEMBER = dataclasses.replace(STRANGER, role="MEMBER")
NAMES = {
    AUTHOR.id: ("Ada", "Author"),
    ADMIN.id: ("Ann", "Admin"),
    STRANGER.id: ("Sam", "Stranger"),
    CUSTOMER.id: ("Cleo Customer", ""),  # an old display name moved whole into first_name
}
SHOT = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"


def kurvenschmiede(viewer: FeedbackActor) -> bool:
    """KS's predicate: never a CUSTOMER."""
    return viewer.role != "CUSTOMER"


class Create(FeedbackCreate):
    attachment_url_policy = POLICY


def ticking() -> Callable[[], datetime]:
    moments = iter(NOW + timedelta(minutes=minute) for minute in range(10_000))
    return lambda: next(moments)


class World:
    """One app with every public part, on fakes that log their calls."""

    def __init__(
        self, *, public_viewer: Callable[[FeedbackActor], bool] | None = kurvenschmiede, **options: Any
    ) -> None:
        clock = ticking()
        self.votes = InMemoryVoteStore(now=clock)
        self.comments = InMemoryCommentStore(now=clock)
        self.replies = InMemoryReplyStore(now=clock)
        self.people = InMemoryPeopleLookup(NAMES)
        self.repository = SpyRepository(
            emails=EMAILS, now=clock, policy=POLICY, votes=self.votes, comments=self.comments
        )
        values: dict[str, Any] = {
            "policy": POLICY,
            "now": clock,
            "replies": self.replies,
            "comments": self.comments,
            "votes": self.votes,
            "people": self.people,
            "public_viewer": public_viewer,
        }
        values.update(options)
        self.service: FeedbackService[MemoryFeedbackRow] = FeedbackService(self.repository, **values)

    async def file(self, person: FeedbackActor = AUTHOR, **fields: Any) -> MemoryFeedbackRow:
        row = await self.service.create(person, Create(title=fields.pop("title", "Sync is slow"), body="b"))
        for name, value in fields.items():
            setattr(row, name, value)
        return row

    def clear(self) -> None:
        for store in (self.votes, self.comments, self.replies, self.people):
            store.calls.clear()
        self.repository.calls.clear()


@pytest.fixture
def world() -> World:
    return World()


# --- construction ------------------------------------------------------------------------


def test_public_viewer_needs_the_stores_and_the_public_reads() -> None:
    with pytest.raises(ValueError, match="public_viewer needs comments, people"):
        World(comments=None, people=None)
    with pytest.raises(ValueError, match="public_viewer needs a repository with public_page"):
        FeedbackService(
            object(),  # type: ignore[arg-type]
            policy=POLICY,
            comments=InMemoryCommentStore(),
            votes=InMemoryVoteStore(),
            people=InMemoryPeopleLookup(),
            public_viewer=kurvenschmiede,
        )
    # Without public parts nothing is required: 0.10's service.
    FeedbackService(InMemoryFeedbackRepository(), policy=POLICY)


async def test_a_flow_without_its_store_says_so() -> None:
    bare = FeedbackService(InMemoryFeedbackRepository(), policy=POLICY)
    with pytest.raises(RuntimeError, match="no reply store"):
        await bare.reply(ADMIN, 1, FeedbackReplyCreate(body="x"))
    with pytest.raises(RuntimeError, match="no comment store"):
        await bare.comment(AUTHOR, 1, FeedbackCommentCreate(body="x"))
    with pytest.raises(RuntimeError, match="no vote store"):
        await bare.vote(AUTHOR, 1)
    no_reads: FeedbackService[MemoryFeedbackRow] = FeedbackService(object(), policy=POLICY, votes=InMemoryVoteStore())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="no public reads"):
        await no_reads.vote(AUTHOR, 1)


# --- who sees the public parts -------------------------------------------------------------


def test_sees_public_never_a_demo_always_an_admin_else_the_predicate(world: World) -> None:
    assert world.service.sees_public(ADMIN) and world.service.sees_public(MEMBER)
    assert not world.service.sees_public(CUSTOMER)
    assert not world.service.sees_public(dataclasses.replace(ADMIN, is_demo=True))
    assert not World(public_viewer=None).service.sees_public(ADMIN), "no public parts: nobody"


# --- the author's vote, a crash ---------------------------------------------------------------


async def test_the_authors_report_is_their_vote_and_a_crash_is_private(world: World) -> None:
    row = await world.file()
    assert world.votes.rows.keys() == {(row.id, AUTHOR.id)} and row.public
    crash = await world.service.report_crash(AUTHOR, CrashReportCreate(message="x is undefined"))
    stored = world.repository.rows[crash.feedback_id or 0]
    assert stored.public is False and stored.category is FeedbackCategory.CRASH
    assert len(world.votes.rows) == 1, "a crash gets no vote"
    # Without a vote store, a create is 0.10's.
    plain = World(public_viewer=None, votes=None)
    await plain.file()
    assert plain.repository.calls[-1] == "add"


# --- the public list -------------------------------------------------------------------------


async def test_the_public_list_holds_public_reports_only_with_first_names(world: World) -> None:
    wanted = await world.file(title="Wanted")
    private = await world.file(title="Private", public=False)
    await world.service.report_crash(AUTHOR, CrashReportCreate(message="boom"))
    newer = await world.file(CUSTOMER, title="Newer")
    await world.service.vote(MEMBER, wanted.id)
    world.clear()

    entries = await world.service.list_public(MEMBER)
    assert [entry.row.id for entry in entries] == [newer.id, wanted.id]
    assert private.id not in {entry.row.id for entry in entries}
    items = await world.service.public_item(MEMBER, entries)
    assert world.people.calls == ["names"], "one call for the page"
    by_id = {item.id: item for item in items}
    assert by_id[wanted.id].author_name == "Ada" and by_id[newer.id].author_name is None, "no first-word guessing"
    assert (by_id[wanted.id].votes, by_id[wanted.id].voted, by_id[wanted.id].mine) == (2, True, False)
    assert by_id[newer.id].votes == 1 and not by_id[newer.id].voted
    assert_public_view([item.model_dump(mode="json") for item in items], policy=POLICY)

    most = await world.service.list_public(MEMBER, sort="votes")
    assert [entry.row.id for entry in most] == [wanted.id, newer.id]
    searched = await world.service.list_public(ADMIN, q="  want ", status=FeedbackStatus.OPEN, limit=1)
    assert [entry.row.id for entry in searched] == [wanted.id]
    assert await world.service.list_public(MEMBER, category=FeedbackCategory.CRASH) == []
    assert await world.service.public_item(MEMBER, []) == []


async def test_the_public_list_refuses_outsiders_and_bad_pages(world: World) -> None:
    for outsider in (CUSTOMER, DEMO, dataclasses.replace(ADMIN, is_demo=True)):
        with pytest.raises(FeedbackNotFoundError):
            await world.service.list_public(outsider)
    for limit, offset in ((0, 0), (101, 0), (50, -1)):
        with pytest.raises(FeedbackValidationError, match="limit is 1–100"):
            await world.service.list_public(MEMBER, limit=limit, offset=offset)
    sealed = World(public_search=False)
    with pytest.raises(FeedbackValidationError, match="q is not offered here"):
        await sealed.service.list_public(MEMBER, q="slow")
    assert await sealed.service.list_public(MEMBER, q="   ") == []


# --- the public detail ------------------------------------------------------------------------


async def test_the_public_detail_says_only_what_the_public_may_read(world: World) -> None:
    body = f"Slow.\n[screenshot] {SHOT}\n\n--- REWORK 2026-10-04 09:12 ---\nsee {SHOT}"
    row = await world.file()
    row.body = body
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body=f"Look at {SHOT}", status=FeedbackStatus.READY))
    world.replies.rows[2] = MemoryReplyRow(2, row.id, None, "A loop's answer", None, None, NOW + timedelta(days=1))
    world.clear()

    found = await world.service.get_public(MEMBER, row.id)
    detail = await world.service.public_view(MEMBER, found)
    assert {name: len(calls) for name, calls in _calls(world).items()} == {
        "replies": 1,
        "comments": 1,
        "votes": 1,
        "people": 1,
    }
    assert detail.body == f"Slow.\n\n--- REWORK 2026-10-04 09:12 ---\nsee {REMOVED_URL}"
    assert [(reply.author_name, reply.body, reply.status_to) for reply in detail.replies] == [
        ("Ann", f"Look at {REMOVED_URL}", FeedbackStatus.READY),
        (None, "A loop's answer", None),
    ]
    assert all(type(reply) is FeedbackPublicReplyResponse for reply in detail.replies)
    assert (detail.author_name, detail.votes, detail.voted, detail.comment_count) == ("Ada", 1, False, 0)
    assert_public_view(detail, policy=POLICY)


async def test_the_public_detail_404s(world: World) -> None:
    row = await world.file()
    with pytest.raises(FeedbackNotFoundError):
        await world.service.get_public(CUSTOMER, row.id)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.get_public(MEMBER, 999)
    crash = await world.service.report_crash(AUTHOR, CrashReportCreate(message="boom"))
    with pytest.raises(FeedbackNotFoundError):
        await world.service.get_public(ADMIN, crash.feedback_id or 0)
    # Switched off: gone for others, still there for the author and an admin.
    row.public = False
    with pytest.raises(FeedbackNotFoundError):
        await world.service.get_public(MEMBER, row.id)
    assert (await world.service.get_public(AUTHOR, row.id)).id == row.id
    assert (await world.service.get_public(ADMIN, row.id)).id == row.id


async def test_the_public_detail_without_a_reply_store() -> None:
    world = World(replies=None)
    row = await world.file()
    detail = await world.service.public_view(MEMBER, await world.service.get_public(MEMBER, row.id))
    assert detail.replies == []


def _calls(world: World) -> dict[str, list[str]]:
    return {
        "replies": world.replies.calls,
        "comments": world.comments.calls,
        "votes": world.votes.calls,
        "people": world.people.calls,
    }


# --- comments ---------------------------------------------------------------------------------


async def test_comments_per_viewer_hidden_and_stripped(world: World) -> None:
    row = await world.file()
    mine = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body=f"Same here {SHOT}"))
    assert (mine.author_name, mine.mine, mine.body, mine.hidden) == ("Sam Stranger", True, f"Same here {SHOT}", False)
    assert world.comments.added[0][1] is MEMBER, "the store's add sees the actor"
    await world.service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="Thanks"))
    hidden = await world.service.hide_comment(ADMIN, row.id, mine.id, FeedbackCommentHide(hidden=True))
    assert hidden.hidden and hidden.body == f"Same here {SHOT}" and hidden.author_name == "Sam Stranger"

    by_admin = await world.service.comments(ADMIN, row.id)
    assert [(c.body, c.hidden) for c in by_admin] == [(f"Same here {SHOT}", True), ("Thanks", False)]
    by_writer = await world.service.comments(MEMBER, row.id)
    assert by_writer[0].body == f"Same here {SHOT}" and by_writer[0].hidden and by_writer[0].mine
    by_author = await world.service.comments(AUTHOR, row.id)
    assert (by_author[0].body, by_author[0].author_name, by_author[0].hidden) == (None, None, True)
    assert (by_author[1].author_name, by_author[1].mine) == ("Ada Author", True)
    other = actor(id=6, email="o@example.test", display_name=None, role="MEMBER")
    seen = await world.service.comments(other, row.id)
    assert seen[1].author_name == "Ada" and seen[0].body is None
    assert_public_view([c.model_dump(mode="json") for c in seen], policy=POLICY)

    world.clear()
    shown = await world.service.hide_comment(ADMIN, row.id, mine.id, FeedbackCommentHide(hidden=True))
    assert shown.hidden and "apply" not in world.comments.calls, "already hidden: nothing written"
    unhidden = await world.service.hide_comment(ADMIN, row.id, mine.id, FeedbackCommentHide(hidden=False))
    assert not unhidden.hidden and world.comments.rows[mine.id].hidden_by is None
    other_view = await world.service.comments(other, row.id)
    assert other_view[0].body == f"Same here {REMOVED_URL}", "others read it through public_text"


async def test_who_may_comment(world: World) -> None:
    row = await world.file(CUSTOMER)
    payload = FeedbackCommentCreate(body="x")
    with pytest.raises(FeedbackNotFoundError):
        await world.service.comment(CUSTOMER, row.id, payload)  # KS: the author outside the predicate
    with pytest.raises(FeedbackNotFoundError):
        await world.service.comments(CUSTOMER, row.id)
    assert (await world.service.vote(CUSTOMER, row.id)).voted, "a vote is the author's own"
    with pytest.raises(DemoError):
        await world.service.comment(DEMO, row.id, payload)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.comments(DEMO, row.id)
    assert await world.service.comment(ADMIN, row.id, payload)
    row.public = False
    with pytest.raises(FeedbackNotFoundError):  # switched off: its commenters lose the thread
        await world.service.comments(MEMBER, row.id)
    assert len(await world.service.comments(ADMIN, row.id)) == 1
    crash = await world.service.report_crash(AUTHOR, CrashReportCreate(message="boom"))
    with pytest.raises(FeedbackNotFoundError):
        await world.service.comment(ADMIN, crash.feedback_id or 0, payload)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.comment(MEMBER, 999, payload)


async def test_the_comment_window_is_charged_before_the_work(world: World) -> None:
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=3600)
    row = await world.file()
    await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x"), limiter=limiter)
    world.clear()
    with pytest.raises(FeedbackThrottledError, match=COMMENTS_THROTTLED_DETAIL) as refused:
        await world.service.comment(MEMBER, 999, FeedbackCommentCreate(body="x"), limiter=limiter)
    assert refused.value.status_code == 429 and refused.value.headers["Retry-After"]
    assert world.comments.calls == [] and world.repository.calls == []


async def test_hide_and_delete_are_the_admins_and_the_writers(world: World) -> None:
    row = await world.file()
    comment = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x"))
    hide = FeedbackCommentHide(hidden=True)
    with pytest.raises(FeedbackForbiddenError, match="Insufficient role"):
        await world.service.hide_comment(AUTHOR, row.id, comment.id, hide)
    with pytest.raises(DemoError):
        await world.service.hide_comment(dataclasses.replace(ADMIN, is_demo=True), row.id, comment.id, hide)
    with pytest.raises(FeedbackNotFoundError, match="Comment not found"):
        await world.service.hide_comment(ADMIN, row.id + 1, comment.id, hide)
    for someone in (ADMIN, AUTHOR):
        with pytest.raises(FeedbackNotFoundError, match="Comment not found"):
            await world.service.delete_comment(someone, row.id, comment.id)
    with pytest.raises(DemoError):
        await world.service.delete_comment(DEMO, row.id, comment.id)
    await world.service.delete_comment(MEMBER, row.id, comment.id)
    assert world.comments.rows == {}
    with pytest.raises(FeedbackNotFoundError):
        await world.service.delete_comment(MEMBER, row.id, comment.id)
    orphan = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="y"))
    world.comments.rows[orphan.id].user_id = None  # erased
    with pytest.raises(FeedbackNotFoundError):
        await world.service.delete_comment(MEMBER, row.id, orphan.id)


# --- votes ------------------------------------------------------------------------------------


async def test_votes_are_idempotent_and_the_author_cannot_take_theirs_back(world: World) -> None:
    row = await world.file()
    assert (await world.service.vote(MEMBER, row.id)).model_dump() == {"votes": 2, "voted": True}
    assert (await world.service.vote(MEMBER, row.id)).model_dump() == {"votes": 2, "voted": True}
    assert (await world.service.unvote(MEMBER, row.id)).model_dump() == {"votes": 1, "voted": False}
    assert (await world.service.unvote(MEMBER, row.id)).model_dump() == {"votes": 1, "voted": False}
    assert (await world.service.vote(AUTHOR, row.id)).model_dump() == {"votes": 1, "voted": True}
    with pytest.raises(FeedbackConflictError, match="Your report counts as your vote") as conflict:
        await world.service.unvote(AUTHOR, row.id)
    assert conflict.value.status_code == 409
    row.status = FeedbackStatus.WONT_DO
    assert (await world.service.vote(ADMIN, row.id)).votes == 2, "any status"
    with pytest.raises(DemoError):
        await world.service.vote(DEMO, row.id)
    with pytest.raises(DemoError):
        await world.service.unvote(DEMO, row.id)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.vote(CUSTOMER, row.id)
    world.votes.rows.clear()
    assert (await world.service.unvote(MEMBER, row.id)).model_dump() == {"votes": 0, "voted": False}


async def test_the_vote_window_is_charged_before_the_work(world: World) -> None:
    limiter = SlidingWindowRateLimiter(max_hits=1, window_seconds=3600)
    row = await world.file()
    await world.service.vote(MEMBER, row.id, limiter=limiter)
    for flow in (world.service.vote, world.service.unvote):
        with pytest.raises(FeedbackThrottledError, match=VOTES_THROTTLED_DETAIL):
            await flow(MEMBER, 999, limiter=limiter)


# --- replies ----------------------------------------------------------------------------------


class Journaled(InMemoryReplyStore):
    """Logs its ``add`` into the repository's own call list, so the order across the two shows."""

    log: list[str]

    async def add(self, new: NewFeedbackReply, *, actor: FeedbackActor | None) -> MemoryReplyRow:
        self.log.append("reply")
        return await super().add(new, actor=actor)


async def test_a_reply_is_written_then_the_report() -> None:
    """One write per store, the report last (§9.14)."""
    replies = Journaled()
    world = World(replies=replies)
    replies.log = world.repository.calls
    row = await world.file()
    world.repository.calls.clear()
    answered = await world.service.reply(
        ADMIN, row.id, FeedbackReplyCreate(body="Fixed in 0.4.27.", status=FeedbackStatus.DONE)
    )
    assert world.repository.calls == ["get", "reply", "apply"]
    assert answered.status is FeedbackStatus.DONE and answered.resolved_at is not None
    [(stored, by)] = replies.added
    assert (stored.status_from, stored.status_to, stored.author_id, by) == (
        FeedbackStatus.OPEN,
        FeedbackStatus.DONE,
        ADMIN.id,
        ADMIN,
    )
    world.repository.calls.clear()
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Noted."))
    assert world.repository.calls == ["get", "reply"], "no status: the report is not written"


async def test_who_may_reply(world: World) -> None:
    row = await world.file()
    payload = FeedbackReplyCreate(body="x")
    with pytest.raises(FeedbackForbiddenError, match="Insufficient role"):
        await world.service.reply(AUTHOR, row.id, payload)
    with pytest.raises(DemoError):
        await world.service.reply(dataclasses.replace(ADMIN, is_demo=True), row.id, payload)
    with pytest.raises(FeedbackNotFoundError):
        await world.service.reply(ADMIN, 999, payload)


# --- the views: one call per store for a page ---------------------------------------------------


async def test_the_admin_view_fills_a_page_with_one_call_per_store(world: World) -> None:
    rows = [await world.file(), await world.file(CUSTOMER), await world.file(MEMBER)]
    await world.service.reply(ADMIN, rows[0].id, FeedbackReplyCreate(body=f"See {SHOT}", status=FeedbackStatus.READY))
    comment = await world.service.comment(MEMBER, rows[0].id, FeedbackCommentCreate(body="x"))
    await world.service.hide_comment(ADMIN, rows[0].id, comment.id, FeedbackCommentHide(hidden=True))
    rows[2].user_id = None  # erased
    world.clear()

    pairs = list(await world.service.inbox(ADMIN))
    answers = await world.service.admin_view(ADMIN, pairs)
    assert _calls(world) == {
        "replies": ["for_reports"],
        "comments": ["counts"],
        "votes": ["counts"],
        "people": ["names"],
    }
    by_id = {answer.id: answer for answer in answers}
    first = by_id[rows[0].id]
    assert (first.author_name, first.user_email, first.mine, first.votes, first.comment_count) == (
        "Ada Author",
        AUTHOR.email,
        False,
        1,
        1,
    )
    assert first.replies is not None
    [reply] = first.replies
    assert (reply.author_name, reply.body, reply.status_from) == ("Ann Admin", f"See {SHOT}", FeedbackStatus.OPEN)
    assert by_id[rows[1].id].author_name == "Cleo Customer", "the admin sees the whole name"
    assert by_id[rows[2].id].author_name is None and by_id[rows[2].id].replies == []


async def test_the_author_view_shows_replies_as_others_do(world: World) -> None:
    row = await world.file()
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body=f"See {SHOT}", status=FeedbackStatus.READY))
    comment = await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="x"))
    await world.service.hide_comment(ADMIN, row.id, comment.id, FeedbackCommentHide(hidden=True))
    [answer] = await world.service.author_view(AUTHOR, [row])
    assert (answer.author_name, answer.mine, answer.voted, answer.comment_count) == ("Ada Author", True, True, 0)
    assert answer.replies is not None
    [reply] = answer.replies
    assert (reply.author_name, reply.body, reply.status_from) == ("Ann", f"See {REMOVED_URL}", None)
    world.clear()
    assert await world.service.author_view(AUTHOR, []) == []
    assert all(calls == [] for calls in _calls(world).values()), "an empty page asks nobody"


async def test_without_public_parts_the_views_are_010s(world: World) -> None:
    plain = World(public_viewer=None, replies=None, comments=None, votes=None, people=None)
    row = await plain.file()
    [answer] = await plain.service.admin_view(ADMIN, [(row, "a@example.test")])
    assert answer == FeedbackResponse.model_validate(row).model_copy(update={"user_email": "a@example.test"})
    # kastlan: replies only.
    replies_only = World(public_viewer=None, comments=None, votes=None, people=None)
    row = await replies_only.file()
    await replies_only.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="ok"))
    [answer] = await replies_only.service.author_view(
        AUTHOR, [row], to_response=lambda r, a: FeedbackResponse.model_validate(r)
    )
    assert answer.replies is not None and answer.replies[0].author_name is None
    assert (answer.author_name, answer.mine, answer.votes, answer.comment_count) == (None, None, None, None)


async def test_outcome_answers_as_the_newest_reply(world: World) -> None:
    # §9.7, kastlan's 0.38 finding 2: a client from before 0.38 reads `outcome`.
    row = await world.file()
    [before] = await world.service.admin_view(ADMIN, [(row, None)])
    assert before.outcome == row.outcome
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="First"))
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body=f"Then {SHOT}"))
    [admin] = await world.service.admin_view(ADMIN, [(row, None)])
    [author] = await world.service.author_view(AUTHOR, [row])
    assert admin.outcome == f"Then {SHOT}"
    assert author.outcome == f"Then {REMOVED_URL}"


# --- update ------------------------------------------------------------------------------------


async def test_the_public_switch_goes_through_the_rules(world: World) -> None:
    row = await world.file()
    assert (await world.service.update(AUTHOR, row.id, FeedbackUpdate(public=False))).public is False
    row.status = FeedbackStatus.DONE
    with pytest.raises(FeedbackForbiddenError, match="Entry is in DONE"):
        await world.service.update(AUTHOR, row.id, FeedbackUpdate(public=True))
    row.filed_private = True
    assert (await world.service.update(AUTHOR, row.id, FeedbackUpdate(public=True))).public is True
    kastlan = World(public_viewer=None)
    other = await kastlan.file()
    with pytest.raises(FeedbackValidationError, match="public is not offered here"):
        await kastlan.service.update(ADMIN, other.id, FeedbackUpdate(public=False))
