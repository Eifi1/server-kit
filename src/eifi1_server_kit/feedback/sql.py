"""The feedback tables as SQLAlchemy declarative mixins, and the feedback ports' stores over
them: the ``sql`` extra (``docs/server-services-harmonization.md`` §2.3–§2.6 and
``docs/feedback-harmonization.md`` §9.14 in ``Eifi1/ui-kit``).

Each app wrote these tables and stores itself — Kurvenschmiede some 1,100 lines of
``feedback_service.py`` and ``feedback_stores.py``, kastlan its ``feedback_repository.py`` and
``feedback_reply_store.py`` — and :mod:`eifi1_server_kit.auth.sql`'s reasoning holds here too:
the copies drift. For the common case these mixins ARE the tables. The app subclasses each on
its own declarative base, names the table, adds what is its own, and writes its migration:

* :class:`FeedbackColumns`: a report, every column of the contract's §3.1 (``outcome`` too,
  frozen since 0.11 but still read by :class:`~eifi1_server_kit.feedback.FeedbackResponse`)
  and §9.3's ``public`` and ``filed_private``;
* :class:`FeedbackReplyColumns`: an entry of a report's conversation, a reply or a rework
  (``kind``), with at most one file; ``kind`` and ``created_at`` have database defaults, so
  a loop's raw ``INSERT`` writes a reply stamped by the database;
* :class:`FeedbackCommentColumns`: a comment, hidden by an admin or not, with at most one file;
* :class:`FeedbackVoteColumns`: one row per person per report, the primary key saying so;
* :class:`FeedbackAttachmentColumns`: who uploaded which file — the database half of the
  attachment store, whose bytes are an :class:`~eifi1_server_kit.objects.ObjectStore`'s.

**The stores** implement :mod:`eifi1_server_kit.feedback.ports` over models built from these
mixins, on the request's ``AsyncSession``: :class:`SqlFeedbackRepository` (the
``FeedbackRepository`` and the ``PublicFeedbackRepository``), :class:`SqlFeedbackReplyStore`,
:class:`SqlFeedbackCommentStore`, :class:`SqlFeedbackVoteStore`, :class:`SqlPeopleLookup` and
:class:`SqlAttachmentStorage`. Every write flushes and none commits. Every list is ordered by
``created_at`` and then the id, every count is a ``GROUP BY``, and an enum is written as its
``.value``: the columns are plain strings, so a new status is no migration.

**The claim.** A store given ``uploads=`` (the model built from
:class:`FeedbackAttachmentColumns`) lets a writer name only a file they uploaded — a report its
author's, an entry its writer's, a comment its writer's — and refuses another with
:class:`~eifi1_server_kit.feedback.UnknownAttachmentUrlError` (the kit's 422), as the port asks
of an app that records who uploaded a file. An entry without a writer (a loop's, a migrated
one) is the server's own word and is not claimed. That is what keeps
:class:`SqlAttachmentStorage`'s files predicate sound: a file is read through what names it,
so naming must be the uploader's.

**The notices.** The reply and comment stores' ``add`` is where the port fires
``feedback_answered`` and ``feedback_comment``, because it is the one place that sees the
writer. Who hears of it, and how (a mail, a push, the inbox), is the app's: each store takes
``notify=``, an async callable handed the new row and the actor after the flush — for a reply,
never for a rework (§10.2 decision 10). It runs inside the transaction: it reads what it needs
(the report's author, :meth:`SqlFeedbackCommentStore.commenters`,
:func:`~eifi1_server_kit.feedback.feedback_recipients`) and queues the sending with
:class:`~eifi1_server_kit.auth.sql.SqlAfterCommit`, so a rollback sends nothing.

**What this does not do.** It names no table, picks no base and creates no schema (the
migrations are the app's; :func:`~eifi1_server_kit.auth.sql.alembic_render_item` writes the
instant columns as ``timestamptz``). It seals nothing, scopes nothing to a company and knows
no team: an app that does subclasses the store and narrows it (:meth:`SqlFeedbackRepository.visible_to`,
the ``people`` clauses). It deletes no file: the erasure's keys and :meth:`SqlAttachmentStorage.prune`'s
are the app's to purge after its commit (:func:`~eifi1_server_kit.objects.purge_objects`).

**The table names.** Every foreign key to an account is built from ``__accounts_table__``
(default ``"users"``), as in :mod:`~eifi1_server_kit.auth.sql`, and every foreign key to a
report from ``__feedback_table__`` (default ``"feedback"``). Constraints are unnamed, so the
app's ``MetaData(naming_convention=…)`` names them. A subclass that needs table arguments of
its own adds them to :meth:`FeedbackAttachmentColumns.table_args`.

Needs the ``sql`` extra (``eifi1-server-kit[sql]``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

try:
    from sqlalchemy import (
        JSON,
        Boolean,
        ColumnElement,
        ForeignKey,
        Insert,
        Integer,
        Select,
        String,
        Text,
        UniqueConstraint,
        delete,
        false,
        func,
        literal,
        or_,
        select,
        update,
    )
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.dialects.postgresql import insert as postgresql_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Mapped, declared_attr, mapped_column
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.feedback.sql needs SQLAlchemy: install the kit with its sql extra, eifi1-server-kit[sql]."
    ) from exc

from eifi1_server_kit.auth.sql import UtcDateTime, _assign, _horizon, _new, _PointsAtAccounts, _rowcount
from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.enums import TERMINAL_STATUSES, FeedbackCategory, FeedbackEntryKind, FeedbackStatus
from eifi1_server_kit.feedback.erasure import feedback_attachment_urls
from eifi1_server_kit.feedback.errors import UnknownAttachmentUrlError
from eifi1_server_kit.feedback.ports import (
    FeedbackActor,
    NewFeedback,
    NewFeedbackComment,
    NewFeedbackReply,
    PublicListEntry,
    StoredAttachment,
)
from eifi1_server_kit.feedback.schemas import MAX_SCREENSHOT_URL_LENGTH, MAX_TITLE_LENGTH, PublicSort
from eifi1_server_kit.objects import ObjectNotFoundError, ObjectStore
from eifi1_server_kit.translation_review.scope import LIKE_ESCAPE, _like_literal
from eifi1_server_kit.uploads import CheckedUpload, MintKeyFn, content_addressed_key

__all__ = [
    "FEEDBACK_OBJECT_PREFIX",
    "FEEDBACK_TABLE",
    "FeedbackAttachmentColumns",
    "FeedbackColumns",
    "FeedbackCommentColumns",
    "FeedbackReplyColumns",
    "FeedbackVoteColumns",
    "SqlAttachmentStorage",
    "SqlFeedbackCommentStore",
    "SqlFeedbackReplyStore",
    "SqlFeedbackRepository",
    "SqlFeedbackVoteStore",
    "SqlPeopleLookup",
    "UploadPrune",
]

#: The report table every entry's, comment's and vote's foreign key points at unless a
#: subclass says otherwise (``__feedback_table__``).
FEEDBACK_TABLE = "feedback"

#: Where :class:`SqlAttachmentStorage` keeps a file in its object store: ``feedback/<key>``,
#: keksdose's folder.
FEEDBACK_OBJECT_PREFIX = "feedback/"

#: A status's stored value: ``"NEEDS_LIVE_TEST"``, the longest, and room.
_STATUS_WIDTH = 20
#: A category's stored value (``"QUESTION"``) and room.
_CATEGORY_WIDTH = 16
#: An entry's ``kind`` (``"reply"``, ``"rework"``).
_KIND_WIDTH = 12
#: :func:`~eifi1_server_kit.feedback.crash_fingerprint`'s 32 hex characters.
_FINGERPRINT_WIDTH = 32
#: A key the app's policy accepts: keksdose's 17 characters, kastlan's 50, an opaque one more.
_KEY_WIDTH = 255
#: A checked upload's media type (``"application/pdf"``).
_MEDIA_TYPE_WIDTH = 100
#: The name an upload came with; a longer one is cut to it.
_FILENAME_WIDTH = 255

#: JSON, as JSONB on PostgreSQL, with ``None`` stored as SQL ``NULL`` rather than JSON ``null``:
#: "no files" and "no context" are then ``IS NULL`` on every database.
_JSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class _PointsAtFeedback:
    """The report table's name for the foreign keys of the mixins below."""

    __feedback_table__: ClassVar[str] = FEEDBACK_TABLE

    @classmethod
    def _feedback_id(cls) -> str:
        return f"{cls.__feedback_table__}.id"


class FeedbackColumns(_PointsAtAccounts):
    """A report (the contract's §3.1, §9.3), read by the kit through
    :class:`~eifi1_server_kit.feedback.PublicFeedbackRow` as it is.

    ``user_id`` is ``SET NULL``: an erased account's reports stay, anonymised by
    :meth:`~eifi1_server_kit.feedback.FeedbackService.erase_author`, because the admin's half
    of the conversation is in them too. ``status`` and ``category`` are plain strings,
    ``context`` and ``attachment_urls`` JSON (``NULL`` for none), ``updated_at`` moves on every
    write."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    @declared_attr
    def user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    title: Mapped[str] = mapped_column(String(MAX_TITLE_LENGTH), nullable=False)
    #: ``""`` when the author gave none, never ``NULL``.
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: A :class:`~eifi1_server_kit.feedback.FeedbackCategory` value.
    category: Mapped[str] = mapped_column(String(_CATEGORY_WIDTH), nullable=False)
    #: A :class:`~eifi1_server_kit.feedback.FeedbackStatus` value.
    status: Mapped[str] = mapped_column(String(_STATUS_WIDTH), default=FeedbackStatus.OPEN.value, nullable=False)
    #: Where the author was and who they were (§3.1's ``context``); ``NULL`` when nothing was.
    context: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    #: The one captured screenshot's URL.
    screenshot_url: Mapped[str | None] = mapped_column(String(MAX_SCREENSHOT_URL_LENGTH), nullable=True)
    #: Every other file's URL, in the order added; ``NULL`` for none, never ``[]``.
    attachment_urls: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    #: The one answer before replies (0.11): read-only, written by nothing, answered by the
    #: views as the newest reply's body. Kept because the response model reads it.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The crash's identity, for the fold's "is this one already open?".
    crash_fingerprint: Mapped[str | None] = mapped_column(String(_FINGERPRINT_WIDTH), index=True, nullable=True)
    #: Set on entering DONE or WONT_DO, cleared on leaving them (the kit's plans decide it).
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    #: "Visible to others" (§9.3); a CRASH is never public, whatever this says.
    public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Filed while every report was private: its author may switch it off at any status.
    #: Written by an app's migration, never by the kit.
    filed_private: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False)


class FeedbackReplyColumns(_PointsAtFeedback, _PointsAtAccounts):
    """One entry of a report's conversation (§9.7, §10.3): an answer — an admin's or a loop's
    — or, ``kind = 'rework'``, the author sending it back; at most one file
    (``attachment_url``). No edit and no delete: a correction is a new entry. ``author_id`` is
    ``NULL`` for a loop's reply, a migrated outcome and an erased writer's entry.

    ``kind`` defaults to ``'reply'`` and ``created_at`` to ``now()`` in the database as well,
    so a loop's raw ``INSERT`` naming neither writes a reply, and can never write a rework."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    @declared_attr
    def feedback_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._feedback_id(), ondelete="CASCADE"), index=True, nullable=False)

    @declared_attr
    def author_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: The status before the move, the admin's view only; ``NULL``: no move.
    status_from: Mapped[str | None] = mapped_column(String(_STATUS_WIDTH), nullable=True)
    status_to: Mapped[str | None] = mapped_column(String(_STATUS_WIDTH), nullable=True)
    #: A :class:`~eifi1_server_kit.feedback.FeedbackEntryKind` value.
    kind: Mapped[str] = mapped_column(
        String(_KIND_WIDTH),
        default=FeedbackEntryKind.REPLY.value,
        server_default=FeedbackEntryKind.REPLY.value,
        nullable=False,
    )
    attachment_url: Mapped[str | None] = mapped_column(String(MAX_SCREENSHOT_URL_LENGTH), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, server_default=func.now(), nullable=False
    )


class FeedbackCommentColumns(_PointsAtFeedback, _PointsAtAccounts):
    """One comment (§9.8), by anybody who sees the report. An admin hides one
    (``hidden_at``, ``hidden_by``) rather than deleting it; its writer deletes their own.
    ``user_id`` and ``hidden_by`` are ``SET NULL``: an erased writer's comment stays, scrubbed
    and shown as "A user", and an erased admin's hide stays a hide. ``staff`` is ``False`` on
    every row the kit writes: the column is the contract's, for an app's migrated comments."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    @declared_attr
    def feedback_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._feedback_id(), ondelete="CASCADE"), index=True, nullable=False)

    @declared_attr
    def user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    body: Mapped[str] = mapped_column(Text, nullable=False)
    staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    @declared_attr
    def hidden_by(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    #: Its one file (0.18, §12), read by whoever reads the comment while it is not hidden.
    attachment_url: Mapped[str | None] = mapped_column(String(MAX_SCREENSHOT_URL_LENGTH), nullable=True)


class FeedbackVoteColumns(_PointsAtFeedback, _PointsAtAccounts):
    """A vote, "I want this too" (§9.6): one row per person per report, the primary key saying
    so. Who voted never leaves the server; the vote goes with its account and its report."""

    @declared_attr
    def feedback_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._feedback_id(), ondelete="CASCADE"), primary_key=True)

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="CASCADE"), primary_key=True, index=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)


class FeedbackAttachmentColumns(_PointsAtAccounts):
    """An uploaded file, by whom (§2.5, §9.10): the database half of
    :class:`SqlAttachmentStorage`, whose bytes are in an object store under the same key.

    A file is uploaded before the report, entry or comment that names it, and is tied to
    none: what names it is the URL in their columns. One row per key and uploader — the same
    bytes are one key (content-addressed), and two people who upload them each have a row,
    so each reads their own. ``user_id`` is ``SET NULL``: an erased person's uploads lose
    their uploader, and :meth:`SqlAttachmentStorage.prune` takes the ones nothing names.
    ``created_at`` is the last upload's, so a file uploaded again waits for its report anew."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(_KEY_WIDTH), nullable=False)

    @declared_attr
    def user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    #: The checked type (:class:`~eifi1_server_kit.uploads.CheckedUpload`'s), never the
    #: request's header. The download reads the bytes again all the same.
    media_type: Mapped[str] = mapped_column(String(_MEDIA_TYPE_WIDTH), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(_FILENAME_WIDTH), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, index=True, nullable=False)

    @classmethod
    def table_args(cls) -> tuple[Any, ...]:
        """One row per key and uploader, a fresh constraint per table; its leading column
        serves the lookup by key."""
        return (UniqueConstraint("key", "user_id"),)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return cls.table_args()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _value(value: Any) -> Any:
    """An enum as its string column stores it (the port: "a string column stores .value")."""
    return value.value if isinstance(value, Enum) else value


def _columns(changes: Mapping[str, Any]) -> dict[str, Any]:
    """The kit's changes as the columns hold them: each enum's value, the JSON columns as
    fresh objects (so a change is seen), and no files as ``None``, never ``[]``."""
    values = {name: _value(value) for name, value in changes.items()}
    if "attachment_urls" in values:
        values["attachment_urls"] = list(values["attachment_urls"] or ()) or None
    if values.get("context") is not None:
        values["context"] = dict(values["context"])
    return values


def _insert(
    model: type[Any],
    values: Mapping[str, Any],
    dialect: str,
    *,
    conflict: Sequence[str],
    refresh: Sequence[str] = (),
) -> Insert:
    """An INSERT that, when the row exists, does nothing (a second vote) or writes ``refresh``
    from the new values (an upload's stamp): one statement, so two requests at once cannot
    both insert (a read-then-insert could) and the loser's transaction is not left failed (an
    IntegrityError would)."""
    if dialect == "postgresql":
        pg = postgresql_insert(model).values(dict(values))
        if refresh:
            return pg.on_conflict_do_update(
                index_elements=list(conflict), set_={name: pg.excluded[name] for name in refresh}
            )
        return pg.on_conflict_do_nothing(index_elements=list(conflict))
    if dialect == "sqlite":
        lite = sqlite_insert(model).values(dict(values))
        if refresh:
            return lite.on_conflict_do_update(
                index_elements=list(conflict), set_={name: lite.excluded[name] for name in refresh}
            )
        return lite.on_conflict_do_nothing(index_elements=list(conflict))
    raise NotImplementedError(f"the feedback stores know PostgreSQL and SQLite, not {dialect}")


def _dialect(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


async def _claim(
    session: AsyncSession,
    uploads: type[FeedbackAttachmentColumns],
    policy: AttachmentUrlPolicy,
    urls: Sequence[str],
    writer_id: int,
) -> None:
    """Refuse a URL that is not one of ``writer_id``'s own uploads (the module's claim)."""
    wanted = {url: policy.key_of(url) for url in dict.fromkeys(urls)}
    keys = [key for key in wanted.values() if key is not None]
    held = set(await session.scalars(select(uploads.key).where(uploads.user_id == writer_id, uploads.key.in_(keys))))
    for url, key in wanted.items():
        if key not in held:
            raise UnknownAttachmentUrlError(
                f"{url} is not a file you uploaded: upload it, then name the URL the upload answered"
            )


async def _urls_named(
    session: AsyncSession,
    reports: type[FeedbackColumns],
    replies: type[FeedbackReplyColumns] | None,
    comments: type[FeedbackCommentColumns] | None,
    policy: AttachmentUrlPolicy,
    *,
    except_ids: Collection[int] = (),
) -> set[str]:
    """Every attachment URL the reports outside ``except_ids`` name — a column or a rework
    line of the body — and every entry's and comment's file on any report (0.18, §12)."""
    query = select(reports.screenshot_url, reports.attachment_urls, reports.body)
    if except_ids:
        query = query.where(reports.id.not_in(list(except_ids)))
    urls: set[str] = set()
    for screenshot_url, attachment_urls, body in (await session.execute(query)).all():
        urls |= feedback_attachment_urls(
            screenshot_url=screenshot_url, attachment_urls=attachment_urls, body=body, policy=policy
        )
    for model in (replies, comments):
        if model is not None:
            urls |= set(await session.scalars(select(model.attachment_url).where(model.attachment_url.is_not(None))))
    return urls


def _people(accounts: type[Any]) -> ColumnElement[bool] | None:
    """The ``WHERE`` clause of an account that is a person: no demo where the model has the
    column (:class:`~eifi1_server_kit.auth.sql.DemoAccountColumn`), else ``None``."""
    demo = getattr(accounts, "is_demo", None)
    return None if demo is None else demo.is_(False)


# ── The repository ──────────────────────────────────────────────────────────


class SqlFeedbackRepository[ReportT: FeedbackColumns]:
    """:class:`~eifi1_server_kit.feedback.FeedbackRepository` and
    :class:`~eifi1_server_kit.feedback.PublicFeedbackRepository` over a model built from
    :class:`FeedbackColumns`.

    ``accounts`` is the account model (the inbox's address, the demo accounts
    :meth:`count_by_status` leaves out): :class:`~eifi1_server_kit.auth.sql.AccountColumns`'
    model or the app's own, with ``id`` and ``email`` and, where it has one, ``is_demo``.
    ``policy`` is the app's attachment URL shape. ``replies`` and ``comments`` add their files
    to :meth:`urls_named_except` (0.18) and ``comments`` and ``votes`` their counts to
    :meth:`public_page`; ``uploads`` turns the claim on (:meth:`add`).

    :meth:`get` answers any row by id; an app that hides a row from a viewer (Kurvenschmiede's
    ``visible_to``) subclasses and narrows :meth:`visible_to`, one with a system account of its
    own :meth:`people`."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[ReportT],
        *,
        accounts: type[Any],
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
        replies: type[FeedbackReplyColumns] | None = None,
        comments: type[FeedbackCommentColumns] | None = None,
        votes: type[FeedbackVoteColumns] | None = None,
        uploads: type[FeedbackAttachmentColumns] | None = None,
    ) -> None:
        self._session = session
        self._model = model
        self._accounts = accounts
        self._policy = policy
        self._replies = replies
        self._comments = comments
        self._votes = votes
        self._uploads = uploads

    def visible_to(self, viewer: FeedbackActor) -> ColumnElement[bool] | None:
        """The ``WHERE`` clause of the rows :meth:`get` answers ``viewer``; ``None``: every
        row, and the service's ``foreign_row`` decides what another person's row answers."""
        return None

    def people(self) -> ColumnElement[bool] | None:
        """The ``WHERE`` clause, on the account model, of an author whose reports count: no
        demo account where the model has the column, else ``None`` for every account."""
        return _people(self._accounts)

    def _newest_first(self, query: Select[Any]) -> Select[Any]:
        model = self._model
        return query.order_by(model.created_at.desc(), model.id.desc())

    async def get(self, feedback_id: int, *, viewer: FeedbackActor) -> ReportT | None:
        query = select(self._model).where(self._model.id == feedback_id)
        visible = self.visible_to(viewer)
        if visible is not None:
            query = query.where(visible)
        return (await self._session.scalars(query)).one_or_none()

    async def list_mine(self, user_id: int) -> Sequence[ReportT]:
        query = self._newest_first(select(self._model).where(self._model.user_id == user_id))
        return list(await self._session.scalars(query))

    async def inbox(self, status: FeedbackStatus | None) -> Sequence[tuple[ReportT, str | None]]:
        model, accounts = self._model, self._accounts
        query = self._newest_first(select(model, accounts.email).outerjoin(accounts, accounts.id == model.user_id))
        if status is not None:
            query = query.where(model.status == FeedbackStatus(status).value)
        return [(row, email) for row, email in (await self._session.execute(query)).all()]

    async def count_by_status(self) -> Mapping[FeedbackStatus, int]:
        model = self._model
        query = select(model.status, func.count()).group_by(model.status)
        people = self.people()
        if people is not None:
            authors = select(self._accounts.id).where(people)
            query = query.where(or_(model.user_id.is_(None), model.user_id.in_(authors)))
        counts = (await self._session.execute(query)).all()
        return {FeedbackStatus(status): int(count) for status, count in counts}

    async def open_crash(self, fingerprint: str) -> ReportT | None:
        model = self._model
        settled = [status.value for status in TERMINAL_STATUSES]
        query = select(model).where(model.crash_fingerprint == fingerprint, model.status.not_in(settled))
        return (await self._session.scalars(self._newest_first(query).limit(1))).first()

    async def add(self, new: NewFeedback) -> ReportT:
        """Insert and flush. With ``uploads``, the screenshot and every file named must be the
        author's own uploads (:class:`~eifi1_server_kit.feedback.UnknownAttachmentUrlError`)."""
        named = [*([new.screenshot_url] if new.screenshot_url else []), *(new.attachment_urls or ())]
        if self._uploads is not None and named:
            await _claim(self._session, self._uploads, self._policy, named, new.user_id)
        row = _new(self._model, _columns({field.name: getattr(new, field.name) for field in fields(new)}))
        self._session.add(row)
        await self._session.flush()
        return row

    async def apply(self, row: ReportT, changes: Mapping[str, Any], *, actor: FeedbackActor | None) -> ReportT:
        """Assign what the kit decided and flush; a name the model does not map is a
        :class:`ValueError`. The one write of the two file columns is the erasure's, both
        ``None``, so nothing is claimed here."""
        _assign(row, _columns(changes))
        await self._session.flush()
        return row

    async def by_author(self, user_id: int) -> Sequence[ReportT]:
        return await self.list_mine(user_id)

    async def urls_named_except(self, row_ids: Collection[int]) -> set[str]:
        """Every attachment URL the reports outside ``row_ids`` name, in a column or the body,
        and every entry's and comment's file on every report, ``row_ids``' included (0.18).
        Reads every report's body: the erasure's and a deleted comment's question, asked
        rarely."""
        await self._session.flush()
        return await _urls_named(
            self._session, self._model, self._replies, self._comments, self._policy, except_ids=row_ids
        )

    async def public_page(
        self,
        *,
        viewer_id: int,
        sort: PublicSort,
        status: FeedbackStatus | None,
        category: FeedbackCategory | None,
        q: str | None,
        limit: int,
        offset: int,
    ) -> Sequence[PublicListEntry[ReportT]]:
        """Public rows only, filtered, sorted and paged, each with its counts from two
        ``GROUP BY`` subqueries joined to the page — never a query per row. ``q`` is a
        case-blind substring of the title, ``%`` and ``_`` meaning themselves."""
        model = self._model
        vote_count: ColumnElement[Any] = literal(0)
        voted: ColumnElement[Any] = false()
        comment_count: ColumnElement[Any] = literal(0)
        joins: list[tuple[Any, ColumnElement[bool]]] = []
        if self._votes is not None:
            votes = self._votes
            tally = (
                select(
                    votes.feedback_id.label("feedback_id"),
                    func.count().label("votes"),
                    func.count().filter(votes.user_id == viewer_id).label("mine"),
                )
                .group_by(votes.feedback_id)
                .subquery()
            )
            joins.append((tally, tally.c.feedback_id == model.id))
            vote_count = func.coalesce(tally.c.votes, 0)
            voted = func.coalesce(tally.c.mine, 0) > 0
        if self._comments is not None:
            comments = self._comments
            shown = (
                select(comments.feedback_id.label("feedback_id"), func.count().label("comments"))
                .where(comments.hidden_at.is_(None))
                .group_by(comments.feedback_id)
                .subquery()
            )
            joins.append((shown, shown.c.feedback_id == model.id))
            comment_count = func.coalesce(shown.c.comments, 0)
        query = select(model, vote_count, voted, comment_count)
        for subquery, on in joins:
            query = query.outerjoin(subquery, on)
        query = query.where(model.public.is_(True), model.category != FeedbackCategory.CRASH.value)
        if status is not None:
            query = query.where(model.status == FeedbackStatus(status).value)
        if category is not None:
            query = query.where(model.category == FeedbackCategory(category).value)
        if q:
            query = query.where(model.title.ilike(f"%{_like_literal(q)}%", escape=LIKE_ESCAPE))
        if sort == "votes":
            query = query.order_by(vote_count.desc(), model.created_at.desc(), model.id.desc())
        elif sort == "updated":
            query = query.order_by(model.updated_at.desc(), model.id.desc())
        else:
            query = self._newest_first(query)
        rows = (await self._session.execute(query.limit(limit).offset(offset))).all()
        return [
            PublicListEntry(row=row, votes=int(votes), voted=bool(mine), comment_count=int(shown))
            for row, votes, mine, shown in rows
        ]

    async def get_public(self, feedback_id: int) -> ReportT | None:
        """The row by id, whatever its flag and whoever asks: the service decides who sees it."""
        return (await self._session.scalars(select(self._model).where(self._model.id == feedback_id))).one_or_none()


# ── The entries, the comments, the votes and the names ─────────────────────


class SqlFeedbackReplyStore[ReplyT: FeedbackReplyColumns]:
    """:class:`~eifi1_server_kit.feedback.FeedbackReplyStore` over a model built from
    :class:`FeedbackReplyColumns`.

    ``notify`` is awaited after a reply's insert (never a rework's), with the row and the
    actor: the app's ``feedback_answered``, queued for after the commit. With ``uploads`` an
    entry's file must be its writer's own upload; an entry without a writer is not claimed."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[ReplyT],
        *,
        notify: Callable[[ReplyT, FeedbackActor | None], Awaitable[None]] | None = None,
        uploads: type[FeedbackAttachmentColumns] | None = None,
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
    ) -> None:
        self._session = session
        self._model = model
        self._notify = notify
        self._uploads = uploads
        self._policy = policy

    def _oldest_first(self, query: Select[Any]) -> Select[Any]:
        return query.order_by(self._model.created_at, self._model.id)

    async def for_reports(self, ids: Collection[int]) -> Mapping[int, Sequence[ReplyT]]:
        if not ids:
            return {}
        query = self._oldest_first(select(self._model).where(self._model.feedback_id.in_(list(ids))))
        found: dict[int, list[ReplyT]] = {}
        for entry in await self._session.scalars(query):
            found.setdefault(entry.feedback_id, []).append(entry)
        return found

    async def add(self, new: NewFeedbackReply, *, actor: FeedbackActor | None) -> ReplyT:
        if self._uploads is not None and new.attachment_url and new.author_id is not None:
            await _claim(self._session, self._uploads, self._policy, [new.attachment_url], new.author_id)
        values = {field.name: _value(getattr(new, field.name)) for field in fields(new)}
        if new.created_at is None:
            # The column's default stamps it.
            del values["created_at"]
        entry = _new(self._model, values)
        self._session.add(entry)
        await self._session.flush()
        if self._notify is not None and entry.kind == FeedbackEntryKind.REPLY.value:
            await self._notify(entry, actor)
        return entry

    async def by_author(self, user_id: int) -> Sequence[ReplyT]:
        query = self._oldest_first(select(self._model).where(self._model.author_id == user_id))
        return list(await self._session.scalars(query))

    async def apply(self, reply: ReplyT, changes: Mapping[str, Any]) -> ReplyT:
        _assign(reply, _columns(changes))
        await self._session.flush()
        return reply


class SqlFeedbackCommentStore[CommentT: FeedbackCommentColumns]:
    """:class:`~eifi1_server_kit.feedback.FeedbackCommentStore` over a model built from
    :class:`FeedbackCommentColumns`.

    ``notify`` is awaited after every insert, with the row and its writer: the app's
    ``feedback_comment``. With ``uploads`` a comment's file must be its writer's own upload.
    A deleted comment's file stays as an upload nothing names, which
    :meth:`SqlAttachmentStorage.prune` takes in its time; the store is no
    :class:`~eifi1_server_kit.feedback.FeedbackCommentFilePurge`."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[CommentT],
        *,
        notify: Callable[[CommentT, FeedbackActor], Awaitable[None]] | None = None,
        uploads: type[FeedbackAttachmentColumns] | None = None,
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
    ) -> None:
        self._session = session
        self._model = model
        self._notify = notify
        self._uploads = uploads
        self._policy = policy

    def _oldest_first(self, query: Select[Any]) -> Select[Any]:
        return query.order_by(self._model.created_at, self._model.id)

    async def for_report(self, feedback_id: int) -> Sequence[CommentT]:
        query = self._oldest_first(select(self._model).where(self._model.feedback_id == feedback_id))
        return list(await self._session.scalars(query))

    async def counts(self, ids: Collection[int], *, include_hidden: bool) -> Mapping[int, int]:
        if not ids:
            return {}
        model = self._model
        query = select(model.feedback_id, func.count()).where(model.feedback_id.in_(list(ids)))
        if not include_hidden:
            query = query.where(model.hidden_at.is_(None))
        counts = (await self._session.execute(query.group_by(model.feedback_id))).all()
        return {feedback_id: int(count) for feedback_id, count in counts}

    async def commenters(self, feedback_id: int) -> Sequence[int]:
        model = self._model
        query = self._oldest_first(
            select(model.user_id).where(model.feedback_id == feedback_id, model.user_id.is_not(None))
        )
        return list(dict.fromkeys(user_id for user_id in await self._session.scalars(query) if user_id is not None))

    async def add(self, new: NewFeedbackComment, *, actor: FeedbackActor) -> CommentT:
        if self._uploads is not None and new.attachment_url:
            await _claim(self._session, self._uploads, self._policy, [new.attachment_url], new.user_id)
        comment = _new(self._model, {field.name: getattr(new, field.name) for field in fields(new)})
        self._session.add(comment)
        await self._session.flush()
        if self._notify is not None:
            await self._notify(comment, actor)
        return comment

    async def get(self, feedback_id: int, comment_id: int) -> CommentT | None:
        model = self._model
        query = select(model).where(model.id == comment_id, model.feedback_id == feedback_id)
        return (await self._session.scalars(query)).one_or_none()

    async def apply(self, comment: CommentT, changes: Mapping[str, Any], *, actor: FeedbackActor | None) -> CommentT:
        _assign(comment, _columns(changes))
        await self._session.flush()
        return comment

    async def delete(self, comment: CommentT) -> None:
        await self._session.delete(comment)
        await self._session.flush()

    async def by_author(self, user_id: int) -> Sequence[CommentT]:
        query = self._oldest_first(select(self._model).where(self._model.user_id == user_id))
        return list(await self._session.scalars(query))

    async def clear_hidden_by(self, user_id: int) -> int:
        model = self._model
        await self._session.flush()
        # "fetch": the comments this session already holds read as cleared too, at once.
        statement = update(model).where(model.hidden_by == user_id).values(hidden_by=None)
        return _rowcount(await self._session.execute(statement.execution_options(synchronize_session="fetch")))


class SqlFeedbackVoteStore[VoteT: FeedbackVoteColumns]:
    """:class:`~eifi1_server_kit.feedback.FeedbackVoteStore` over a model built from
    :class:`FeedbackVoteColumns`: a second ``put`` is ``ON CONFLICT DO NOTHING``, one
    statement, never a read and a decision two requests could both make."""

    def __init__(self, session: AsyncSession, model: type[VoteT]) -> None:
        self._session = session
        self._model = model

    async def counts(self, ids: Collection[int], *, viewer_id: int) -> Mapping[int, tuple[int, bool]]:
        if not ids:
            return {}
        model = self._model
        query = (
            select(model.feedback_id, func.count(), func.count().filter(model.user_id == viewer_id))
            .where(model.feedback_id.in_(list(ids)))
            .group_by(model.feedback_id)
        )
        counts = (await self._session.execute(query)).all()
        return {feedback_id: (int(votes), bool(mine)) for feedback_id, votes, mine in counts}

    async def put(self, feedback_id: int, user_id: int) -> None:
        values = {"feedback_id": feedback_id, "user_id": user_id}
        statement = _insert(self._model, values, _dialect(self._session), conflict=("feedback_id", "user_id"))
        await self._session.execute(statement)

    async def delete(self, feedback_id: int, user_id: int) -> None:
        model = self._model
        await self._session.execute(delete(model).where(model.feedback_id == feedback_id, model.user_id == user_id))

    async def delete_by_author(self, user_id: int) -> int:
        return _rowcount(await self._session.execute(delete(self._model).where(self._model.user_id == user_id)))


class SqlPeopleLookup:
    """:class:`~eifi1_server_kit.feedback.PeopleLookup`: the first and last names of a page's
    people, in one query. ``accounts`` is the account model, with ``id``, ``first_name`` and
    ``last_name``. Never a demo account, where the model has the column (shown as "A user");
    an app with a system account of its own subclasses and narrows :meth:`people`."""

    def __init__(self, session: AsyncSession, accounts: type[Any]) -> None:
        self._session = session
        self._accounts = accounts

    def people(self) -> ColumnElement[bool] | None:
        """The ``WHERE`` clause of an account that is a person, or ``None`` for every row."""
        return _people(self._accounts)

    async def names(self, user_ids: Collection[int]) -> Mapping[int, tuple[str, str]]:
        if not user_ids:
            return {}
        accounts = self._accounts
        query = select(accounts.id, accounts.first_name, accounts.last_name).where(accounts.id.in_(list(user_ids)))
        people = self.people()
        if people is not None:
            query = query.where(people)
        rows = (await self._session.execute(query)).all()
        return {user_id: (first or "", last or "") for user_id, first, last in rows}


# ── The files ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UploadPrune:
    """What :meth:`SqlAttachmentStorage.prune` did."""

    #: Upload rows deleted.
    uploads: int = 0
    #: Bare keys no upload row holds any more, sorted: their objects are the app's to
    #: purge after its commit (:meth:`SqlAttachmentStorage.object_key` names each).
    keys: tuple[str, ...] = ()


class SqlAttachmentStorage[UploadT: FeedbackAttachmentColumns]:
    """:class:`~eifi1_server_kit.feedback.AttachmentStorage` over a model built from
    :class:`FeedbackAttachmentColumns` and an :class:`~eifi1_server_kit.objects.ObjectStore`
    (the local disk in development, the bucket in production).

    **save** mints the key — ``mint_key(data, extension)``, keksdose's content address
    ``<sha12>.<ext>`` unless the app passes its own; ``policy`` must accept it — writes the
    object (``prefix`` + key) every time, so a file uploaded again is written anew, then
    the row; the same file twice from one person is one row, stamped again.

    **read** answers the file to whoever the files predicate lets in (:meth:`readable`), and
    ``None`` for anything else — a key nobody saved, an object gone, a viewer not let in —
    alike, so the answer never says that a key exists.

    ``reports``, ``replies`` and ``comments`` are the models whose files :meth:`readable` and
    :meth:`prune` read; ``public_viewer`` the app's predicate, as the service takes it
    (``None``: no public parts, so a comment's file is its writer's and an admin's)."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[UploadT],
        *,
        objects: ObjectStore,
        reports: type[FeedbackColumns],
        replies: type[FeedbackReplyColumns] | None = None,
        comments: type[FeedbackCommentColumns] | None = None,
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
        public_viewer: Callable[[FeedbackActor], bool] | None = None,
        prefix: str = FEEDBACK_OBJECT_PREFIX,
        mint_key: MintKeyFn = content_addressed_key,
    ) -> None:
        self._session = session
        self._model = model
        self._objects = objects
        self._reports = reports
        self._replies = replies
        self._comments = comments
        self._policy = policy
        self._public_viewer = public_viewer
        self._prefix = prefix
        self._mint_key = mint_key

    def object_key(self, key: str) -> str:
        """Where the file of ``key`` is in the object store: ``prefix`` + ``key``."""
        return f"{self._prefix}{key}"

    async def save(self, data: bytes, upload: CheckedUpload, *, uploader: FeedbackActor, filename: str | None) -> str:
        key = self._mint_key(data, upload.extension)
        if not self._policy.is_key(key):
            raise ValueError(f"mint_key answered {key!r}, which the policy refuses")
        # The object first, the row after it: a row never names bytes that are not there.
        await self._objects.save(self.object_key(key), data, content_type=upload.media_type)
        values = {
            "key": key,
            "user_id": uploader.id,
            "media_type": upload.media_type,
            "filename": None if filename is None else filename[:_FILENAME_WIDTH],
        }
        statement = _insert(
            self._model, values, _dialect(self._session), conflict=("key", "user_id"), refresh=("created_at",)
        )
        await self._session.execute(statement)
        return key

    def _sees_public(self, viewer: FeedbackActor) -> bool:
        """The service's ``sees_public`` for a viewer who is not an admin."""
        return not viewer.is_demo and self._public_viewer is not None and self._public_viewer(viewer)

    async def _any(self, query: Select[Any]) -> bool:
        return (await self._session.execute(query.limit(1))).first() is not None

    async def readable(self, key: str, viewer: FeedbackActor) -> bool:
        """The files predicate (feedback §9.10 decision 12, amended in 0.18): an admin; the
        file's uploader; the author of a report an entry names it on (the admin's reply with
        a file); and, for a viewer who sees the public parts, a comment that names it, not
        hidden, on a report that is no CRASH and is public or the viewer's own. Never
        "anyone who can see the report". What a report's own columns name is its author's
        upload (the claim), so the uploader covers it.

        An app with a rule of its own subclasses and overrides this."""
        if viewer.is_admin:
            return True
        model, reports = self._model, self._reports
        if await self._any(select(model.id).where(model.key == key, model.user_id == viewer.id)):
            return True
        url = self._policy.url_for(key)
        if self._replies is not None:
            replies = self._replies
            answered = (
                select(replies.id)
                .join(reports, reports.id == replies.feedback_id)
                .where(replies.attachment_url == url, reports.user_id == viewer.id)
            )
            if await self._any(answered):
                return True
        if self._comments is not None and self._sees_public(viewer):
            comments = self._comments
            shown = (
                select(comments.id)
                .join(reports, reports.id == comments.feedback_id)
                .where(
                    comments.attachment_url == url,
                    comments.hidden_at.is_(None),
                    reports.category != FeedbackCategory.CRASH.value,
                    or_(reports.public.is_(True), reports.user_id == viewer.id),
                )
            )
            return await self._any(shown)
        return False

    async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
        if not self._policy.is_key(key):
            return None
        model = self._model
        rows = list(await self._session.scalars(select(model).where(model.key == key).order_by(model.id)))
        if not rows or not await self.readable(key, viewer):
            return None
        # The viewer's own upload's name where there is one, else the first uploader's.
        row = next((row for row in rows if row.user_id == viewer.id), rows[0])
        try:
            data = await self._objects.read(self.object_key(key))
        except ObjectNotFoundError:
            return None
        return StoredAttachment(data=data, media_type=row.media_type, filename=row.filename)

    async def prune(self, *, before: datetime, now: datetime | None = None) -> UploadPrune:
        """Delete the uploads made before ``before`` that no report, entry or comment names,
        and answer how many, with the keys no row holds any more.

        **Why.** An upload waits for the report that names it; a dialog closed, a tab that
        crashed, a comment deleted or a report erased leaves a file nobody reaches but its
        uploader — a screenshot of their screen, kept for ever unless something deletes it.
        ``before`` is the app's wait (``utc_now() - timedelta(days=1)``, Kurvenschmiede's), and
        must have passed by ``now`` (``utc_now()`` unless a test hands its frozen clock).

        A key another row still holds (the same bytes, uploaded by somebody else or again) is
        not answered: its object is still in use. Purge the answered keys' objects after the
        commit (:meth:`object_key`, :func:`~eifi1_server_kit.objects.purge_objects`). Reads
        every report's body, as :meth:`SqlFeedbackRepository.urls_named_except` does: a
        scheduled job's, never a request's. Flushes first, commits nothing."""
        horizon = _horizon(before, now)
        await self._session.flush()
        model = self._model
        candidates = (await self._session.execute(select(model.id, model.key).where(model.created_at < horizon))).all()
        if not candidates:
            return UploadPrune()
        urls = await _urls_named(self._session, self._reports, self._replies, self._comments, self._policy)
        named = {self._policy.key_of(url) for url in urls}
        gone = [(row_id, key) for row_id, key in candidates if key not in named]
        statement = delete(model).where(model.id.in_([row_id for row_id, _ in gone]))
        deleted = _rowcount(await self._session.execute(statement.execution_options(synchronize_session="fetch")))
        keys = {key for _, key in gone}
        held = set(await self._session.scalars(select(model.key).where(model.key.in_(list(keys)))))
        return UploadPrune(uploads=deleted, keys=tuple(sorted(keys - held)))
