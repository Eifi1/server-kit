"""Test helpers for the feedback service: in-memory fakes and three conformance checks.

``docs/server-services-harmonization.md`` §2.14 in ``Eifi1/ui-kit``. In the wheel and NOT
re-exported from :mod:`eifi1_server_kit.feedback`: import it as
``eifi1_server_kit.feedback.testing``. No FastAPI and no httpx.

* :class:`InMemoryFeedbackRepository` and :class:`InMemoryAttachmentStorage` — the fakes
  the kit's own tests run the service and the router on. The repository records every
  write in :attr:`InMemoryFeedbackRepository.writes`, so a test asserts the one-write rule
  (§2.7);
* :func:`actor` and :func:`fixed_clock`;
* :func:`check_repository` and :func:`check_storage` — what an app runs once against its
  own adapters, on its test database as it is: they assert only on what they add;
* :func:`check_round_trip` — an upload, then its download through the URL the upload
  answered, over the app's own client; it also checks that ``policy.prefix`` is the mount.

Public feedback (0.11, ``docs/feedback-harmonization.md`` §9.14): the fakes
:class:`InMemoryReplyStore`, :class:`InMemoryCommentStore`, :class:`InMemoryVoteStore`
and :class:`InMemoryPeopleLookup` (each logs its calls in ``calls``, so a test counts one
call per store per page); :class:`InMemoryFeedbackRepository` is a
:class:`~eifi1_server_kit.feedback.PublicFeedbackRepository` too; the conformance checks
:func:`check_reply_store`, :func:`check_comment_store`, :func:`check_vote_store` and
:func:`check_public_repository`; and :func:`assert_public_view`, which walks a public
answer for any key outside §9.10's lists and any attachment URL.

The admins' alert (0.16, feedback §11): :class:`RecordingAlerts`.

Synthetic data only: every file is made here, with a fresh token in it, so two runs on
the same database never share a content-addressed key.
"""

from __future__ import annotations

import dataclasses
import secrets
import struct
import zlib
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from eifi1_server_kit.feedback.alerts import FeedbackAlert
from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.body import attachment_line
from eifi1_server_kit.feedback.enums import TERMINAL_STATUSES, FeedbackCategory, FeedbackEntryKind, FeedbackStatus
from eifi1_server_kit.feedback.erasure import feedback_attachment_urls
from eifi1_server_kit.feedback.ports import (
    AttachmentStorage,
    Clock,
    FeedbackActor,
    FeedbackCommentStore,
    FeedbackReplyStore,
    FeedbackRepository,
    FeedbackRow,
    FeedbackVoteStore,
    NewFeedback,
    NewFeedbackComment,
    NewFeedbackReply,
    PublicFeedbackRepository,
    PublicListEntry,
    StoredAttachment,
    utc_now,
)
from eifi1_server_kit.feedback.public import (
    PUBLIC_COMMENT_FIELDS,
    PUBLIC_PAGE_MAX,
    PUBLIC_REPLY_FIELDS,
    PUBLIC_REPORT_FIELDS,
    is_public,
)
from eifi1_server_kit.feedback.schemas import PublicSort
from eifi1_server_kit.feedback.service import _served_type
from eifi1_server_kit.uploads import EXTENSION_MEDIA_TYPES, CheckedUpload, check_upload, content_addressed_key

__all__ = [
    "InMemoryAttachmentStorage",
    "InMemoryCommentStore",
    "InMemoryFeedbackRepository",
    "InMemoryPeopleLookup",
    "InMemoryReplyStore",
    "InMemoryVoteStore",
    "MemoryCommentRow",
    "MemoryFeedbackRow",
    "MemoryReplyRow",
    "RecordingAlerts",
    "actor",
    "assert_public_view",
    "check_comment_store",
    "check_public_repository",
    "check_reply_store",
    "check_repository",
    "check_round_trip",
    "check_storage",
    "check_vote_store",
    "fixed_clock",
]

#: An id no test database reaches: the largest a Postgres ``integer`` holds.
_MISSING_ID = 2**31 - 1


@dataclass(slots=True)
class MemoryFeedbackRow:
    """One report in memory: every column of the contract's §3.1, mutable. Satisfies
    :class:`~eifi1_server_kit.feedback.FeedbackRow`, and ``FeedbackResponse.model_validate``
    reads it. Slotted, so a write naming a column that does not exist fails loudly."""

    id: int
    user_id: int | None
    title: str
    body: str
    category: FeedbackCategory
    status: FeedbackStatus
    context: dict[str, Any] | None
    screenshot_url: str | None
    attachment_urls: list[str] | None
    created_at: datetime
    updated_at: datetime
    outcome: str | None = None
    resolved_at: datetime | None = None
    crash_fingerprint: str | None = None
    #: Public feedback (§9.3).
    public: bool = True
    filed_private: bool = False


@dataclass(slots=True)
class MemoryReplyRow:
    """One reply or rework entry in memory (§9.7, §10.3); satisfies
    :class:`~eifi1_server_kit.feedback.FeedbackReplyRow`."""

    id: int
    feedback_id: int
    author_id: int | None
    body: str
    status_from: FeedbackStatus | None
    status_to: FeedbackStatus | None
    created_at: datetime
    kind: FeedbackEntryKind = FeedbackEntryKind.REPLY
    attachment_url: str | None = None


@dataclass(slots=True)
class MemoryCommentRow:
    """One comment in memory (§9.8); satisfies :class:`~eifi1_server_kit.feedback.FeedbackCommentRow`."""

    id: int
    feedback_id: int
    user_id: int | None
    body: str
    created_at: datetime
    staff: bool = False
    hidden_at: datetime | None = None
    hidden_by: int | None = None
    #: Its one file (0.18, §12).
    attachment_url: str | None = None


def _newest_first[RowT: FeedbackRow](rows: Collection[RowT]) -> list[RowT]:
    return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)


class InMemoryFeedbackRepository:
    """A :class:`~eifi1_server_kit.feedback.FeedbackRepository` of :class:`MemoryFeedbackRow`.

    ``emails`` are the authors' current addresses (the inbox's join); ``hidden(row,
    viewer)`` hides a row from :meth:`get` as Kurvenschmiede's ``visible_to`` or kastlan's
    RLS would; ``demo_authors`` are the demo accounts, whose reports :meth:`count_by_status`
    leaves out (0.15); ``now`` stamps ``created_at`` and ``updated_at``. ``policy`` is the app's,
    whose prefix finds a body's rework lines in :meth:`urls_named_except`; with ``replies``
    (0.13) it reads the entries' files there too, and with ``comments`` (0.18) the
    comments', as an app's query joins them.

    A :class:`~eifi1_server_kit.feedback.PublicFeedbackRepository` too: :meth:`public_page`
    counts in ``votes`` and ``comments`` (the fake stores), as an app's SQL joins its tables.
    """

    def __init__(
        self,
        *,
        emails: Mapping[int, str] | None = None,
        hidden: Callable[[MemoryFeedbackRow, FeedbackActor], bool] | None = None,
        now: Clock = utc_now,
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
        votes: InMemoryVoteStore | None = None,
        comments: InMemoryCommentStore | None = None,
        replies: InMemoryReplyStore | None = None,
        demo_authors: Collection[int] = (),
    ) -> None:
        #: Every row, by id.
        self.rows: dict[int, MemoryFeedbackRow] = {}
        #: Every write, in order, with its row's id.
        self.writes: list[tuple[Literal["add", "apply"], int]] = []
        self.emails = dict(emails or {})
        self.hidden = hidden
        self.now = now
        self.policy = policy
        self.votes = votes
        self.comments = comments
        self.replies = replies
        #: The demo accounts' ids: :meth:`count_by_status` leaves their reports out.
        self.demo_authors = frozenset(demo_authors)

    async def get(self, feedback_id: int, *, viewer: FeedbackActor) -> MemoryFeedbackRow | None:
        row = self.rows.get(feedback_id)
        if row is None or (self.hidden is not None and self.hidden(row, viewer)):
            return None
        return row

    def _of(self, user_id: int) -> list[MemoryFeedbackRow]:
        return _newest_first([row for row in self.rows.values() if row.user_id == user_id])

    async def list_mine(self, user_id: int) -> Sequence[MemoryFeedbackRow]:
        return self._of(user_id)

    async def inbox(self, status: FeedbackStatus | None) -> Sequence[tuple[MemoryFeedbackRow, str | None]]:
        rows = [row for row in self.rows.values() if status is None or row.status == status]
        return [(row, self.emails.get(row.user_id) if row.user_id is not None else None) for row in _newest_first(rows)]

    async def count_by_status(self) -> Mapping[FeedbackStatus, int]:
        """The inbox's rows by status (admin §9.5): crash reports in, ``demo_authors``'
        reports out, an erased author's (``user_id`` None) in. Only the statuses with a
        row, as a GROUP BY answers."""
        counted = Counter(
            FeedbackStatus(row.status)
            for row in self.rows.values()
            if row.user_id is None or row.user_id not in self.demo_authors
        )
        return dict(counted)

    async def open_crash(self, fingerprint: str) -> MemoryFeedbackRow | None:
        rows = [
            row
            for row in self.rows.values()
            if row.crash_fingerprint == fingerprint and FeedbackStatus(row.status) not in TERMINAL_STATUSES
        ]
        return next(iter(_newest_first(rows)), None)

    async def add(self, new: NewFeedback) -> MemoryFeedbackRow:
        at = self.now()
        row = MemoryFeedbackRow(
            id=max(self.rows, default=0) + 1,
            user_id=new.user_id,
            title=new.title,
            body=new.body,
            category=new.category,
            status=new.status,
            context=new.context,
            screenshot_url=new.screenshot_url,
            attachment_urls=new.attachment_urls,
            crash_fingerprint=new.crash_fingerprint,
            created_at=at,
            updated_at=at,
            public=new.public,
        )
        self.rows[row.id] = row
        self.writes.append(("add", row.id))
        return row

    async def apply(
        self, row: MemoryFeedbackRow, changes: Mapping[str, Any], *, actor: FeedbackActor | None
    ) -> MemoryFeedbackRow:
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_at = self.now()
        self.writes.append(("apply", row.id))
        return row

    async def by_author(self, user_id: int) -> Sequence[MemoryFeedbackRow]:
        return self._of(user_id)

    async def urls_named_except(self, row_ids: Collection[int]) -> set[str]:
        urls: set[str] = set()
        for row in self.rows.values():
            if row.id not in row_ids:
                urls |= feedback_attachment_urls(
                    screenshot_url=row.screenshot_url,
                    attachment_urls=row.attachment_urls,
                    body=row.body,
                    policy=self.policy,
                )
        # Since 0.18 (§12) every entry's and comment's file counts, on any report: the
        # erasure has cleared the person's own before it asks.
        if self.replies is not None:
            urls |= {entry.attachment_url for entry in self.replies.rows.values() if entry.attachment_url}
        if self.comments is not None:
            urls |= {comment.attachment_url for comment in self.comments.rows.values() if comment.attachment_url}
        return urls

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
    ) -> Sequence[PublicListEntry[MemoryFeedbackRow]]:
        rows = [
            row
            for row in self.rows.values()
            if is_public(row.category, row.public)
            and (status is None or row.status == status)
            and (category is None or row.category == category)
            and (q is None or q.casefold() in row.title.casefold())
        ]
        ids = [row.id for row in rows]
        tallies = self.votes.tally(ids, viewer_id=viewer_id) if self.votes is not None else {}
        counts = self.comments.tally(ids, include_hidden=False) if self.comments is not None else {}
        entries = [
            PublicListEntry(row, *tallies.get(row.id, (0, False)), comment_count=counts.get(row.id, 0)) for row in rows
        ]
        if sort == "updated":
            entries.sort(key=lambda entry: (entry.row.updated_at, entry.row.id), reverse=True)
        else:
            entries.sort(key=lambda entry: (entry.row.created_at, entry.row.id), reverse=True)
            if sort == "votes":
                entries.sort(key=lambda entry: entry.votes, reverse=True)
        return entries[offset : offset + limit]

    async def get_public(self, feedback_id: int) -> MemoryFeedbackRow | None:
        return self.rows.get(feedback_id)


class InMemoryReplyStore:
    """A :class:`~eifi1_server_kit.feedback.FeedbackReplyStore` of :class:`MemoryReplyRow`.
    ``added`` records each ``add`` with its actor, both kinds; ``notified`` the ones that
    fire ``feedback_answered`` — ``kind = 'reply'`` only (§10.2 decision 10)."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        self.rows: dict[int, MemoryReplyRow] = {}
        self.calls: list[str] = []
        self.added: list[tuple[MemoryReplyRow, FeedbackActor | None]] = []
        self.notified: list[tuple[MemoryReplyRow, FeedbackActor | None]] = []
        self.now = now

    async def for_reports(self, ids: Collection[int]) -> Mapping[int, Sequence[MemoryReplyRow]]:
        self.calls.append("for_reports")
        found: dict[int, list[MemoryReplyRow]] = {}
        for row in sorted(self.rows.values(), key=lambda row: (row.created_at, row.id)):
            if row.feedback_id in ids:
                found.setdefault(row.feedback_id, []).append(row)
        return found

    async def add(self, new: NewFeedbackReply, *, actor: FeedbackActor | None) -> MemoryReplyRow:
        self.calls.append("add")
        row = MemoryReplyRow(
            id=max(self.rows, default=0) + 1,
            feedback_id=new.feedback_id,
            author_id=new.author_id,
            body=new.body,
            status_from=new.status_from,
            status_to=new.status_to,
            created_at=new.created_at or self.now(),
            kind=FeedbackEntryKind(new.kind),
            attachment_url=new.attachment_url,
        )
        self.rows[row.id] = row
        self.added.append((row, actor))
        if row.kind is FeedbackEntryKind.REPLY:
            self.notified.append((row, actor))
        return row

    async def by_author(self, user_id: int) -> Sequence[MemoryReplyRow]:
        self.calls.append("by_author")
        return [row for row in self.rows.values() if row.author_id == user_id]

    async def apply(self, reply: MemoryReplyRow, changes: Mapping[str, Any]) -> MemoryReplyRow:
        self.calls.append("apply")
        for name, value in changes.items():
            setattr(reply, name, value)
        return reply


class InMemoryCommentStore:
    """A :class:`~eifi1_server_kit.feedback.FeedbackCommentStore` of :class:`MemoryCommentRow`.
    ``added`` records each ``add`` with its actor — where an app fires ``feedback_comment``."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        self.rows: dict[int, MemoryCommentRow] = {}
        self.calls: list[str] = []
        self.added: list[tuple[MemoryCommentRow, FeedbackActor]] = []
        self.now = now

    def _ordered(self) -> list[MemoryCommentRow]:
        return sorted(self.rows.values(), key=lambda row: (row.created_at, row.id))

    def tally(self, ids: Collection[int], *, include_hidden: bool) -> dict[int, int]:
        """The counts, without logging a call: :meth:`InMemoryFeedbackRepository.public_page`'s join."""
        counts: dict[int, int] = {}
        for row in self.rows.values():
            if row.feedback_id in ids and (include_hidden or row.hidden_at is None):
                counts[row.feedback_id] = counts.get(row.feedback_id, 0) + 1
        return counts

    async def for_report(self, feedback_id: int) -> Sequence[MemoryCommentRow]:
        self.calls.append("for_report")
        return [row for row in self._ordered() if row.feedback_id == feedback_id]

    async def counts(self, ids: Collection[int], *, include_hidden: bool) -> Mapping[int, int]:
        self.calls.append("counts")
        return self.tally(ids, include_hidden=include_hidden)

    async def commenters(self, feedback_id: int) -> Sequence[int]:
        self.calls.append("commenters")
        out: list[int] = []
        for row in self._ordered():
            if row.feedback_id == feedback_id and row.user_id is not None and row.user_id not in out:
                out.append(row.user_id)
        return out

    async def add(self, new: NewFeedbackComment, *, actor: FeedbackActor) -> MemoryCommentRow:
        self.calls.append("add")
        row = MemoryCommentRow(
            id=max(self.rows, default=0) + 1,
            feedback_id=new.feedback_id,
            user_id=new.user_id,
            body=new.body,
            created_at=self.now(),
            attachment_url=new.attachment_url,
        )
        self.rows[row.id] = row
        self.added.append((row, actor))
        return row

    async def get(self, feedback_id: int, comment_id: int) -> MemoryCommentRow | None:
        self.calls.append("get")
        row = self.rows.get(comment_id)
        return row if row is not None and row.feedback_id == feedback_id else None

    async def apply(
        self, comment: MemoryCommentRow, changes: Mapping[str, Any], *, actor: FeedbackActor | None
    ) -> MemoryCommentRow:
        self.calls.append("apply")
        for name, value in changes.items():
            setattr(comment, name, value)
        return comment

    async def delete(self, comment: MemoryCommentRow) -> None:
        self.calls.append("delete")
        self.rows.pop(comment.id, None)

    async def by_author(self, user_id: int) -> Sequence[MemoryCommentRow]:
        self.calls.append("by_author")
        return [row for row in self._ordered() if row.user_id == user_id]

    async def clear_hidden_by(self, user_id: int) -> int:
        self.calls.append("clear_hidden_by")
        cleared = [row for row in self.rows.values() if row.hidden_by == user_id]
        for row in cleared:
            row.hidden_by = None
        return len(cleared)


class InMemoryVoteStore:
    """A :class:`~eifi1_server_kit.feedback.FeedbackVoteStore`: ``(feedback_id, user_id)`` →
    when it was cast."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        self.rows: dict[tuple[int, int], datetime] = {}
        self.calls: list[str] = []
        self.now = now

    def tally(self, ids: Collection[int], *, viewer_id: int) -> dict[int, tuple[int, bool]]:
        """The counts, without logging a call: :meth:`InMemoryFeedbackRepository.public_page`'s join."""
        counts: dict[int, tuple[int, bool]] = {}
        for feedback_id, user_id in self.rows:
            if feedback_id in ids:
                votes, voted = counts.get(feedback_id, (0, False))
                counts[feedback_id] = (votes + 1, voted or user_id == viewer_id)
        return counts

    async def counts(self, ids: Collection[int], *, viewer_id: int) -> Mapping[int, tuple[int, bool]]:
        self.calls.append("counts")
        return self.tally(ids, viewer_id=viewer_id)

    async def put(self, feedback_id: int, user_id: int) -> None:
        self.calls.append("put")
        self.rows.setdefault((feedback_id, user_id), self.now())

    async def delete(self, feedback_id: int, user_id: int) -> None:
        self.calls.append("delete")
        self.rows.pop((feedback_id, user_id), None)

    async def delete_by_author(self, user_id: int) -> int:
        self.calls.append("delete_by_author")
        mine = [key for key in self.rows if key[1] == user_id]
        for key in mine:
            del self.rows[key]
        return len(mine)


class InMemoryPeopleLookup:
    """A :class:`~eifi1_server_kit.feedback.PeopleLookup` over ``{user_id: (first, last)}``."""

    def __init__(self, names: Mapping[int, tuple[str, str]] | None = None) -> None:
        self.people = dict(names or {})
        self.calls: list[str] = []

    async def names(self, user_ids: Collection[int]) -> Mapping[int, tuple[str, str]]:
        self.calls.append("names")
        return {user_id: self.people[user_id] for user_id in user_ids if user_id in self.people}


class InMemoryAttachmentStorage:
    """An :class:`~eifi1_server_kit.feedback.AttachmentStorage` with keksdose's
    content-addressed keys (``<sha12>.<ext>``). ``uploader_only`` lets only a file's
    uploader read it, as Kurvenschmiede's does for an unfiled upload."""

    def __init__(self, *, uploader_only: bool = False) -> None:
        #: key → (the file, the uploader's id).
        self.files: dict[str, tuple[StoredAttachment, int]] = {}
        self.uploader_only = uploader_only

    async def save(self, data: bytes, upload: CheckedUpload, *, uploader: FeedbackActor, filename: str | None) -> str:
        key = content_addressed_key(data, upload.extension)
        self.files[key] = (StoredAttachment(data=data, media_type=upload.media_type, filename=filename), uploader.id)
        return key

    async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
        found = self.files.get(key)
        if found is None:
            return None
        stored, uploader_id = found
        if self.uploader_only and uploader_id != viewer.id:
            return None
        return stored


@dataclass(slots=True)
class RecordingAlerts:
    """A :class:`~eifi1_server_kit.feedback.FeedbackAlerts` that records each alert with
    its row, in order (feedback §11, 0.16)."""

    alerts: list[tuple[FeedbackAlert, Any]] = dataclasses.field(default_factory=list)

    async def filed(self, alert: FeedbackAlert, row: Any) -> None:
        self.alerts.append((alert, row))

    @property
    def kinds(self) -> list[str]:
        """Each alert's kind, in order."""
        return [alert.kind for alert, _ in self.alerts]


def actor(**overrides: Any) -> FeedbackActor:
    """A :class:`~eifi1_server_kit.feedback.FeedbackActor`: id 1, ``author@example.test``,
    "Ada Author", not an admin, not a demo — each overridable by name."""
    base = FeedbackActor(id=1, email="author@example.test", display_name="Ada Author", is_admin=False)
    return dataclasses.replace(base, **overrides)


def fixed_clock(at: datetime) -> Clock:
    """A :data:`~eifi1_server_kit.feedback.Clock` that always answers ``at``."""

    def now() -> datetime:
        return at

    return now


# --- the files the checks send ---------------------------------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(note: str) -> bytes:
    """A 1 × 1 red PNG that Pillow decodes, carrying ``note`` in a ``tEXt`` chunk."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"tEXt", b"Comment\x00" + note.encode("ascii"))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _pdf(note: str) -> bytes:
    return f"%PDF-1.4\n% {note}\n%%EOF\n".encode("ascii")


def _text(note: str) -> bytes:
    return f"A log line from the conformance check: {note}\n".encode("ascii")


def _check(condition: bool, message: str) -> None:
    """``assert`` that survives ``python -O``."""
    if not condition:
        raise AssertionError(message)


async def _save(storage: AttachmentStorage, data: bytes, declared: str, *, uploader: FeedbackActor, name: str) -> str:
    return await storage.save(data, check_upload(data, declared), uploader=uploader, filename=name)


async def _url(
    storage: AttachmentStorage, policy: AttachmentUrlPolicy, data: bytes, declared: str, *, uploader: FeedbackActor
) -> str:
    key = await _save(storage, data, declared, uploader=uploader, name=f"check.{declared.rpartition('/')[2]}")
    _check(policy.is_key(key), f"save: the key {key!r} is one the policy accepts")
    return policy.url_for(key)


def _status(row: FeedbackRow) -> FeedbackStatus:
    return FeedbackStatus(row.status)


def _same_instant(read: datetime, wanted: datetime) -> bool:
    """A naive read-back is UTC (a ``timestamp without time zone`` column)."""
    return (read if read.tzinfo is not None else read.replace(tzinfo=UTC)) == wanted


def _newest_first_order(rows: Sequence[FeedbackRow]) -> bool:
    keys = [(row.created_at, row.id) for row in rows]
    return keys == sorted(keys, reverse=True)


# --- the conformance checks ------------------------------------------------------------


async def check_repository(
    repository: FeedbackRepository[Any],
    *,
    storage: AttachmentStorage,
    policy: AttachmentUrlPolicy,
    author: FeedbackActor,
    other: FeedbackActor,
    demo: FeedbackActor | None = None,
    replies: FeedbackReplyStore[Any] | None = None,
    comments: FeedbackCommentStore[Any] | None = None,
) -> None:
    """Does the app's repository keep the port's promises (§2.14)? Raises
    :class:`AssertionError` naming the first that fails.

    With ``replies`` and ``comments`` (0.18, §12; the app's stores on the same session) it
    checks ``urls_named_except``'s 0.18 rule as well: an entry's file — a rework entry
    ``author`` writes on their own report — and a comment's — one ``other`` writes there —
    are named although that report is among ``row_ids``. Each file is uploaded by its
    writer first, for a store that claims it (Kurvenschmiede).

    For two existing accounts that are not admins. It files through ``storage`` as
    ``author`` first, since an app's ``add`` may claim only the author's own unfiled
    uploads (Kurvenschmiede). Not checked: ``resolved_at`` (an app may derive it), whom
    ``get`` hides and an app's claim refusal — those are the app's own tests. It asserts
    only on the rows it adds, so it runs on the app's test database as it is.

    ``count_by_status`` (0.15, admin §9.5) is checked by the change each step makes: a
    report counts, a crash report counts, an erased author's report still counts, and with
    ``demo`` (an existing demo account) a demo's report does not.
    """
    token = secrets.token_hex(8)
    counted = await repository.count_by_status()
    picture = await _url(storage, policy, _png(token), "image/png", uploader=author)
    document = await _url(storage, policy, _pdf(token), "application/pdf", uploader=author)

    # 1. Two reports: one naming the picture and the PDF, one naming none.
    def report(title: str, **fields: Any) -> NewFeedback:
        values: dict[str, Any] = {
            "user_id": author.id,
            "title": f"{title} ({token})",
            "body": "Filed by the conformance check.",
            "category": FeedbackCategory.BUG,
            "status": FeedbackStatus.OPEN,
            "context": {"route": "/check"},
        }
        values.update(fields)
        return NewFeedback(**values)

    with_files = await repository.add(report("With files", screenshot_url=picture, attachment_urls=[document]))
    bare = await repository.add(report("Without files"))
    _check(isinstance(with_files.id, int) and isinstance(bare.id, int), "add: the id is set by the flush")
    _check(with_files.id != bare.id, "add: each row has its own id")
    _check(with_files.screenshot_url == picture, "add: screenshot_url reads back")
    _check(list(with_files.attachment_urls or ()) == [document], "add: attachment_urls read back")
    _check(bare.screenshot_url is None, "add: no screenshot reads back as None")
    _check(bare.attachment_urls is None, "add: no files read back as None, never []")
    counted = await _count_grew(repository, counted, 2, "count_by_status: the inbox's rows by status")

    # 2. get: the author's own row; a missing id is None.
    found = await repository.get(with_files.id, viewer=author)
    _check(found is not None and found.id == with_files.id, "get: the author's own row")
    _check(await repository.get(_MISSING_ID, viewer=author) is None, "get: a missing id is None")

    # 3. list_mine: the author's rows only, newest first, the higher id first on a tie.
    mine = list(await repository.list_mine(author.id))
    _check(all(row.user_id == author.id for row in mine), "list_mine: only the author's rows")
    _check(_newest_first_order(mine), "list_mine: newest first, the higher id first on a tie")
    order = [row.id for row in mine]
    _check(
        bare.id in order and with_files.id in order and order.index(bare.id) < order.index(with_files.id),
        "list_mine: holds both rows, the later one first",
    )

    # 4. inbox: each row with its author's address; a status filter holds that status only.
    everything = list(await repository.inbox(None))
    _check(_newest_first_order([row for row, _ in everything]), "inbox: in list_mine's order")
    addresses = {row.id: address for row, address in everything}
    _check(
        addresses.get(with_files.id) == author.email and addresses.get(bare.id) == author.email,
        "inbox: each row paired with its author's address",
    )
    open_rows = list(await repository.inbox(FeedbackStatus.OPEN))
    _check(all(_status(row) is FeedbackStatus.OPEN for row, _ in open_rows), "inbox(status): that status only")
    _check(bare.id in {row.id for row, _ in open_rows}, "inbox(status): holds the rows in that status")

    # 5. apply: an enum goes in, whatever the column stores, and reads back.
    retitled = f"Retitled ({token})"
    await repository.apply(bare, {"status": FeedbackStatus.IN_PROGRESS, "title": retitled}, actor=author)
    again = await repository.get(bare.id, viewer=author)
    _check(
        again is not None and _status(again) is FeedbackStatus.IN_PROGRESS and again.title == retitled,
        "apply: the status and the title read back",
    )

    # 6. open_crash: finds an open crash by its fingerprint, and no longer once it is settled.
    fingerprint = secrets.token_hex(16)
    counted = await repository.count_by_status()
    crash = await repository.add(
        report("[crash] Error: check", category=FeedbackCategory.CRASH, crash_fingerprint=fingerprint)
    )
    await _count_grew(repository, counted, 1, "count_by_status: a crash report counts")
    open_crash = await repository.open_crash(fingerprint)
    _check(open_crash is not None and open_crash.id == crash.id, "open_crash: finds the open row")
    for settled in (FeedbackStatus.DONE, FeedbackStatus.WONT_DO):
        await repository.apply(crash, {"status": settled}, actor=author)
        _check(await repository.open_crash(fingerprint) is None, f"open_crash: not a {settled.value} row")

    # 7. The erasure's queries: another person's body naming one of the author's files.
    await repository.add(
        report(
            "Same picture",
            user_id=other.id,
            body=f"Seen it too.\n{attachment_line(picture)}",
        )
    )
    authors = [with_files.id, bare.id, crash.id]
    named = await repository.urls_named_except(authors)
    _check(picture in named, "urls_named_except: a URL another row's body names")
    _check(document not in named, "urls_named_except: not a URL only the excepted rows name")
    if replies is not None:
        entry_file = await _url(storage, policy, _png(f"{token}-entry"), "image/png", uploader=author)
        await replies.add(
            NewFeedbackReply(
                feedback_id=bare.id,
                author_id=author.id,
                body=f"Still broken ({token})",
                kind=FeedbackEntryKind.REWORK,
                attachment_url=entry_file,
            ),
            actor=author,
        )
        _check(
            entry_file in await repository.urls_named_except(authors),
            "urls_named_except: an entry's file on an excepted report (0.18: every report's entries)",
        )
    if comments is not None:
        comment_file = await _url(storage, policy, _png(f"{token}-comment"), "image/png", uploader=other)
        await comments.add(
            NewFeedbackComment(bare.id, other.id, f"Here too ({token})", attachment_url=comment_file), actor=other
        )
        _check(
            comment_file in await repository.urls_named_except(authors),
            "urls_named_except: a comment's file, on any report (0.18)",
        )
    by_author = {row.id for row in await repository.by_author(author.id)}
    _check(set(authors) <= by_author, "by_author: the author's rows")
    _check(all(row.user_id == author.id for row in await repository.by_author(author.id)), "by_author: only theirs")

    # 8. The erasure's write: no author, no files.
    counted = await repository.count_by_status()
    await repository.apply(with_files, {"user_id": None, "screenshot_url": None, "attachment_urls": None}, actor=None)
    await _count_grew(repository, counted, 0, "count_by_status: an erased author's report counts")
    erased = {row.id: (row, address) for row, address in await repository.inbox(None)}.get(with_files.id)
    _check(erased is not None and erased[1] is None, "apply(actor=None): the inbox pairs an erased row with None")
    _check(
        erased is not None and erased[0].screenshot_url is None and not erased[0].attachment_urls,
        "apply(actor=None): the row names no file",
    )
    _check(
        with_files.id not in {row.id for row in await repository.by_author(author.id)},
        "apply(actor=None): by_author no longer lists it",
    )

    # 9. count_by_status leaves a demo account's report out.
    if demo is not None:
        counted = await repository.count_by_status()
        await repository.add(report("By the demo", user_id=demo.id))
        await _count_grew(repository, counted, 0, "count_by_status: a demo account's report is left out")


async def _count_grew(
    repository: FeedbackRepository[Any], before: Mapping[FeedbackStatus, int], by: int, message: str
) -> Mapping[FeedbackStatus, int]:
    """The OPEN count grew by ``by`` since ``before``; answers the count now."""
    now = await repository.count_by_status()
    status = FeedbackStatus.OPEN
    _check(now.get(status, 0) - before.get(status, 0) == by, message)
    return now


#: Each hex digit to the next one, ``f`` to ``0``.
_NEXT_HEX_DIGIT = str.maketrans("0123456789abcdef", "123456789abcdef0")


def _absent_key(key: str) -> str:
    """``key`` with every hex digit before its extension turned to the next one: the same
    shape, another key."""
    stem, dot, extension = key.rpartition(".")
    if not dot:
        return key.translate(_NEXT_HEX_DIGIT)
    return f"{stem.translate(_NEXT_HEX_DIGIT)}.{extension}"


async def check_storage(storage: AttachmentStorage, *, policy: AttachmentUrlPolicy, uploader: FeedbackActor) -> None:
    """Does the app's storage keep the port's promises (§2.14)? Raises
    :class:`AssertionError` naming the first that fails.

    It saves a picture, a PDF and a text file as ``uploader``, each through
    :func:`~eifi1_server_kit.uploads.check_upload`: each key passes ``policy.is_key``, and
    ``read(key, viewer=uploader)`` answers the same bytes, from which the download's rules
    give the right type. A well-formed key nobody saved — a saved key with the hex digits
    of its stem turned — answers None.
    """
    token = secrets.token_hex(8)
    files = (
        (_png(token), "image/png", "png", "image/png"),
        (_pdf(token), "application/pdf", "pdf", "application/pdf"),
        (_text(token), "text/plain", "txt", "text/plain"),
    )
    keys: list[str] = []
    for data, declared, extension, served in files:
        key = await _save(storage, data, declared, uploader=uploader, name=f"check-{token}.{extension}")
        _check(policy.is_key(key), f"save: the {extension} file's key {key!r} is one the policy accepts")
        stored = await storage.read(key, viewer=uploader)
        if stored is None or stored.data != data:
            raise AssertionError(f"read: the {extension} file's bytes come back")
        _check(
            _served_type(stored, require_utf8_text=False) == served, f"read: the {extension} file serves as {served}"
        )
        keys.append(key)
    absent = _absent_key(keys[0])
    if policy.is_key(absent) and absent not in keys:
        _check(await storage.read(absent, viewer=uploader) is None, "read: a key nobody saved is None")


async def check_round_trip(client: Any, *, policy: AttachmentUrlPolicy, headers: Mapping[str, str]) -> None:
    """Upload a small decodable PNG through the app's router, then download it through the
    URL the upload answered (§2.14). Raises :class:`AssertionError` naming the first
    thing that fails.

    It POSTs the picture as the multipart ``file`` to the mount (``policy.prefix`` without
    its last ``/``): 201, and a ``url`` that ``policy.is_own_url`` accepts — so the prefix
    is the mounted path. Then it GETs that URL: 200, the same bytes, ``image/png``,
    ``Content-Disposition: inline…`` and ``X-Content-Type-Options: nosniff``. ``client`` is
    an ``httpx.AsyncClient`` (``ASGITransport`` over the app) or anything with its
    ``post`` and ``get``; ``headers`` sign the request in as a person who may upload.
    """
    picture = _png(secrets.token_hex(8))
    sent = dict(headers)
    uploaded = await client.post(
        policy.prefix.rstrip("/"), files={"file": ("check.png", picture, "image/png")}, headers=sent
    )
    _check(uploaded.status_code == 201, f"upload: 201, not {uploaded.status_code} {uploaded.text}")
    url = uploaded.json().get("url")
    _check(isinstance(url, str) and policy.is_own_url(url), f"upload: {url!r} is not one of the policy's URLs")
    downloaded = await client.get(url, headers=sent)
    _check(downloaded.status_code == 200, f"download: 200, not {downloaded.status_code}")
    _check(downloaded.content == picture, "download: the same bytes")
    _check(downloaded.headers.get("content-type") == EXTENSION_MEDIA_TYPES["png"], "download: served as image/png")
    _check(
        downloaded.headers.get("content-disposition", "").startswith("inline"), "download: a picture is shown inline"
    )
    _check(downloaded.headers.get("x-content-type-options") == "nosniff", "download: nosniff")


# --- public feedback's conformance checks (0.11, feedback §9.14) ------------------------


async def check_reply_store(
    store: FeedbackReplyStore[Any],
    *,
    feedback_id: int,
    admin: FeedbackActor,
    policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
    attachment_url: str | None = None,
) -> None:
    """Does the app's reply store keep the port's promises (§9.7, §10.3)? Raises
    :class:`AssertionError` naming the first that fails. ``feedback_id`` is an existing
    report; it asserts only on the entries it adds: two replies, then a rework entry with
    a file and one with an explicit ``created_at`` (a migrated block, oldest of all). It
    cannot see a notification: that ``add`` fires ``feedback_answered`` for a reply only
    is the app's own test.

    The rework's file is ``attachment_url`` where given — a file the app's test stored, for
    a store that resolves it (Kurvenschmiede claims it as an upload) — else one under
    ``policy``'s prefix (0.13.1: the app's own policy, not the kit's default)."""
    token = secrets.token_hex(8)
    first = await store.add(
        NewFeedbackReply(
            feedback_id=feedback_id,
            author_id=admin.id,
            body=f"First answer ({token})",
            status_from=FeedbackStatus.OPEN,
            status_to=FeedbackStatus.IN_EVALUATION,
        ),
        actor=admin,
    )
    second = await store.add(
        NewFeedbackReply(feedback_id=feedback_id, author_id=None, body=f"A loop's answer ({token})"), actor=None
    )
    _check(isinstance(first.id, int) and first.id != second.id, "add: each reply has its own id, set by the flush")
    _check(isinstance(first.created_at, datetime), "add: created_at is set (server_default now())")
    _check(
        FeedbackStatus(first.status_from or "") is FeedbackStatus.OPEN
        and FeedbackStatus(first.status_to or "") is FeedbackStatus.IN_EVALUATION,
        "add: status_from and status_to read back",
    )
    _check(second.status_from is None and second.status_to is None, "add: no status change reads back as None")
    found = (await store.for_reports([feedback_id, _MISSING_ID])).get(feedback_id, ())
    ours = [reply.id for reply in found if reply.id in {first.id, second.id}]
    _check(ours == [first.id, second.id], "for_reports: the report's replies, oldest first")
    _check(not (await store.for_reports([_MISSING_ID])).get(_MISSING_ID), "for_reports: a report with none has none")
    by_admin = {reply.id for reply in await store.by_author(admin.id)}
    _check(first.id in by_admin and second.id not in by_admin, "by_author: the replies that person wrote")
    for reply in (first, second):
        _check(FeedbackEntryKind(reply.kind) is FeedbackEntryKind.REPLY, "add: kind defaults to reply")
        _check(reply.attachment_url is None, "add: a reply has no file")
    url = attachment_url or f"{policy.prefix}check{token}.png"
    rework = await store.add(
        NewFeedbackReply(
            feedback_id=feedback_id,
            author_id=admin.id,
            body=f"Still broken ({token})",
            status_from=FeedbackStatus.DONE,
            status_to=FeedbackStatus.REWORK,
            kind=FeedbackEntryKind.REWORK,
            attachment_url=url,
        ),
        actor=admin,
    )
    stamped_at = datetime(2001, 2, 3, 4, 5, tzinfo=UTC)
    migrated = await store.add(
        NewFeedbackReply(
            feedback_id=feedback_id,
            author_id=None,
            body=f"A migrated rework ({token})",
            kind=FeedbackEntryKind.REWORK,
            created_at=stamped_at,
        ),
        actor=None,
    )
    _check(
        FeedbackEntryKind(rework.kind) is FeedbackEntryKind.REWORK and rework.attachment_url == url,
        "add: kind and attachment_url read back",
    )
    _check(FeedbackStatus(rework.status_to or "") is FeedbackStatus.REWORK, "add: status_to REWORK reads back")
    _check(_same_instant(migrated.created_at, stamped_at), "add: an explicit created_at is written as given")
    entries = [
        entry.id
        for entry in (await store.for_reports([feedback_id])).get(feedback_id, ())
        if entry.id in {first.id, second.id, rework.id, migrated.id}
    ]
    _check(entries == [migrated.id, first.id, second.id, rework.id], "for_reports: both kinds, oldest first")
    by_admin = {reply.id for reply in await store.by_author(admin.id)}
    _check(rework.id in by_admin, "by_author: both kinds")
    await store.apply(first, {"author_id": None, "body": "[erased]"})
    again = [reply for reply in (await store.for_reports([feedback_id]))[feedback_id] if reply.id == first.id]
    _check(
        len(again) == 1 and again[0].author_id is None and again[0].body == "[erased]",
        "apply: author_id and body read back",
    )
    await store.apply(rework, {"author_id": None, "attachment_url": None})
    cleared = [entry for entry in (await store.for_reports([feedback_id]))[feedback_id] if entry.id == rework.id]
    _check(len(cleared) == 1 and cleared[0].attachment_url is None, "apply: attachment_url reads back")


async def check_comment_store(
    store: FeedbackCommentStore[Any],
    *,
    feedback_id: int,
    author: FeedbackActor,
    other: FeedbackActor,
    admin: FeedbackActor,
    policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
    attachment_url: str | None = None,
) -> None:
    """Does the app's comment store keep the port's promises (§9.8)? Raises
    :class:`AssertionError` naming the first that fails. ``feedback_id`` is an existing
    report WITHOUT comments; ``clear_hidden_by(admin.id)`` clears that account's hides on
    the whole test database.

    Since 0.18 (§12) the first comment carries a file: ``attachment_url`` where given — one
    ``author`` uploaded in the app's test, for a store that claims it (Kurvenschmiede) —
    else one under ``policy``'s prefix; it must read back, and the erasure's ``apply``
    must clear it."""
    token = secrets.token_hex(8)
    url = attachment_url or f"{policy.prefix}check{token}.png"
    first = await store.add(
        NewFeedbackComment(feedback_id, author.id, f"Same here ({token})", attachment_url=url), actor=author
    )
    second = await store.add(NewFeedbackComment(feedback_id, other.id, f"And here ({token})"), actor=other)
    third = await store.add(NewFeedbackComment(feedback_id, author.id, f"Still ({token})"), actor=author)
    _check(len({first.id, second.id, third.id}) == 3, "add: each comment has its own id, set by the flush")
    _check(not first.staff and first.hidden_at is None and first.hidden_by is None, "add: not staff, not hidden")
    _check(first.attachment_url == url and second.attachment_url is None, "add: attachment_url reads back")
    order = [comment.id for comment in await store.for_report(feedback_id)]
    _check(order == [first.id, second.id, third.id], "for_report: the report's comments, oldest first")
    found = await store.get(feedback_id, second.id)
    _check(found is not None and found.id == second.id, "get: the comment")
    _check(await store.get(_MISSING_ID, second.id) is None, "get: not under another report")
    _check(await store.get(feedback_id, _MISSING_ID) is None, "get: a missing id is None")
    _check(
        list(await store.commenters(feedback_id)) == [author.id, other.id], "commenters: each once, first comment first"
    )
    await store.apply(second, {"hidden_at": utc_now(), "hidden_by": admin.id}, actor=admin)
    _check((await store.counts([feedback_id], include_hidden=True)).get(feedback_id) == 3, "counts: hidden included")
    _check((await store.counts([feedback_id], include_hidden=False)).get(feedback_id) == 2, "counts: visible only")
    _check(await store.clear_hidden_by(admin.id) >= 1, "clear_hidden_by: how many rows")
    cleared = await store.get(feedback_id, second.id)
    _check(
        cleared is not None and cleared.hidden_by is None and cleared.hidden_at is not None,
        "clear_hidden_by: hidden_by NULL, the comment still hidden",
    )
    mine = {comment.id for comment in await store.by_author(author.id)}
    _check({first.id, third.id} <= mine and second.id not in mine, "by_author: the comments that person wrote")
    await store.apply(first, {"user_id": None, "attachment_url": None}, actor=None)
    erased = await store.get(feedback_id, first.id)
    _check(erased is not None and erased.attachment_url is None, "apply: attachment_url reads back")
    await store.delete(third)
    _check(await store.get(feedback_id, third.id) is None, "delete: gone")


async def check_vote_store(
    store: FeedbackVoteStore, *, feedback_id: int, voter: FeedbackActor, other: FeedbackActor
) -> None:
    """Does the app's vote store keep the port's promises (§9.6)? Raises
    :class:`AssertionError` naming the first that fails. ``feedback_id`` is an existing
    report neither person has voted on; ``delete_by_author(other.id)`` deletes every vote
    ``other`` cast on the test database."""

    async def tally(person: FeedbackActor) -> tuple[int, bool]:
        return (await store.counts([feedback_id, _MISSING_ID], viewer_id=person.id)).get(feedback_id, (0, False))

    before, voted = await tally(voter)
    _check(not voted and not (await tally(other))[1], "a report neither person has voted on")
    await store.put(feedback_id, voter.id)
    await store.put(feedback_id, voter.id)
    _check(await tally(voter) == (before + 1, True), "put: twice is one vote, and the viewer's")
    await store.put(feedback_id, other.id)
    _check(await tally(voter) == (before + 2, True), "counts: every vote")
    await store.delete(feedback_id, voter.id)
    await store.delete(feedback_id, voter.id)
    _check(await tally(voter) == (before + 1, False), "delete: gone, and none to delete is no error")
    _check(await tally(other) == (before + 1, True), "counts: voted is the viewer's")
    _check(await store.delete_by_author(other.id) >= 1, "delete_by_author: how many")
    _check(await tally(other) == (before, False), "delete_by_author: that person's votes are gone")
    _check(
        (await store.counts([_MISSING_ID], viewer_id=voter.id)).get(_MISSING_ID, (0, False)) == (0, False),
        "counts: a report without votes",
    )


async def _whole_public_list(
    repository: PublicFeedbackRepository[Any], *, viewer_id: int, sort: PublicSort, q: str | None, **filters: Any
) -> list[PublicListEntry[Any]]:
    entries: list[PublicListEntry[Any]] = []
    offset, previous = 0, [-1]
    while True:
        found = list(
            await repository.public_page(
                viewer_id=viewer_id,
                sort=sort,
                status=filters.get("status"),
                category=filters.get("category"),
                q=q,
                limit=PUBLIC_PAGE_MAX,
                offset=offset,
            )
        )
        ids = [entry.row.id for entry in found]
        _check(ids != previous, "public_page: the offset moves on to the next page")
        entries += found
        if len(found) < PUBLIC_PAGE_MAX:
            return entries
        previous, offset = ids, offset + PUBLIC_PAGE_MAX


async def check_public_repository(
    repository: Any,
    *,
    votes: FeedbackVoteStore,
    author: FeedbackActor,
    other: FeedbackActor,
    search: bool = True,
) -> None:
    """Does the app's repository keep :class:`~eifi1_server_kit.feedback.PublicFeedbackRepository`'s
    promises (§9.9)? Raises :class:`AssertionError` naming the first that fails.

    ``repository`` is the app's :class:`~eifi1_server_kit.feedback.FeedbackRepository`
    that implements the public reads; ``votes`` its vote store. It files four reports as
    ``author`` — two public, one private, one CRASH — has ``other`` vote on one, and reads
    them back through ``public_page`` (with ``q``, or page by page where ``search`` is
    False — titles sealed) and ``get_public``. It asserts only on the rows it adds.
    """
    token = secrets.token_hex(8)

    def report(title: str, **fields: Any) -> NewFeedback:
        values: dict[str, Any] = {
            "user_id": author.id,
            "title": f"{title} ({token})",
            "body": "Filed by the public conformance check.",
            "category": FeedbackCategory.BUG,
            "status": FeedbackStatus.OPEN,
            "context": {"route": "/check"},
        }
        values.update(fields)
        return NewFeedback(**values)

    wanted = await repository.add(report("Wanted"))
    private = await repository.add(report("Private", public=False))
    crash = await repository.add(
        report("[crash] Error: check", category=FeedbackCategory.CRASH, crash_fingerprint=secrets.token_hex(16))
    )
    newer = await repository.add(report("Newer"))
    await votes.put(wanted.id, other.id)
    await votes.put(wanted.id, author.id)
    _check(bool(wanted.public) and not private.public, "add: public reads back")

    found = await repository.get_public(private.id)
    _check(found is not None and found.id == private.id, "get_public: the row by id, whatever its flag")
    _check(await repository.get_public(_MISSING_ID) is None, "get_public: a missing id is None")

    ours = {wanted.id, private.id, crash.id, newer.id}

    async def listed(sort: PublicSort, **filters: Any) -> list[PublicListEntry[Any]]:
        entries = await _whole_public_list(
            repository, viewer_id=other.id, sort=sort, q=token if search else None, **filters
        )
        return [entry for entry in entries if entry.row.id in ours]

    newest = await listed("newest")
    _check([entry.row.id for entry in newest] == [newer.id, wanted.id], "public_page: public rows only, newest first")
    by_id = {entry.row.id: entry for entry in newest}
    _check(
        (by_id[wanted.id].votes, by_id[wanted.id].voted, by_id[wanted.id].comment_count) == (2, True, 0),
        "public_page: the counts, voted the viewer's",
    )
    _check((by_id[newer.id].votes, by_id[newer.id].voted) == (0, False), "public_page: a row without votes")
    most = await listed("votes")
    _check([entry.row.id for entry in most] == [wanted.id, newer.id], "public_page(sort=votes): most wanted first")
    _check(len(await listed("updated")) == 2, "public_page(sort=updated): the same rows")
    _check(len(await listed("newest", status=FeedbackStatus.OPEN)) == 2, "public_page(status): that status")
    _check(not await listed("newest", status=FeedbackStatus.DONE), "public_page(status): only that status")
    _check(not await listed("newest", category=FeedbackCategory.IDEA), "public_page(category): only that category")


def _public_kind(keys: Collection[str]) -> tuple[str, frozenset[str]]:
    if "staff" in keys:
        return "comment", PUBLIC_COMMENT_FIELDS
    if "status_to" in keys and "title" not in keys:
        return "reply", PUBLIC_REPLY_FIELDS
    return "report", PUBLIC_REPORT_FIELDS


def assert_public_view(payload: Any, *, policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY) -> None:
    """Does a public answer say only what §9.10 lets it? Raises :class:`AssertionError`.

    ``payload`` is the JSON (or a Pydantic model) of a public list, detail, comment list,
    comment or vote. Each object is read as a report, a reply (``status_to``, no
    ``title``) or a comment (``staff``) and may carry only that list's keys — never
    ``user_id``, ``user_email``, ``context``, ``public``, ``filed_private``,
    ``status_from``, a file column — and no string anywhere may hold ``policy.prefix``:
    no attachment URL or key, but for a comment's own ``attachment_url`` (0.18, §12),
    which must be one of ``policy``'s URLs or null. A CRASH is never in it.
    """
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    if isinstance(payload, list):
        for item in payload:
            assert_public_view(item, policy=policy)
        return
    _check(isinstance(payload, Mapping), f"a public answer is an object or a list, not {type(payload).__name__}")
    kind, allowed = _public_kind(payload)
    extra = sorted(set(payload) - allowed)
    _check(not extra, f"a public {kind} carries {extra}")
    _check(payload.get("category") != FeedbackCategory.CRASH.value, "a CRASH report is never public")
    for key, value in payload.items():
        if kind == "comment" and key == "attachment_url":
            # A comment's own file (0.18, §12): this app's URL, or nothing.
            _check(value is None or policy.is_own_url(value), f"a public {kind}'s {key} names an attachment URL")
        elif isinstance(value, str):
            _check(policy.prefix not in value, f"a public {kind}'s {key} names an attachment URL")
        elif isinstance(value, list):
            for item in value:
                assert_public_view(item, policy=policy)
