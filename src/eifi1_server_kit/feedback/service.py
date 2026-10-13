"""The feedback service: the contract's flows, written once against the ports.

Layer 2 (``docs/server-services-harmonization.md`` §2, §4.1 in ``Eifi1/ui-kit``). What the
three apps each wrote around the layer-1 rules — the lookups, the demo refusal, the
limiters, reading an upload, the download's type and headers, the 404 or 403, the
erasure's loop — as one service over the app's :class:`~eifi1_server_kit.feedback.FeedbackRepository`
and :class:`~eifi1_server_kit.feedback.AttachmentStorage`. No FastAPI: a caller without
a router (Kurvenschmiede's ``feedback_loop.py``) uses it with the app's repository::

    service = FeedbackService(repository, policy=ATTACHMENT_URLS, foreign_row="not_found")
    row = await service.create(first_admin, payload)

**The kit never commits** (§2.7). Each flow makes at most one repository write (``add``
or ``apply``), and it is the flow's last database step; who commits is the app's —
keksdose's ``commit_boundary``, kastlan's adapter flag, Kurvenschmiede's session.

**The limiter is charged before the work** (§2.11; feedback §3.5 and §3.6 as amended in
round 0.36): an upload before it is read, a crash before the lookup. A refused file uses
a slot; a crash's repeats count.

**The kit deletes no file** (§2.15): :meth:`FeedbackService.erase_author` answers the keys
no other report names, and each app deletes them its own way, at its own commit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.feedback.attachments import AttachmentUrlPolicy
from eifi1_server_kit.feedback.context import stamp_identity
from eifi1_server_kit.feedback.crash import (
    CrashDecision,
    crash_body,
    crash_context,
    crash_fingerprint,
    crash_reference,
    crash_seen_at,
    crash_title,
    decide_crash,
    fold_crash_context,
)
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackStatus
from eifi1_server_kit.feedback.erasure import anonymise_feedback, attachment_keys_to_purge
from eifi1_server_kit.feedback.errors import (
    AttachmentNotFoundError,
    FeedbackForbiddenError,
    FeedbackNotFoundError,
    FeedbackThrottledError,
)
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
from eifi1_server_kit.feedback.rules import initial_status, plan_update, reject_manual_crash
from eifi1_server_kit.feedback.schemas import CrashReportCreate, CrashReportResponse, FeedbackCreate, FeedbackUpdate
from eifi1_server_kit.limiter import SlidingWindowRateLimiter
from eifi1_server_kit.uploads import (
    EXTENSION_MEDIA_TYPES,
    UploadSource,
    bare_media_type,
    check_upload,
    content_disposition,
    ensure_decodable_image,
    inline_or_attachment,
    is_plain_text,
    read_capped_upload,
    signature_type,
)

__all__ = [
    "AttachmentDownload",
    "ErasedFeedback",
    "FeedbackService",
    "download_attachment",
    "throttle_upload",
    "upload_attachment",
]

#: What a file is served as when its bytes name nothing the policy accepts.
_UNKNOWN_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class ErasedFeedback:
    """What :meth:`FeedbackService.erase_author` did (§2.15)."""

    #: Rows anonymised.
    reports: int
    #: Bare policy keys no other row names, sorted; the app deletes them at its commit.
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttachmentDownload:
    """One file, ready to answer (§2.12): the bytes, the type they give and the headers."""

    data: bytes
    media_type: str
    #: ``Content-Disposition`` and ``X-Content-Type-Options``.
    headers: dict[str, str]


class FeedbackService[RowT: FeedbackRow]:
    """The contract's flows over the app's repository (§2.4, §2.10, §2.15).

    ``policy`` is the app's attachment URL shape; ``foreign_row`` what a non-admin gets on
    another person's row — ``"forbidden"`` lets :func:`~eifi1_server_kit.feedback.plan_update`'s
    403 speak (keksdose, kastlan), ``"not_found"`` answers 404 before it (Kurvenschmiede);
    ``now`` stamps ``resolved_at`` and a crash's ``seen_at``.

    The demo refusal and the inbox's admin check are here too, for a caller without the
    router (§2.8); over HTTP the app's own guards answer first.
    """

    def __init__(
        self,
        repository: FeedbackRepository[RowT],
        *,
        policy: AttachmentUrlPolicy,
        foreign_row: Literal["forbidden", "not_found"] = "forbidden",
        now: Clock = utc_now,
    ) -> None:
        self.repository = repository
        self.policy = policy
        self.foreign_row = foreign_row
        self.now = now

    async def create(self, actor: FeedbackActor, payload: FeedbackCreate) -> RowT:
        """File a report (§3.3): refused for a demo, CRASH refused (422), the first status
        the server's (:func:`~eifi1_server_kit.feedback.initial_status`), the identity in
        ``context`` the session's (:func:`~eifi1_server_kit.feedback.stamp_identity`, §2.13),
        ``attachment_urls`` ``None`` for none."""
        refuse_demo(actor.is_demo, "feedback")
        reject_manual_crash(payload.category)
        return await self.repository.add(
            NewFeedback(
                user_id=actor.id,
                title=payload.title,
                body=payload.body,
                category=payload.category,
                status=initial_status(author_is_admin=actor.is_admin),
                context=stamp_identity(
                    payload.context,
                    user_id=actor.id,
                    user_email=actor.email,
                    user_display_name=actor.display_name,
                ),
                screenshot_url=payload.screenshot_url,
                attachment_urls=list(payload.attachment_urls) or None,
            )
        )

    async def report_crash(
        self,
        actor: FeedbackActor,
        payload: CrashReportCreate,
        *,
        limiter: SlidingWindowRateLimiter | None = None,
    ) -> CrashReportResponse:
        """File a crash, or fold it into the open row with its fingerprint (§3.6).

        ``stored: false`` for a demo, then for a spent window — charged before the lookup,
        so repeats count (§2.11) — and never an error status: the caller is a page that
        has just crashed.
        """
        if actor.is_demo:
            return CrashReportResponse(stored=False)
        if limiter is not None and limiter.hit(str(actor.id)) is not None:
            return CrashReportResponse(stored=False)
        fingerprint = crash_fingerprint(payload, actor.id)
        seen_at = crash_seen_at(payload, self.now())
        found = await self.repository.open_crash(fingerprint)
        if found is not None and decide_crash(found.status) is CrashDecision.FOLD:
            row = await self.repository.apply(
                found, {"context": fold_crash_context(found.context, seen_at)}, actor=actor
            )
            duplicate = True
        else:
            row = await self.repository.add(
                NewFeedback(
                    user_id=actor.id,
                    title=crash_title(payload),
                    body=crash_body(
                        payload, user_id=actor.id, user_email=actor.email, fingerprint=fingerprint, seen_at=seen_at
                    ),
                    category=FeedbackCategory.CRASH,
                    status=initial_status(author_is_admin=actor.is_admin, crash=True),
                    context=crash_context(
                        payload,
                        user_id=actor.id,
                        user_email=actor.email,
                        user_display_name=actor.display_name,
                        fingerprint=fingerprint,
                        seen_at=seen_at,
                    ),
                    crash_fingerprint=fingerprint,
                )
            )
            duplicate = False
        return CrashReportResponse(
            stored=True, duplicate=duplicate, feedback_id=row.id, reference=crash_reference(fingerprint)
        )

    async def list_mine(self, actor: FeedbackActor) -> Sequence[RowT]:
        """The actor's own reports, newest first — an admin's too."""
        return await self.repository.list_mine(actor.id)

    async def inbox(
        self, actor: FeedbackActor, status: FeedbackStatus | None = None
    ) -> Sequence[tuple[RowT, str | None]]:
        """Every report with its author's address, for an admin and nobody else
        (:class:`~eifi1_server_kit.feedback.FeedbackForbiddenError` "Insufficient role")."""
        if not actor.is_admin:
            raise FeedbackForbiddenError("Insufficient role")
        return await self.repository.inbox(status)

    async def update(self, actor: FeedbackActor, feedback_id: int, payload: FeedbackUpdate) -> RowT:
        """A PATCH, in keksdose's order (§2.10): refused for a demo; CRASH refused before
        the lookup (422); the row this actor may see, or 404; a foreign row's 404 where
        the app chose it; :func:`~eifi1_server_kit.feedback.plan_update` (403 or 422); then
        the one write."""
        refuse_demo(actor.is_demo, "feedback")
        reject_manual_crash(payload.category)
        row = await self.repository.get(feedback_id, viewer=actor)
        if row is None:
            raise FeedbackNotFoundError()
        is_author = row.user_id == actor.id
        if self.foreign_row == "not_found" and not actor.is_admin and not is_author:
            raise FeedbackNotFoundError()
        plan = plan_update(
            status=row.status,
            body=row.body,
            changes=payload.model_dump(exclude_unset=True),
            is_admin=actor.is_admin,
            is_author=is_author,
            now=self.now(),
        )
        return await self.repository.apply(row, plan.changes, actor=actor)

    async def erase_author(self, user_id: int, identifiers: Iterable[str | None]) -> ErasedFeedback:
        """Take an erased account out of its reports, and answer the files to delete (§2.15).

        Each of the author's rows is anonymised
        (:func:`~eifi1_server_kit.feedback.anonymise_feedback`) and written with
        ``actor=None``: an erasure claims nothing and answers nobody. ``keys`` are the bare
        keys of the URLs those rows named — columns and the body's rework lines — that no
        other row still names, sorted. Call it inside the app's own erasure transaction,
        with a repository that does not commit; the app deletes the keys at its commit
        (keksdose prefixes ``feedback/`` and arms its after-commit purge, kastlan deletes
        after its one commit, Kurvenschmiede ignores them).
        """
        needles = list(identifiers)
        rows = await self.repository.by_author(user_id)
        erased_ids: list[int] = []
        erased_urls: set[str] = set()
        for row in rows:
            result = anonymise_feedback(
                title=row.title,
                body=row.body,
                context=row.context,
                screenshot_url=row.screenshot_url,
                attachment_urls=row.attachment_urls,
                identifiers=needles,
                policy=self.policy,
            )
            erased_ids.append(row.id)
            erased_urls |= result.urls
            await self.repository.apply(row, result.changes, actor=None)
        keys: tuple[str, ...] = ()
        if erased_urls:
            others = await self.repository.urls_named_except(erased_ids)
            keys = tuple(attachment_keys_to_purge(erased_urls, others, policy=self.policy))
        return ErasedFeedback(reports=len(erased_ids), keys=keys)


def throttle_upload(limiter: SlidingWindowRateLimiter, user_id: int) -> None:
    """Charge one upload to ``user_id``'s window, or refuse with
    :class:`~eifi1_server_kit.feedback.FeedbackThrottledError` (429, ``Retry-After``).

    keksdose's support door calls it too, on the same ``.upload`` window, so the 429's
    sentence is written once."""
    wait = limiter.hit(str(user_id))
    if wait is not None:
        raise FeedbackThrottledError(wait)


async def upload_attachment(
    actor: FeedbackActor,
    file: UploadSource,
    *,
    declared_type: str | None,
    filename: str | None,
    storage: AttachmentStorage,
    policy: AttachmentUrlPolicy,
    limiter: SlidingWindowRateLimiter | None,
    decode_images: bool = False,
    require_utf8_text: bool = False,
) -> str:
    """Store one file for a report not filed yet, and answer the URL to name it by (§3.5).

    In order: refused for a demo; the window charged (before the file is read, §2.11);
    :func:`~eifi1_server_kit.uploads.read_capped_upload`;
    :func:`~eifi1_server_kit.uploads.check_upload` (400 / 413 / 415);
    :func:`~eifi1_server_kit.uploads.ensure_decodable_image` with ``decode_images`` (the
    ``images`` extra; keksdose, kastlan); ``storage.save`` mints the key, and the answer
    is ``policy.url_for(key)``.
    """
    refuse_demo(actor.is_demo, "uploads")
    if limiter is not None:
        throttle_upload(limiter, actor.id)
    data = await read_capped_upload(file)
    checked = check_upload(data, declared_type, require_utf8_text=require_utf8_text)
    if decode_images:
        ensure_decodable_image(data, checked.media_type)
    key = await storage.save(data, checked, uploader=actor, filename=filename)
    return policy.url_for(key)


def _served_type(stored: StoredAttachment, *, require_utf8_text: bool) -> str | None:
    """What the bytes say they are (§2.12): their signature, else ``text/plain`` when
    stored as text (the bare type) and still text, else ``None``."""
    found = signature_type(stored.data)
    if found is not None:
        return found
    if bare_media_type(stored.media_type) == "text/plain" and is_plain_text(
        stored.data, require_utf8_text=require_utf8_text
    ):
        return "text/plain"
    return None


def _fallback_name(key: str) -> str:
    """``attachment.<the key's extension>``, or ``attachment`` for a key without one."""
    stem, dot, extension = key.rpartition(".")
    return f"attachment.{extension}" if dot and stem and extension else "attachment"


async def download_attachment(
    actor: FeedbackActor,
    key: str,
    *,
    storage: AttachmentStorage,
    policy: AttachmentUrlPolicy,
    require_utf8_text: bool = False,
) -> AttachmentDownload:
    """One stored file, with Kurvenschmiede's download rules for all three (§2.12).

    404 ("Attachment not found") for a key outside the policy's pattern — the only thing
    between a caller and the store path — and for a file the store does not have or will
    not show this viewer. The type is read off the BYTES, never the stored word: their
    signature, else ``text/plain; charset=utf-8`` when stored as text and still text,
    else ``application/octet-stream``. ``Content-Disposition`` always — ``inline`` for
    the four pictures only — with the stored name or ``attachment.<ext>``, and
    ``X-Content-Type-Options: nosniff`` always.
    """
    if not policy.is_key(key):
        raise AttachmentNotFoundError()
    stored = await storage.read(key, viewer=actor)
    if stored is None:
        raise AttachmentNotFoundError()
    kind = _served_type(stored, require_utf8_text=require_utf8_text)
    media_type = EXTENSION_MEDIA_TYPES["txt"] if kind == "text/plain" else kind or _UNKNOWN_MEDIA_TYPE
    headers = {
        "Content-Disposition": content_disposition(stored.filename or _fallback_name(key), inline_or_attachment(kind)),
        "X-Content-Type-Options": "nosniff",
    }
    return AttachmentDownload(data=stored.data, media_type=media_type, headers=headers)
