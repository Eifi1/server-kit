"""The ports the feedback service runs on: who asks, the rows, the files and the clock.

Layer 2 (``docs/server-services-harmonization.md`` §2.2–§2.6 in ``Eifi1/ui-kit``). Each app
implements :class:`FeedbackRepository` and :class:`AttachmentStorage` over its own
session and store, and builds a :class:`FeedbackActor` from its own ``User``; the kit
never sees the app's models, and no SQLAlchemy or FastAPI is imported here.

The ports are async, as the three apps' sessions are, and run in the caller's
transaction: **every write flushes and none commits** (§2.7). An app's row is read through
:class:`FeedbackRow` as it is, and every write goes through the repository, so an app's
string columns, claimed files and derived columns stay in its adapter.

**Public feedback** (0.11, ``docs/feedback-harmonization.md`` §9.14): four stores beside
the repository — :class:`FeedbackReplyStore`, :class:`FeedbackCommentStore`,
:class:`FeedbackVoteStore` and :class:`PeopleLookup`, the one read of ``users`` — and
:class:`PublicFeedbackRepository`, which the app's repository implements too. Each store
is asked once per page (the views batch by id), and each write flushes and none commits.
The notifications fire in the reply and comment stores' ``add``, which see the actor
(server-services §2.16), sent after the app's commit.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

# The clock is layer 2's, shared with the translation review since 0.10 (§6.2.1); the
# same objects, re-exported here under the names 0.9 gave them.
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackEntryKind, FeedbackStatus
from eifi1_server_kit.feedback.schemas import PublicSort
from eifi1_server_kit.uploads import CheckedUpload

__all__ = [
    "AttachmentStorage",
    "Clock",
    "FeedbackActor",
    "FeedbackCommentRow",
    "FeedbackCommentStore",
    "FeedbackReplyRow",
    "FeedbackReplyStore",
    "FeedbackRepository",
    "FeedbackRow",
    "FeedbackVoteStore",
    "NewFeedback",
    "NewFeedbackComment",
    "NewFeedbackReply",
    "PeopleLookup",
    "PublicFeedbackRepository",
    "PublicFeedbackRow",
    "PublicListEntry",
    "StoredAttachment",
    "utc_now",
]


@dataclass(frozen=True, slots=True)
class FeedbackActor:
    """Who is asking (§2.2): the ``viewer`` of a read, the ``uploader`` of a file, the
    ``actor`` of a write.

    One app dependency builds it from the app's own ``get_current_user``. ``is_admin``
    says exactly what the app's ``admin_guard`` lets through. A caller without a session
    (Kurvenschmiede's feedback loop) builds one for the account it files under.
    """

    id: int
    email: str
    #: The context stamp's and a crash body's name.
    display_name: str | None
    #: keksdose role ADMIN; kastlan ADMIN in the acting company; Kurvenschmiede role ADMIN.
    is_admin: bool
    is_demo: bool = False
    #: The app's role name, for its ``public_viewer`` predicate (§9.2 decision 7):
    #: Kurvenschmiede lets ADMIN, MEMBER and REVIEWER in, never CUSTOMER.
    role: str | None = None


class FeedbackRow(Protocol):
    """A report as the kit reads it (§2.3): read-only members, so an app's mapped row
    satisfies it as it is. The kit never assigns to it — every write goes through
    :meth:`FeedbackRepository.apply`. ``created_at`` is read only for the order."""

    @property
    def id(self) -> int: ...
    @property
    def user_id(self) -> int | None: ...
    @property
    def status(self) -> FeedbackStatus | str: ...
    @property
    def title(self) -> str: ...
    @property
    def body(self) -> str: ...
    @property
    def context(self) -> Mapping[str, Any] | None: ...
    @property
    def crash_fingerprint(self) -> str | None: ...
    @property
    def screenshot_url(self) -> str | None: ...
    @property
    def attachment_urls(self) -> Sequence[str] | None: ...
    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class NewFeedback:
    """A row to insert (§2.3): the kit's columns; the app adds its own (kastlan's company)."""

    user_id: int
    title: str
    body: str
    category: FeedbackCategory
    status: FeedbackStatus
    context: dict[str, Any] | None
    screenshot_url: str | None = None
    #: ``None`` for none, never ``[]``.
    attachment_urls: list[str] | None = None
    crash_fingerprint: str | None = None
    #: "Visible to others" (§9.3): a new report is public; the service passes False for a CRASH.
    public: bool = True


class FeedbackRepository[RowT: FeedbackRow](Protocol):
    """The app's feedback rows, on the request's session (§2.4). Async; every write
    flushes and never commits. Ties in a list go to the higher id."""

    async def get(self, feedback_id: int, *, viewer: FeedbackActor) -> RowT | None:
        """The row, or None: missing, or hidden from this viewer (ka's RLS, KS's visible_to)."""

    async def list_mine(self, user_id: int) -> Sequence[RowT]:
        """The author's rows, newest first, the higher id first on a tie."""

    async def inbox(self, status: FeedbackStatus | None) -> Sequence[tuple[RowT, str | None]]:
        """Every row the request may see, in list_mine's order, each with its author's
        current address: an outer join, None once the author is erased."""

    async def open_crash(self, fingerprint: str) -> RowT | None:
        """The newest row with this fingerprint whose status is not in TERMINAL_STATUSES,
        the higher id first on a tie."""

    async def add(self, new: NewFeedback) -> RowT:
        """Insert and flush, so the id is set. The app adds its own columns (ka's company)
        and checks what is its own (KS claims the named uploads as new.user_id's)."""

    async def apply(self, row: RowT, changes: Mapping[str, Any], *, actor: FeedbackActor | None) -> RowT:
        """Assign and flush. The keys are §3.1's columns; the values the kit's (enums
        included: a string column stores .value). `actor` makes the change: the request's
        for an update and a crash's fold, None for the erasure (2.15)."""

    async def by_author(self, user_id: int) -> Sequence[RowT]:
        """The erasure's rows (2.15)."""

    async def urls_named_except(self, row_ids: Collection[int]) -> set[str]:
        """Every attachment URL the rows outside row_ids name, in a column or the body
        (feedback_attachment_urls), and the ``attachment_url`` of their rework entries
        (0.13): the erasure's shared-key check."""


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    """A stored file as :meth:`AttachmentStorage.read` answers it (§2.5)."""

    data: bytes
    #: As stored, or from the key's extension (kk, ka); the kit re-reads the bytes (2.12).
    media_type: str | None
    #: ``None``: served as ``attachment.<ext>``.
    filename: str | None


class AttachmentStorage(Protocol):
    """The app's attachment store (§2.5). It mints the key, so each app keeps its own
    (keksdose ``content_addressed_key``, kastlan ``<uuid32>_<sha12>``, Kurvenschmiede its
    HMAC with its dedupe and its sweep). There is no purge: the kit deletes no file."""

    async def save(self, data: bytes, upload: CheckedUpload, *, uploader: FeedbackActor, filename: str | None) -> str:
        """Store a checked file and answer its key, one the app's AttachmentUrlPolicy accepts."""

    async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
        """The file, or None: missing, or not this viewer's to read (KS).

        **The files predicate** (feedback §9.10, decision 12) is not the report's: a file
        is read by its uploader, the author of a report naming it, or an admin — never
        "anyone who can see the report". Public feedback shows other people a report,
        never its files: no key reaches them (``public_body``), and a store that serves
        any signed-in user (keksdose, kastlan) stays safe only because of that.
        """


# --- public feedback (0.11, feedback §9) ------------------------------------------------


class PublicFeedbackRow(FeedbackRow, Protocol):  # pragma: no cover - a protocol, as the others
    """A report as the public parts read it (§9.3): :class:`FeedbackRow` and the columns
    the public list and detail show."""

    @property
    def category(self) -> FeedbackCategory | str: ...
    @property
    def public(self) -> bool: ...
    @property
    def filed_private(self) -> bool: ...
    @property
    def updated_at(self) -> datetime: ...
    @property
    def resolved_at(self) -> datetime | None: ...


class FeedbackReplyRow(Protocol):
    """One entry of ``feedback_replies``, read-only: an answer (§9.7) or, since 0.13, the
    author's rework (§10.3), told apart by ``kind``. ``author_id`` is ``None`` for a loop's
    reply, a migrated outcome, an erased admin — or an erased author's rework."""

    @property
    def id(self) -> int: ...
    @property
    def feedback_id(self) -> int: ...
    @property
    def author_id(self) -> int | None: ...
    @property
    def body(self) -> str: ...
    @property
    def status_from(self) -> FeedbackStatus | str | None: ...
    @property
    def status_to(self) -> FeedbackStatus | str | None: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def kind(self) -> FeedbackEntryKind | str: ...
    @property
    def attachment_url(self) -> str | None: ...


class FeedbackCommentRow(Protocol):
    """One comment (§9.8): ``feedback_comments``, read-only. ``user_id`` is ``None`` once
    its writer is erased; ``hidden_at`` is set while an admin hides it."""

    @property
    def id(self) -> int: ...
    @property
    def feedback_id(self) -> int: ...
    @property
    def user_id(self) -> int | None: ...
    @property
    def body(self) -> str: ...
    @property
    def staff(self) -> bool: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def hidden_at(self) -> datetime | None: ...
    @property
    def hidden_by(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class NewFeedbackReply:
    """An entry to insert (§9.7, §10.3): a reply, or a ``rework`` with its one file. The
    store adds ``created_at`` (``server_default now()``) unless one is given — the
    migration's explicit stamp — and the app its own columns (kastlan's ``company_id``,
    keksdose's ``notified_at``)."""

    feedback_id: int
    author_id: int | None
    body: str
    status_from: FeedbackStatus | None = None
    status_to: FeedbackStatus | None = None
    kind: FeedbackEntryKind = FeedbackEntryKind.REPLY
    attachment_url: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewFeedbackComment:
    """A comment to insert (§9.8): never ``staff``, never hidden."""

    feedback_id: int
    user_id: int
    body: str


@dataclass(frozen=True, slots=True)
class PublicListEntry[RowT]:
    """One row of the public list with its counts, as the repository's SQL answers them
    (§9.9, B4): ``votes`` and ``comment_count`` (visible comments) are counts, ``voted``
    whether the viewer has a vote."""

    row: RowT
    votes: int
    voted: bool
    comment_count: int


class PublicFeedbackRepository[RowT: PublicFeedbackRow](Protocol):
    """The public reads (§9.9), on the app's repository. Async; reads only."""

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
    ) -> Sequence[PublicListEntry[RowT]]:
        """Public rows only (``public`` and not CRASH), filtered, sorted — ``newest``
        (``created_at``, then id, descending), ``votes`` (most, then newest) or ``updated``
        (``updated_at``, then id) — and paged, each with its counts in SQL (a ``GROUP BY``,
        never per row). ``q`` searches ``title`` (case-insensitive) where it is a
        plaintext column; the service never sends one where it is not."""

    async def get_public(self, feedback_id: int) -> RowT | None:
        """The row by id, whatever its flag and whoever asks, or None when missing (kastlan:
        within the company's RLS). The service decides who sees it."""


class FeedbackReplyStore[ReplyRowT: FeedbackReplyRow](Protocol):
    """The replies and rework entries (§9.7, §10.3), one table. Async; every write flushes
    and none commits."""

    async def for_reports(self, ids: Collection[int]) -> Mapping[int, Sequence[ReplyRowT]]:
        """Each report's entries, both kinds, oldest first (``created_at``, then id); a
        report with none may be missing from the answer. One query for a page."""

    async def add(self, new: NewFeedbackReply, *, actor: FeedbackActor | None) -> ReplyRowT:
        """Insert and flush, ``kind`` and ``attachment_url`` included, and ``created_at``
        when given. For ``kind = 'reply'`` only, fires ``feedback_answered`` (to
        :func:`~eifi1_server_kit.feedback.feedback_recipients`), sent after the commit; a
        ``rework`` entry fires nothing (§10.2 decision 10)."""

    async def by_author(self, user_id: int) -> Sequence[ReplyRowT]:
        """The erasure's entries: those ``user_id`` wrote, both kinds."""

    async def apply(self, reply: ReplyRowT, changes: Mapping[str, Any]) -> ReplyRowT:
        """Assign and flush (the erasure's ``author_id``, ``body`` and ``attachment_url``)."""


class FeedbackCommentStore[CommentRowT: FeedbackCommentRow](Protocol):
    """The comments (§9.8). Async; every write flushes and none commits."""

    async def for_report(self, feedback_id: int) -> Sequence[CommentRowT]:
        """The report's comments, hidden ones included, oldest first (``created_at``, then id)."""

    async def counts(self, ids: Collection[int], *, include_hidden: bool) -> Mapping[int, int]:
        """Each report's comment count, in SQL; a report with none may be missing."""

    async def commenters(self, feedback_id: int) -> Sequence[int]:
        """The writers of the report's comments, each once, in the order of their first
        comment; erased writers left out (for ``feedback_recipients``)."""

    async def add(self, new: NewFeedbackComment, *, actor: FeedbackActor) -> CommentRowT:
        """Insert and flush. Fires ``feedback_comment``, sent after the commit."""

    async def get(self, feedback_id: int, comment_id: int) -> CommentRowT | None:
        """The comment, when it belongs to that report; else None."""

    async def apply(
        self, comment: CommentRowT, changes: Mapping[str, Any], *, actor: FeedbackActor | None
    ) -> CommentRowT:
        """Assign and flush: ``hidden_at`` / ``hidden_by`` (a hide), or the erasure's
        ``body`` / ``user_id`` (``actor=None``)."""

    async def delete(self, comment: CommentRowT) -> None:
        """Delete it (its writer's own) and flush."""

    async def by_author(self, user_id: int) -> Sequence[CommentRowT]:
        """The erasure's comments: those ``user_id`` wrote, hidden ones included."""

    async def clear_hidden_by(self, user_id: int) -> int:
        """Set ``hidden_by`` NULL where it is ``user_id`` (the erasure); how many rows."""


class FeedbackVoteStore(Protocol):
    """The votes (§9.6): one row per person per report. Async; every write flushes."""

    async def counts(self, ids: Collection[int], *, viewer_id: int) -> Mapping[int, tuple[int, bool]]:
        """Each report's ``(votes, the viewer voted)``, in SQL; a report with none may be missing."""

    async def put(self, feedback_id: int, user_id: int) -> None:
        """Add the vote; a second ``put`` is the same one row (idempotent)."""

    async def delete(self, feedback_id: int, user_id: int) -> None:
        """Remove the vote; none to remove is no error."""

    async def delete_by_author(self, user_id: int) -> int:
        """The erasure: delete every vote ``user_id`` cast; how many."""


class PeopleLookup(Protocol):
    """The one read of ``users`` (§9.5): the names the views show."""

    async def names(self, user_ids: Collection[int]) -> Mapping[int, tuple[str, str]]:
        """``(first_name, last_name)`` for each id found; never a demo or system account
        (left out, so it is shown as "A user"). One query for a page."""
