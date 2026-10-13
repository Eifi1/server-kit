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

Synthetic data only: every file is made here, with a fresh token in it, so two runs on
the same database never share a content-addressed key.
"""

from __future__ import annotations

import dataclasses
import secrets
import struct
import zlib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.body import attachment_line
from eifi1_server_kit.feedback.enums import TERMINAL_STATUSES, FeedbackCategory, FeedbackStatus
from eifi1_server_kit.feedback.erasure import feedback_attachment_urls
from eifi1_server_kit.feedback.ports import (
    AttachmentStorage,
    Clock,
    FeedbackActor,
    FeedbackRepository,
    FeedbackRow,
    NewFeedback,
    StoredAttachment,
    utc_now,
)
from eifi1_server_kit.feedback.service import _served_type
from eifi1_server_kit.uploads import EXTENSION_MEDIA_TYPES, CheckedUpload, check_upload, content_addressed_key

__all__ = [
    "InMemoryAttachmentStorage",
    "InMemoryFeedbackRepository",
    "MemoryFeedbackRow",
    "actor",
    "check_repository",
    "check_round_trip",
    "check_storage",
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


def _newest_first[RowT: FeedbackRow](rows: Collection[RowT]) -> list[RowT]:
    return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)


class InMemoryFeedbackRepository:
    """A :class:`~eifi1_server_kit.feedback.FeedbackRepository` of :class:`MemoryFeedbackRow`.

    ``emails`` are the authors' current addresses (the inbox's join); ``hidden(row,
    viewer)`` hides a row from :meth:`get` as Kurvenschmiede's ``visible_to`` or kastlan's
    RLS would; ``now`` stamps ``created_at`` and ``updated_at``. ``policy`` is the app's,
    whose prefix finds a body's rework lines in :meth:`urls_named_except`.
    """

    def __init__(
        self,
        *,
        emails: Mapping[int, str] | None = None,
        hidden: Callable[[MemoryFeedbackRow, FeedbackActor], bool] | None = None,
        now: Clock = utc_now,
        policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
    ) -> None:
        #: Every row, by id.
        self.rows: dict[int, MemoryFeedbackRow] = {}
        #: Every write, in order, with its row's id.
        self.writes: list[tuple[Literal["add", "apply"], int]] = []
        self.emails = dict(emails or {})
        self.hidden = hidden
        self.now = now
        self.policy = policy

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
        return urls


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
) -> None:
    """Does the app's repository keep the port's promises (§2.14)? Raises
    :class:`AssertionError` naming the first that fails.

    For two existing accounts that are not admins. It files through ``storage`` as
    ``author`` first, since an app's ``add`` may claim only the author's own unfiled
    uploads (Kurvenschmiede). Not checked: ``resolved_at`` (an app may derive it), whom
    ``get`` hides and an app's claim refusal — those are the app's own tests. It asserts
    only on the rows it adds, so it runs on the app's test database as it is.
    """
    token = secrets.token_hex(8)
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
    crash = await repository.add(
        report("[crash] Error: check", category=FeedbackCategory.CRASH, crash_fingerprint=fingerprint)
    )
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
    by_author = {row.id for row in await repository.by_author(author.id)}
    _check(set(authors) <= by_author, "by_author: the author's rows")
    _check(all(row.user_id == author.id for row in await repository.by_author(author.id)), "by_author: only theirs")

    # 8. The erasure's write: no author, no files.
    await repository.apply(with_files, {"user_id": None, "screenshot_url": None, "attachment_urls": None}, actor=None)
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
