"""The ``sql`` extra's feedback and translation review tables, as an app declares them, and the
contract suites run over their stores: shared by the SQLite tests and the opt-in PostgreSQL
run (``tests/test_sql_on_postgresql.py``), so both hold the same models to the same checks.

Two schemas, as ``tests/test_auth_sql.py`` has them: the common one (``users`` with the demo
column, ``feedback``) and one that renames the account and report tables, has no demo column
and carries table arguments and a column of its own.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from eifi1_server_kit.auth.ports import NewAccount
from eifi1_server_kit.auth.sql import AccountColumns, DemoAccountColumn, SqlUserStore
from eifi1_server_kit.feedback import (
    SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    FeedbackActor,
    FeedbackCategory,
    FeedbackStatus,
    NewFeedback,
)
from eifi1_server_kit.feedback.sql import (
    FeedbackAttachmentColumns,
    FeedbackColumns,
    FeedbackCommentColumns,
    FeedbackReplyColumns,
    FeedbackVoteColumns,
    SqlAttachmentStorage,
    SqlFeedbackCommentStore,
    SqlFeedbackReplyStore,
    SqlFeedbackRepository,
    SqlFeedbackVoteStore,
    SqlPeopleLookup,
)
from eifi1_server_kit.feedback.testing import (
    check_comment_store,
    check_public_repository,
    check_reply_store,
    check_repository,
    check_storage,
    check_vote_store,
)
from eifi1_server_kit.objects import LocalObjectStore
from eifi1_server_kit.translation_review.ports import ReviewerName
from eifi1_server_kit.translation_review.sql import SqlTranslationReviewStore, TranslationReviewColumns
from eifi1_server_kit.translation_review.testing import check_store
from eifi1_server_kit.uploads import check_upload

#: keksdose's content-addressed keys, which the storage mints by default.
POLICY = AttachmentUrlPolicy(SHA12_KEY_PATTERN)


class Base(DeclarativeBase):
    pass


class User(AccountColumns, DemoAccountColumn, Base):
    __tablename__ = "users"


class Feedback(FeedbackColumns, Base):
    __tablename__ = "feedback"


class FeedbackReply(FeedbackReplyColumns, Base):
    __tablename__ = "feedback_replies"


class FeedbackComment(FeedbackCommentColumns, Base):
    __tablename__ = "feedback_comments"


class FeedbackVote(FeedbackVoteColumns, Base):
    __tablename__ = "feedback_votes"


class FeedbackAttachment(FeedbackAttachmentColumns, Base):
    __tablename__ = "feedback_attachments"


class TranslationReview(TranslationReviewColumns, Base):
    __tablename__ = "translation_reviews"


class OtherBase(DeclarativeBase):
    pass


class Account(AccountColumns, OtherBase):
    """An app without a demo, whose account table is not ``users``."""

    __tablename__ = "accounts"


class _Renamed:
    """The app's own mixin before the kit's: both table names, said once."""

    __accounts_table__ = "accounts"
    __feedback_table__ = "reports"


class Report(_Renamed, FeedbackColumns, OtherBase):
    __tablename__ = "reports"
    #: A column of the app's own beside the kit's.
    team: Mapped[str | None] = mapped_column(String(40))


class ReportEntry(_Renamed, FeedbackReplyColumns, OtherBase):
    __tablename__ = "report_entries"


class ReportComment(_Renamed, FeedbackCommentColumns, OtherBase):
    __tablename__ = "report_comments"


class ReportVote(_Renamed, FeedbackVoteColumns, OtherBase):
    __tablename__ = "report_votes"


class ReportUpload(_Renamed, FeedbackAttachmentColumns, OtherBase):
    """Table arguments of its own beside the mixin's rule."""

    __tablename__ = "report_uploads"
    __table_args__ = (*FeedbackAttachmentColumns.table_args(), Index("ix_report_uploads_media", "media_type"))


class Verdict(_Renamed, TranslationReviewColumns, OtherBase):
    __tablename__ = "verdicts"


# ── People and reports ──────────────────────────────────────────────────────


async def person[UserT: AccountColumns](
    session: AsyncSession, model: type[UserT], email: str, *, first: str = "Ada", last: str = "Lovelace"
) -> UserT:
    """An account, through the account layer's own store."""
    new = NewAccount(email=email, password_hash="hash", first_name=first, last_name=last, locale="en", role="MEMBER")
    return await SqlUserStore(session, model).add(new)


def actor_of(account: AccountColumns, *, admin: bool = False) -> FeedbackActor:
    return FeedbackActor(
        id=account.id,
        email=account.email,
        display_name=f"{account.first_name} {account.last_name}",
        is_admin=admin,
        is_demo=bool(getattr(account, "is_demo", False)),
    )


def report(user_id: int, title: str = "A report", **fields: Any) -> NewFeedback:
    values: dict[str, Any] = {
        "user_id": user_id,
        "title": title,
        "body": "Filed by the test.",
        "category": FeedbackCategory.BUG,
        "status": FeedbackStatus.OPEN,
        "context": {"route": "/test"},
    }
    values.update(fields)
    return NewFeedback(**values)


def sees_everyone(actor: FeedbackActor) -> bool:
    """A ``public_viewer`` that lets every signed-in account in (keksdose, Kurvenschmiede)."""
    return True


async def upload(storage: SqlAttachmentStorage[Any], uploader: FeedbackActor, note: str) -> str:
    """A small text file of ``uploader``'s, saved through ``storage``, as its URL."""
    data = f"{note} by {uploader.email} ({secrets.token_hex(4)})\n".encode()
    key = await storage.save(data, check_upload(data, "text/plain"), uploader=uploader, filename=f"{note}.txt")
    return POLICY.url_for(key)


# ── The suites ──────────────────────────────────────────────────────────────


async def run_feedback_suites(session: AsyncSession, files: Path, *, renamed: bool = False) -> None:
    """Every feedback contract suite over the SQL stores of one schema, on ``session``: the
    storage, the repository (with the 0.18 shared-key rule, and a demo where the schema has
    one), the public reads, the replies (claimed and not), the comments and the votes. Each
    suite asserts only on what it adds, so this runs twice on one database."""
    accounts: type[AccountColumns] = Account if renamed else User
    reports: type[FeedbackColumns] = Report if renamed else Feedback
    entries: type[FeedbackReplyColumns] = ReportEntry if renamed else FeedbackReply
    remarks: type[FeedbackCommentColumns] = ReportComment if renamed else FeedbackComment
    ballots: type[FeedbackVoteColumns] = ReportVote if renamed else FeedbackVote
    uploads: type[FeedbackAttachmentColumns] = ReportUpload if renamed else FeedbackAttachment

    tag = secrets.token_hex(4)
    author = actor_of(await person(session, accounts, f"author-{tag}@example.org"))
    other = actor_of(await person(session, accounts, f"other-{tag}@example.org", first="Grace", last="Hopper"))
    admin = actor_of(await person(session, accounts, f"admin-{tag}@example.org", last="Admin"), admin=True)
    demo: FeedbackActor | None = None
    if not renamed:
        account = await person(session, User, f"demo-{tag}@example.org")
        account.is_demo = True
        await session.flush()
        demo = actor_of(account)

    storage = SqlAttachmentStorage(
        session,
        uploads,
        objects=LocalObjectStore(files),
        reports=reports,
        replies=entries,
        comments=remarks,
        policy=POLICY,
        public_viewer=sees_everyone,
    )
    replies = SqlFeedbackReplyStore(session, entries, uploads=uploads, policy=POLICY)
    comments = SqlFeedbackCommentStore(session, remarks, uploads=uploads, policy=POLICY)
    votes = SqlFeedbackVoteStore(session, ballots)
    repository = SqlFeedbackRepository(
        session,
        reports,
        accounts=accounts,
        policy=POLICY,
        replies=entries,
        comments=remarks,
        votes=ballots,
        uploads=uploads,
    )

    await check_storage(storage, policy=POLICY, uploader=author)
    await check_repository(
        repository,
        storage=storage,
        policy=POLICY,
        author=author,
        other=other,
        demo=demo,
        replies=replies,
        comments=comments,
    )
    await check_public_repository(repository, votes=votes, author=author, other=other)

    answered = await repository.add(report(author.id, "To answer"))
    reply_file = await upload(storage, admin, "reply")
    await check_reply_store(replies, feedback_id=answered.id, admin=admin, policy=POLICY, attachment_url=reply_file)
    # A store that claims nothing takes any URL of the policy's: the suite's own default.
    await check_reply_store(
        SqlFeedbackReplyStore(session, entries), feedback_id=answered.id, admin=admin, policy=POLICY
    )

    commented = await repository.add(report(author.id, "To comment on"))
    comment_file = await upload(storage, author, "comment")
    await check_comment_store(
        comments,
        feedback_id=commented.id,
        author=author,
        other=other,
        admin=admin,
        policy=POLICY,
        attachment_url=comment_file,
    )

    wanted = await repository.add(report(author.id, "To vote on"))
    await check_vote_store(votes, feedback_id=wanted.id, voter=author, other=other)

    names = await SqlPeopleLookup(session, accounts).names([author.id, other.id])
    assert names == {author.id: ("Ada", "Lovelace"), other.id: ("Grace", "Hopper")}


async def run_translation_review_suite(session: AsyncSession, *, renamed: bool = False) -> None:
    """``translation_review.testing.check_store`` over the SQL store of one schema."""
    accounts: type[AccountColumns] = Account if renamed else User
    model: type[TranslationReviewColumns] = Verdict if renamed else TranslationReview
    email = f"reviewer-{secrets.token_hex(4)}@example.org"
    reviewer = await person(session, accounts, email, first="Rita", last="Reviewer")
    store = SqlTranslationReviewStore(session, model, accounts=accounts)
    await check_store(store, reviewer_id=reviewer.id, name=ReviewerName("Rita", "Reviewer"), locale="de-CH")
