"""The ports the feedback service runs on: who asks, the rows, the files and the clock.

Layer 2 (``docs/server-services-harmonization.md`` §2.2–§2.6 in ``Eifi1/ui-kit``). Each app
implements :class:`FeedbackRepository` and :class:`AttachmentStorage` over its own
session and store, and builds a :class:`FeedbackActor` from its own ``User``; the kit
never sees the app's models, and no SQLAlchemy or FastAPI is imported here.

The ports are async, as the three apps' sessions are, and run in the caller's
transaction: **every write flushes and none commits** (§2.7). An app's row is read through
:class:`FeedbackRow` as it is, and every write goes through the repository, so an app's
string columns, claimed files and derived columns stay in its adapter.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

# The clock is layer 2's, shared with the translation review since 0.10 (§6.2.1); the
# same objects, re-exported here under the names 0.9 gave them.
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackStatus
from eifi1_server_kit.uploads import CheckedUpload

__all__ = [
    "AttachmentStorage",
    "Clock",
    "FeedbackActor",
    "FeedbackRepository",
    "FeedbackRow",
    "NewFeedback",
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
        (feedback_attachment_urls): the erasure's shared-key check."""


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
        """The file, or None: missing, or not this viewer's to read (KS)."""
