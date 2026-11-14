"""The ``sql`` extra's feedback half: the report, entry, comment, vote and upload tables as
mixins and the feedback ports' stores over them, on SQLite in memory through aiosqlite with
foreign keys on, so a cascade or a ``SET NULL`` is the database's and not the test's. The
contract suites run over both schemas of ``tests/sql_tables.py``; what they cannot see (the
claims, the notices, the files predicate, the prune, the service on top) is tested here."""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import ColumnElement, Table, event, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from eifi1_server_kit.feedback import (
    CrashReportCreate,
    FeedbackActor,
    FeedbackCategory,
    FeedbackCommentCreate,
    FeedbackCommentHide,
    FeedbackCreate,
    FeedbackEntryKind,
    FeedbackReplyCreate,
    FeedbackService,
    FeedbackStatus,
    FeedbackUpdate,
    NewFeedbackComment,
    NewFeedbackReply,
    UnknownAttachmentUrlError,
    attachment_line,
    sql,
)
from eifi1_server_kit.feedback.sql import (
    FeedbackAttachmentColumns,
    SqlAttachmentStorage,
    SqlFeedbackCommentStore,
    SqlFeedbackReplyStore,
    SqlFeedbackRepository,
    SqlFeedbackVoteStore,
    SqlPeopleLookup,
    UploadPrune,
)
from eifi1_server_kit.feedback.testing import assert_public_view
from eifi1_server_kit.objects import LocalObjectStore
from eifi1_server_kit.uploads import CheckedUpload, check_upload
from tests.sql_tables import (
    POLICY,
    Account,
    Base,
    Feedback,
    FeedbackAttachment,
    FeedbackComment,
    FeedbackReply,
    FeedbackVote,
    OtherBase,
    Report,
    ReportComment,
    ReportEntry,
    ReportUpload,
    ReportVote,
    User,
    actor_of,
    person,
    report,
    run_feedback_suites,
    sees_everyone,
    upload,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _table(model: type[Any]) -> Table:
    return cast(Table, model.__table__)


def _ddl(model: type[Any]) -> str:
    """The CREATE TABLE PostgreSQL would run (SQLAlchemy's compile is untyped)."""
    return str(CreateTable(_table(model)).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def _keys(model: type[Any]) -> dict[str, tuple[str, str | None]]:
    """Each foreign key's column → (the column it points at, its ``ON DELETE``)."""
    return {key.parent.name: (key.target_fullname, key.ondelete) for key in _table(model).foreign_keys}


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def on(connection: Any, record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(OtherBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def files(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


class World:
    """Three people (an author, another, an admin) and every store, on one session."""

    def __init__(self, session: AsyncSession, objects: LocalObjectStore) -> None:
        self.session = session
        self.objects = objects
        self.notified: list[tuple[str, int, int | None]] = []

    async def build(self) -> World:
        session = self.session
        self.author = actor_of(await person(session, User, "author@example.org"))
        self.other = actor_of(await person(session, User, "other@example.org", first="Grace", last="Hopper"))
        self.admin = actor_of(await person(session, User, "admin@example.org", last="Admin"), admin=True)
        self.storage = SqlAttachmentStorage(
            session,
            FeedbackAttachment,
            objects=self.objects,
            reports=Feedback,
            replies=FeedbackReply,
            comments=FeedbackComment,
            policy=POLICY,
            public_viewer=sees_everyone,
        )
        self.replies = SqlFeedbackReplyStore(
            session, FeedbackReply, notify=self.answered, uploads=FeedbackAttachment, policy=POLICY
        )
        self.comments = SqlFeedbackCommentStore(
            session, FeedbackComment, notify=self.commented, uploads=FeedbackAttachment, policy=POLICY
        )
        self.votes = SqlFeedbackVoteStore(session, FeedbackVote)
        self.people = SqlPeopleLookup(session, User)
        self.repository = SqlFeedbackRepository(
            session,
            Feedback,
            accounts=User,
            policy=POLICY,
            replies=FeedbackReply,
            comments=FeedbackComment,
            votes=FeedbackVote,
            uploads=FeedbackAttachment,
        )
        return self

    async def answered(self, reply: FeedbackReply, actor: FeedbackActor | None) -> None:
        self.notified.append(("answered", reply.id, None if actor is None else actor.id))

    async def commented(self, comment: FeedbackComment, actor: FeedbackActor) -> None:
        self.notified.append(("commented", comment.id, actor.id))

    def service(self) -> FeedbackService[Any]:
        # `[Any]`: mypy checks a type argument against the port's protocol before it has read
        # the mapped columns' types, and refuses a model it accepts everywhere else.
        return FeedbackService(
            self.repository,
            policy=POLICY,
            replies=self.replies,
            comments=self.comments,
            votes=self.votes,
            people=self.people,
            public_viewer=sees_everyone,
        )


@pytest.fixture
async def world(session: AsyncSession, files: LocalObjectStore) -> World:
    return await World(session, files).build()


# ── The import ──────────────────────────────────────────────────────────────


def test_the_module_names_its_extra_without_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import sqlalchemy`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.feedback.sql", raising=False)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(
        ImportError,
        match=r"feedback\.sql needs SQLAlchemy: install the kit with its sql extra, eifi1-server-kit\[sql\]",
    ):
        importlib.import_module("eifi1_server_kit.feedback.sql")


# ── The tables ──────────────────────────────────────────────────────────────


def test_on_postgresql_the_report_stores_jsonb_and_instants_with_their_zone() -> None:
    ddl = _ddl(Feedback)
    assert "context JSONB" in ddl and "attachment_urls JSONB" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "resolved_at TIMESTAMP WITH TIME ZONE" in ddl
    assert "title VARCHAR(255) NOT NULL" in ddl and "screenshot_url VARCHAR(500)" in ddl
    assert "status VARCHAR(20) NOT NULL" in ddl and "category VARCHAR(16) NOT NULL" in ddl


def test_every_foreign_key_points_at_the_accounts_and_the_reports_and_lets_a_person_go() -> None:
    assert _keys(Feedback) == {"user_id": ("users.id", "SET NULL")}
    assert _keys(FeedbackReply) == {
        "feedback_id": ("feedback.id", "CASCADE"),
        "author_id": ("users.id", "SET NULL"),
    }
    assert _keys(FeedbackComment) == {
        "feedback_id": ("feedback.id", "CASCADE"),
        "user_id": ("users.id", "SET NULL"),
        "hidden_by": ("users.id", "SET NULL"),
    }
    # A vote goes with its account, and is the one row per person per report.
    assert _keys(FeedbackVote) == {"feedback_id": ("feedback.id", "CASCADE"), "user_id": ("users.id", "CASCADE")}
    assert [column.name for column in _table(FeedbackVote).primary_key] == ["feedback_id", "user_id"]
    assert _keys(FeedbackAttachment) == {"user_id": ("users.id", "SET NULL")}
    assert "UNIQUE (key, user_id)" in _ddl(FeedbackAttachment)


def test_renamed_tables_move_every_foreign_key_and_keep_the_app_s_own_table_args() -> None:
    assert _keys(Report) == {"user_id": ("accounts.id", "SET NULL")}
    assert _keys(ReportEntry)["feedback_id"] == ("reports.id", "CASCADE")
    assert _keys(ReportComment)["hidden_by"] == ("accounts.id", "SET NULL")
    assert _keys(ReportVote)["user_id"] == ("accounts.id", "CASCADE")
    assert {index.name for index in _table(ReportUpload).indexes} >= {"ix_report_uploads_media"}
    assert "UNIQUE (key, user_id)" in _ddl(ReportUpload)
    assert "team VARCHAR(40)" in _ddl(Report)


def test_an_entry_written_by_hand_is_a_reply_stamped_by_the_database() -> None:
    """A loop's raw ``INSERT`` (kastlan's) names neither ``kind`` nor ``created_at``."""
    ddl = _ddl(FeedbackReply)
    assert "kind VARCHAR(12) DEFAULT 'reply' NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


async def test_a_raw_insert_of_an_entry_reads_back_as_a_reply_with_its_stamp(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    await world.session.execute(
        text("INSERT INTO feedback_replies (feedback_id, body) VALUES (:id, 'From the loop')"), {"id": row.id}
    )
    (entry,) = (await world.replies.for_reports([row.id]))[row.id]
    assert entry.kind == FeedbackEntryKind.REPLY.value
    assert entry.created_at.tzinfo is not None


async def test_no_files_and_no_context_are_sql_null_not_json_null(world: World) -> None:
    row = await world.repository.add(report(world.author.id, context=None))
    found = await world.session.scalar(
        select(func.count())
        .select_from(Feedback)
        .where(Feedback.id == row.id, Feedback.attachment_urls.is_(None), Feedback.context.is_(None))
    )
    assert found == 1


# ── The contract suites ─────────────────────────────────────────────────────


async def test_every_contract_suite_passes_on_the_common_schema_twice(session: AsyncSession, tmp_path: Path) -> None:
    await run_feedback_suites(session, tmp_path / "objects")
    # Again on the same database: each suite asserts only on what it adds.
    await run_feedback_suites(session, tmp_path / "objects")


async def test_every_contract_suite_passes_on_the_renamed_schema(session: AsyncSession, tmp_path: Path) -> None:
    await run_feedback_suites(session, tmp_path / "objects", renamed=True)


# ── The repository ──────────────────────────────────────────────────────────


async def test_the_repository_writes_an_enum_as_its_value_and_no_files_as_none(world: World) -> None:
    row = await world.repository.add(report(world.author.id, attachment_urls=[]))
    assert row.status == "OPEN" and type(row.status) is str
    assert row.category == "BUG" and row.attachment_urls is None
    await world.repository.apply(row, {"status": FeedbackStatus.DONE, "resolved_at": NOW}, actor=world.admin)
    assert row.status == "DONE" and type(row.status) is str and row.resolved_at == NOW


async def test_apply_moves_updated_at_and_refuses_a_column_the_model_does_not_map(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    filed = row.updated_at
    await world.repository.apply(row, {"title": "Retitled"}, actor=world.author)
    assert row.updated_at > filed
    with pytest.raises(ValueError, match="Feedback maps no company_id"):
        await world.repository.apply(row, {"company_id": 3}, actor=world.author)


async def test_get_answers_what_a_subclass_lets_the_viewer_see(world: World) -> None:
    """Kurvenschmiede's ``visible_to``: an admin sees every report, anybody else their own."""

    class Visible(SqlFeedbackRepository[Feedback]):
        def visible_to(self, viewer: FeedbackActor) -> ColumnElement[bool] | None:
            return None if viewer.is_admin else Feedback.user_id == viewer.id

    repository = Visible(world.session, Feedback, accounts=User)
    row = await repository.add(report(world.author.id))
    assert await repository.get(row.id, viewer=world.author) is row
    assert await repository.get(row.id, viewer=world.admin) is row
    assert await repository.get(row.id, viewer=world.other) is None
    assert await world.repository.get(row.id, viewer=world.other) is row


async def test_count_by_status_counts_every_report_where_the_accounts_have_no_demo(
    session: AsyncSession,
) -> None:
    repository = SqlFeedbackRepository(session, Report, accounts=Account)
    author = await person(session, Account, "a@example.org")
    await repository.add(report(author.id))
    erased = await repository.add(report(author.id, status=FeedbackStatus.READY))
    await repository.apply(erased, {"user_id": None}, actor=None)
    assert await repository.count_by_status() == {FeedbackStatus.OPEN: 1, FeedbackStatus.READY: 1}


async def test_a_report_names_only_its_author_s_own_uploads(world: World) -> None:
    theirs = await upload(world.storage, world.other, "theirs")
    mine = await upload(world.storage, world.author, "mine")
    with pytest.raises(UnknownAttachmentUrlError, match=r"is not a file you uploaded"):
        await world.repository.add(report(world.author.id, screenshot_url=theirs))
    with pytest.raises(UnknownAttachmentUrlError, match=r"is not a file you uploaded"):
        await world.repository.add(report(world.author.id, attachment_urls=[mine, theirs]))
    with pytest.raises(UnknownAttachmentUrlError, match=r"is not a file you uploaded"):
        await world.repository.add(report(world.author.id, attachment_urls=["/elsewhere/abcdefabcdef.png"]))
    row = await world.repository.add(report(world.author.id, screenshot_url=mine, attachment_urls=[mine]))
    assert row.screenshot_url == mine and row.attachment_urls == [mine]


async def test_a_repository_without_uploads_claims_nothing(world: World) -> None:
    repository = SqlFeedbackRepository(world.session, Feedback, accounts=User, policy=POLICY)
    row = await repository.add(report(world.author.id, screenshot_url=POLICY.url_for("abcdefabcdef.png")))
    assert row.screenshot_url == POLICY.url_for("abcdefabcdef.png")


async def test_a_repository_without_the_stores_tables_names_only_the_reports_files(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    url = await upload(world.storage, world.other, "comment")
    await world.comments.add(NewFeedbackComment(row.id, world.other.id, "See", attachment_url=url), actor=world.other)
    assert url in await world.repository.urls_named_except(())
    bare = SqlFeedbackRepository(world.session, Feedback, accounts=User, policy=POLICY)
    assert url not in await bare.urls_named_except([row.id])


async def test_the_public_list_without_vote_or_comment_tables_counts_nothing(world: World) -> None:
    repository = SqlFeedbackRepository(world.session, Feedback, accounts=User)
    row = await repository.add(report(world.author.id, "Bare 100%_ title"))
    await world.votes.put(row.id, world.other.id)
    # The search is literal: `%` and `_` are characters here, not wildcards.
    (entry,) = await repository.public_page(
        viewer_id=world.other.id, sort="votes", status=None, category=None, q="100%_", limit=10, offset=0
    )
    assert (entry.row, entry.votes, entry.voted, entry.comment_count) == (row, 0, False, 0)
    assert not await repository.public_page(
        viewer_id=world.other.id, sort="newest", status=None, category=None, q="100%x", limit=10, offset=0
    )


async def test_the_public_list_sorts_by_the_last_change(world: World) -> None:
    first = await world.repository.add(report(world.author.id, "First"))
    second = await world.repository.add(report(world.author.id, "Second"))
    await world.repository.apply(first, {"title": "First, changed"}, actor=world.author)
    found = await world.repository.public_page(
        viewer_id=world.other.id, sort="updated", status=None, category=None, q=None, limit=10, offset=0
    )
    assert [entry.row.id for entry in found] == [first.id, second.id]


# ── The entries, the comments and the notices ──────────────────────────────


async def test_a_reply_tells_the_app_and_a_rework_tells_nobody(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    reply = await world.replies.add(NewFeedbackReply(row.id, world.admin.id, "Answered"), actor=world.admin)
    loop = await world.replies.add(NewFeedbackReply(row.id, None, "The loop's"), actor=None)
    await world.replies.add(
        NewFeedbackReply(row.id, world.author.id, "Still broken", kind=FeedbackEntryKind.REWORK), actor=world.author
    )
    assert world.notified == [("answered", reply.id, world.admin.id), ("answered", loop.id, None)]


async def test_an_entry_or_a_comment_names_only_its_writer_s_own_upload(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    theirs = await upload(world.storage, world.other, "theirs")
    with pytest.raises(UnknownAttachmentUrlError):
        await world.replies.add(
            NewFeedbackReply(row.id, world.author.id, "Rework", kind=FeedbackEntryKind.REWORK, attachment_url=theirs),
            actor=world.author,
        )
    with pytest.raises(UnknownAttachmentUrlError):
        await world.comments.add(
            NewFeedbackComment(row.id, world.author.id, "Look", attachment_url=theirs), actor=world.author
        )
    # A writer the entry does not name (a loop's, a migration's) is the server's own word.
    loop = await world.replies.add(NewFeedbackReply(row.id, None, "Migrated", attachment_url=theirs), actor=None)
    assert loop.attachment_url == theirs
    assert [kind for kind, *_ in world.notified] == ["answered"]


async def test_a_comment_tells_the_app_and_its_counts_are_the_report_s(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    comment = await world.comments.add(NewFeedbackComment(row.id, world.other.id, "Me too"), actor=world.other)
    assert world.notified == [("commented", comment.id, world.other.id)]
    assert await world.comments.counts([], include_hidden=True) == {}
    assert await world.replies.for_reports([]) == {}
    assert await world.votes.counts([], viewer_id=world.other.id) == {}
    assert await world.people.names([]) == {}


async def test_commenters_leave_out_an_erased_writer_and_name_each_once(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    for writer in (world.other, world.author, world.other):
        await world.comments.add(NewFeedbackComment(row.id, writer.id, "Here"), actor=writer)
    gone = await world.comments.add(NewFeedbackComment(row.id, world.admin.id, "Gone"), actor=world.admin)
    await world.comments.apply(gone, {"user_id": None}, actor=None)
    assert list(await world.comments.commenters(row.id)) == [world.other.id, world.author.id]


async def test_a_vote_and_a_comment_go_with_their_report(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    await world.votes.put(row.id, world.other.id)
    await world.comments.add(NewFeedbackComment(row.id, world.other.id, "Me too"), actor=world.other)
    await world.replies.add(NewFeedbackReply(row.id, world.admin.id, "Answered"), actor=world.admin)
    await world.session.delete(row)
    await world.session.flush()
    for model in (FeedbackVote, FeedbackComment, FeedbackReply):
        assert await world.session.scalar(select(func.count()).select_from(model)) == 0


async def test_the_people_lookup_leaves_out_a_demo_and_reads_every_account_where_there_is_none(
    world: World, session: AsyncSession
) -> None:
    demo = await person(session, User, "demo@example.org", first="Demo")
    demo.is_demo = True
    await session.flush()
    assert set(await world.people.names([world.author.id, demo.id])) == {world.author.id}
    plain = await person(session, Account, "plain@example.org", first="Plain", last="")
    assert await SqlPeopleLookup(session, Account).names([plain.id]) == {plain.id: ("Plain", "")}


def test_a_vote_is_one_statement_that_ignores_a_conflict_on_postgresql() -> None:
    statement = sql._insert(
        FeedbackVote, {"feedback_id": 1, "user_id": 2}, "postgresql", conflict=("feedback_id", "user_id")
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "ON CONFLICT (feedback_id, user_id) DO NOTHING" in compiled
    touched = sql._insert(
        FeedbackAttachment,
        {"key": "abcdefabcdef.png", "user_id": 2, "media_type": "image/png"},
        "postgresql",
        conflict=("key", "user_id"),
        refresh=("created_at",),
    )
    compiled = str(touched.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "ON CONFLICT (key, user_id) DO UPDATE SET created_at = excluded.created_at" in compiled
    with pytest.raises(NotImplementedError, match="PostgreSQL and SQLite, not mysql"):
        sql._insert(FeedbackVote, {"feedback_id": 1, "user_id": 2}, "mysql", conflict=("feedback_id", "user_id"))


# ── The files ───────────────────────────────────────────────────────────────


async def _file(world: World, uploader: FeedbackActor, note: str) -> tuple[str, str]:
    url = await upload(world.storage, uploader, note)
    key = POLICY.key_of(url)
    assert key is not None
    return url, key


async def test_a_file_is_read_by_its_uploader_and_an_admin_and_nobody_else(world: World) -> None:
    _, key = await _file(world, world.author, "screen")
    found = await world.storage.read(key, viewer=world.author)
    assert found is not None and found.media_type == "text/plain" and found.filename == "screen.txt"
    assert await world.storage.read(key, viewer=world.admin) is not None
    assert await world.storage.read(key, viewer=world.other) is None
    assert await world.storage.read("not a key", viewer=world.admin) is None


async def test_the_report_s_author_reads_the_file_an_admin_replied_with(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    url, key = await _file(world, world.admin, "answer")
    await world.replies.add(NewFeedbackReply(row.id, world.admin.id, "See", attachment_url=url), actor=world.admin)
    assert await world.storage.read(key, viewer=world.author) is not None
    assert await world.storage.read(key, viewer=world.other) is None


async def test_a_visible_comment_s_file_is_read_by_whoever_reads_the_comment(world: World) -> None:
    row = await world.repository.add(report(world.author.id))
    url, key = await _file(world, world.other, "comment")
    comment = await world.comments.add(
        NewFeedbackComment(row.id, world.other.id, "Look", attachment_url=url), actor=world.other
    )
    third = actor_of(await person(world.session, User, "third@example.org"))
    assert await world.storage.read(key, viewer=third) is not None
    # Never for a demo, and not where the app has no public parts.
    assert await world.storage.read(key, viewer=FeedbackActor(third.id, third.email, None, False, is_demo=True)) is None
    private = SqlAttachmentStorage(
        world.session,
        FeedbackAttachment,
        objects=world.objects,
        reports=Feedback,
        comments=FeedbackComment,
        policy=POLICY,
    )
    assert await private.read(key, viewer=third) is None
    # Hidden, it is back to its writer and an admin.
    await world.comments.apply(comment, {"hidden_at": NOW, "hidden_by": world.admin.id}, actor=world.admin)
    assert await world.storage.read(key, viewer=third) is None
    assert await world.storage.read(key, viewer=world.other) is not None


async def test_a_comment_s_file_on_a_private_report_is_its_author_s_and_on_a_crash_nobody_s(world: World) -> None:
    private = await world.repository.add(report(world.author.id, public=False))
    crash = await world.repository.add(report(world.author.id, category=FeedbackCategory.CRASH, public=False))
    for row, note in ((private, "private"), (crash, "crash")):
        url, key = await _file(world, world.other, note)
        await world.comments.add(
            NewFeedbackComment(row.id, world.other.id, "Look", attachment_url=url), actor=world.other
        )
        third = actor_of(await person(world.session, User, f"{note}@example.org"))
        assert await world.storage.read(key, viewer=third) is None
        readable = await world.storage.read(key, viewer=world.author)
        assert (readable is not None) is (row is private)


async def test_a_file_whose_object_is_gone_reads_as_none(world: World) -> None:
    _, key = await _file(world, world.author, "gone")
    await world.objects.remove(world.storage.object_key(key))
    assert await world.storage.read(key, viewer=world.author) is None


async def test_the_same_file_twice_is_one_row_stamped_again(world: World) -> None:
    """Uploaded again, a file starts its wait for a report anew: the prune that would have
    taken the first upload leaves the second."""
    data = b"the same bytes\n"
    checked = check_upload(data, "text/plain")
    key = await world.storage.save(data, checked, uploader=world.author, filename="first.txt")
    long_ago = datetime(2001, 2, 3, tzinfo=UTC)
    await _aged(world, key, long_ago)
    assert await world.storage.save(data, checked, uploader=world.author, filename=None) == key
    rows = list(await world.session.scalars(select(FeedbackAttachment).execution_options(populate_existing=True)))
    assert len(rows) == 1 and rows[0].filename == "first.txt"
    assert rows[0].created_at > long_ago
    # Another person's upload of the same bytes is a row of their own.
    assert await world.storage.save(data, checked, uploader=world.other, filename=None) == key
    assert await world.session.scalar(select(func.count()).select_from(FeedbackAttachment)) == 2


async def test_a_long_name_is_cut_to_the_column_and_a_minted_key_outside_the_policy_is_refused(world: World) -> None:
    data = b"named\n"
    key = await world.storage.save(data, check_upload(data, "text/plain"), uploader=world.author, filename="n" * 400)
    found = await world.storage.read(key, viewer=world.author)
    assert found is not None and found.filename == "n" * 255
    odd = SqlAttachmentStorage(
        world.session,
        FeedbackAttachment,
        objects=world.objects,
        reports=Feedback,
        policy=POLICY,
        mint_key=lambda data, extension: f"odd.{extension}",
    )
    with pytest.raises(ValueError, match=r"'odd\.txt', which the policy refuses"):
        await odd.save(
            data, CheckedUpload("text/plain", "txt", "attachment", len(data)), uploader=world.author, filename=None
        )


async def test_the_upload_rule_holds_without_the_store(world: World) -> None:
    """One row per person per file, whoever writes it."""
    world.session.add(FeedbackAttachment(key="abcdefabcdef.png", user_id=world.author.id, media_type="image/png"))
    world.session.add(FeedbackAttachment(key="abcdefabcdef.png", user_id=world.author.id, media_type="image/png"))
    with pytest.raises(IntegrityError):
        await world.session.flush()


# ── Pruning the uploads ─────────────────────────────────────────────────────


async def _aged(world: World, key: str, at: datetime) -> None:
    await world.session.execute(update(FeedbackAttachment).where(FeedbackAttachment.key == key).values(created_at=at))


async def test_prune_deletes_the_uploads_nothing_names_and_answers_the_keys_no_row_holds(world: World) -> None:
    old = NOW - timedelta(days=3)
    _, unfiled = await _file(world, world.author, "unfiled")
    filed_url, filed = await _file(world, world.author, "filed")
    entry_url, in_entry = await _file(world, world.admin, "entry")
    comment_url, in_comment = await _file(world, world.other, "comment")
    body_url, in_body = await _file(world, world.author, "body")
    _, recent = await _file(world, world.author, "recent")
    row = await world.repository.add(
        report(world.author.id, attachment_urls=[filed_url], body=f"See\n{attachment_line(body_url)}")
    )
    await world.replies.add(
        NewFeedbackReply(row.id, world.admin.id, "See", attachment_url=entry_url), actor=world.admin
    )
    await world.comments.add(
        NewFeedbackComment(row.id, world.other.id, "Me", attachment_url=comment_url), actor=world.other
    )
    for key in (unfiled, filed, in_entry, in_comment, in_body):
        await _aged(world, key, old)
    # The same bytes as the unfiled upload, by somebody else and just now: the object stays.
    shared_data = await world.objects.read(world.storage.object_key(unfiled))
    await world.storage.save(shared_data, check_upload(shared_data, "text/plain"), uploader=world.other, filename=None)
    _, lonely = await _file(world, world.other, "lonely")
    await _aged(world, lonely, old)

    pruned = await world.storage.prune(before=NOW - timedelta(days=1), now=NOW)
    assert pruned == UploadPrune(uploads=2, keys=(lonely,))
    left = set(await world.session.scalars(select(FeedbackAttachment.key)))
    assert left == {filed, in_entry, in_comment, in_body, recent, unfiled}
    assert recent in left and await world.storage.prune(before=NOW - timedelta(days=1), now=NOW) == UploadPrune()


async def test_prune_refuses_a_horizon_that_has_not_passed_and_finds_nothing_young(world: World) -> None:
    with pytest.raises(ValueError, match="has not passed yet"):
        await world.storage.prune(before=NOW + timedelta(days=1), now=NOW)
    await _file(world, world.author, "young")
    assert await world.storage.prune(before=datetime(2001, 2, 3, tzinfo=UTC), now=NOW) == UploadPrune()


# ── The service on top ──────────────────────────────────────────────────────


async def test_the_service_runs_its_flows_on_the_sql_stores(world: World) -> None:
    """The kit's own flows over the SQL stores: every answer is read off a mapped row, so a
    column the ORM left unloaded would fail here as the async session's lazy load."""
    service = world.service()
    screen = await upload(world.storage, world.author, "screen")
    row = await service.create(world.author, FeedbackCreate(title="Broken", body="It broke.", screenshot_url=screen))
    await service.reply(world.admin, row.id, FeedbackReplyCreate(body="Fixed", status=FeedbackStatus.DONE))
    assert row.status == "DONE" and row.resolved_at is not None
    comment = await service.comment(world.other, row.id, FeedbackCommentCreate(body="Thanks"))
    await service.hide_comment(world.admin, row.id, comment.id, FeedbackCommentHide(hidden=True))
    await service.vote(world.other, row.id)

    (admin_view,) = await service.admin_view(world.admin, await service.inbox(world.admin))
    assert admin_view.user_email == world.author.email and admin_view.author_name == "Ada Lovelace"
    assert (admin_view.votes, admin_view.comment_count, admin_view.outcome) == (2, 1, "Fixed")
    (mine,) = await service.author_view(world.author, await service.list_mine(world.author))
    assert mine.comment_count == 0 and mine.mine
    (item,) = await service.public_item(world.other, await service.list_public(world.other))
    detail = await service.public_view(world.other, await service.get_public(world.other, row.id))
    assert_public_view(item, policy=POLICY)
    assert_public_view(detail, policy=POLICY)
    assert (item.votes, item.voted, detail.author_name) == (2, True, "Ada")

    updated = await service.update(world.admin, row.id, FeedbackUpdate(title="Broken again"))
    assert updated.title == "Broken again"
    crash = CrashReportCreate(message="TypeError: x is undefined", stack="at f (app.js:1:1)", route="/x")
    filed = await service.report_crash(world.author, crash)
    folded = await service.report_crash(world.author, crash)
    assert filed.stored and not filed.duplicate and folded.duplicate and folded.feedback_id == filed.feedback_id
    counts = await world.repository.count_by_status()
    assert counts[FeedbackStatus.DONE] == 1


async def test_the_erasure_runs_on_the_sql_stores_and_answers_the_files_nobody_else_names(world: World) -> None:
    service = world.service()
    mine = await upload(world.storage, world.author, "mine")
    row = await service.create(world.author, FeedbackCreate(title="Mine", screenshot_url=mine))
    reply_file = await upload(world.storage, world.admin, "answer")
    await service.reply(world.admin, row.id, FeedbackReplyCreate(body="Seen", attachment_url=reply_file))
    other = await service.create(world.other, FeedbackCreate(title="Theirs"))
    await service.comment(world.author, other.id, FeedbackCommentCreate(body="Me too, Ada Lovelace"))
    erased = await service.erase_author(world.author.id, [world.author.email, "Ada Lovelace"])
    assert (erased.reports, erased.comments, erased.votes, erased.replies) == (1, 1, 1, 0)
    assert erased.keys == (POLICY.key_of(mine),)
    assert row.user_id is None and row.screenshot_url is None
    (comment,) = await world.comments.for_report(other.id)
    assert comment.user_id is None and "Ada Lovelace" not in comment.body


async def test_a_deleted_comment_s_file_is_answered_when_nothing_else_names_it(world: World) -> None:
    service = world.service()
    row = await service.create(world.author, FeedbackCreate(title="Commented"))
    url = await upload(world.storage, world.other, "comment")
    comment = await service.comment(world.other, row.id, FeedbackCommentCreate(body="See", attachment_url=url))
    assert await service.delete_comment(world.other, row.id, comment.id) == (POLICY.key_of(url),)
    assert await world.comments.get(row.id, comment.id) is None
    assert issubclass(FeedbackAttachment, FeedbackAttachmentColumns)
