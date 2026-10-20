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

**Public feedback** (0.11, ``docs/feedback-harmonization.md`` §9): the reply, comment and
vote flows, the public list and detail, and the views that fill a page's names, counts
and replies with one call per store. The one-write rule becomes **one write per store,
the report last** (a reply with a status adds the reply, then applies the row; a create
adds the author's vote after the report, which it needs the id of).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from eifi1_server_kit.auth.accounts import first_name_of, full_name
from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.feedback.alerts import FeedbackAlert, FeedbackAlertKind, FeedbackAlerts
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
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackEntryKind, FeedbackStatus
from eifi1_server_kit.feedback.erasure import (
    anonymise_feedback,
    anonymise_reply,
    attachment_keys_to_purge,
    scrub_text,
)
from eifi1_server_kit.feedback.errors import (
    COMMENTS_THROTTLED_DETAIL,
    THROTTLED_DETAIL,
    VOTES_THROTTLED_DETAIL,
    AttachmentNotFoundError,
    FeedbackConflictError,
    FeedbackForbiddenError,
    FeedbackNotFoundError,
    FeedbackThrottledError,
    FeedbackValidationError,
)
from eifi1_server_kit.feedback.ports import (
    AttachmentStorage,
    Clock,
    FeedbackActor,
    FeedbackCommentRow,
    FeedbackCommentStore,
    FeedbackReplyRow,
    FeedbackReplyStore,
    FeedbackRepository,
    FeedbackRow,
    FeedbackVoteStore,
    NewFeedback,
    NewFeedbackComment,
    NewFeedbackReply,
    PeopleLookup,
    PublicFeedbackRepository,
    PublicFeedbackRow,
    PublicListEntry,
    StoredAttachment,
    utc_now,
)
from eifi1_server_kit.feedback.public import PUBLIC_PAGE_MAX, PUBLIC_PAGE_SIZE, is_public, public_body, public_text
from eifi1_server_kit.feedback.rules import (
    initial_status,
    plan_reply,
    plan_rework,
    plan_update,
    reject_manual_crash,
)
from eifi1_server_kit.feedback.schemas import (
    CrashReportCreate,
    CrashReportResponse,
    FeedbackCommentCreate,
    FeedbackCommentHide,
    FeedbackCommentResponse,
    FeedbackCreate,
    FeedbackPublicListItem,
    FeedbackPublicReplyResponse,
    FeedbackPublicResponse,
    FeedbackReplyCreate,
    FeedbackReplyResponse,
    FeedbackResponse,
    FeedbackReworkCreate,
    FeedbackUpdate,
    FeedbackVoteResponse,
    PublicSort,
)
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

#: ``(row, address)`` → the answer: the app's ``to_response``.
type ToResponse = Callable[[Any, str | None], FeedbackResponse]
#: ``user_id`` → ``(first_name, last_name)``, as :class:`PeopleLookup` answers.
type _Names = Mapping[int, tuple[str, str]]


def _default_to_response(row: Any, address: str | None) -> FeedbackResponse:
    return FeedbackResponse.model_validate(row).model_copy(update={"user_email": address})


def _full_name(names: _Names, user_id: int | None) -> str | None:
    found = names.get(user_id) if user_id is not None else None
    return (full_name(found[0], found[1], None) or None) if found is not None else None


def _first_name(names: _Names, user_id: int | None) -> str | None:
    found = names.get(user_id) if user_id is not None else None
    return first_name_of(found[0], found[1]) if found is not None else None


def _status(value: FeedbackStatus | str | None) -> FeedbackStatus | None:
    return None if value is None else FeedbackStatus(value)


def _kind(reply: FeedbackReplyRow) -> FeedbackEntryKind:
    return FeedbackEntryKind(reply.kind)


def _oldest_first[T: (FeedbackReplyRow, FeedbackCommentRow)](rows: Iterable[T]) -> list[T]:
    return sorted(rows, key=lambda row: (row.created_at, row.id))


def _throttle(limiter: SlidingWindowRateLimiter | None, user_id: int, detail: str) -> None:
    if limiter is None:
        return
    wait = limiter.hit(str(user_id))
    if wait is not None:
        raise FeedbackThrottledError(wait, detail)


@dataclass(frozen=True, slots=True)
class ErasedFeedback:
    """What :meth:`FeedbackService.erase_author` did (§2.15)."""

    #: Rows anonymised.
    reports: int
    #: Bare policy keys no other row names, sorted; the app deletes them at its commit.
    keys: tuple[str, ...]
    #: Comments scrubbed and un-named (§9.12), hidden ones included.
    comments: int = 0
    #: Votes deleted.
    votes: int = 0
    #: Entries un-named and scrubbed, kept: an erased admin's replies and an erased
    #: author's rework entries (whose files join ``keys``).
    replies: int = 0


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

    Public feedback (0.11, §9.14), every part optional: ``replies``, ``comments``,
    ``votes`` and ``people`` are the app's stores on the request's session;
    ``public_viewer`` is the app's predicate on the viewer (``None``: no public parts —
    kastlan); ``public_search`` whether ``q`` may search the title (False where it is
    sealed — Kurvenschmiede). With ``public_viewer`` set, ``comments``, ``votes`` and
    ``people`` are required and the repository must implement
    :class:`~eifi1_server_kit.feedback.PublicFeedbackRepository` (:class:`ValueError`).

    ``alerts`` (0.16, feedback §11) is the app's :class:`~eifi1_server_kit.feedback.FeedbackAlerts`:
    told after a new report, a new crash row and a rework that lands OPEN. ``None`` tells
    nobody — a loop's service (Kurvenschmiede's ``feedback_loop``) is built without it.
    """

    def __init__(
        self,
        repository: FeedbackRepository[RowT],
        *,
        policy: AttachmentUrlPolicy,
        foreign_row: Literal["forbidden", "not_found"] = "forbidden",
        now: Clock = utc_now,
        replies: FeedbackReplyStore[Any] | None = None,
        comments: FeedbackCommentStore[Any] | None = None,
        votes: FeedbackVoteStore | None = None,
        people: PeopleLookup | None = None,
        public_viewer: Callable[[FeedbackActor], bool] | None = None,
        public_search: bool = True,
        alerts: FeedbackAlerts | None = None,
    ) -> None:
        self.repository = repository
        self.alerts = alerts
        self.policy = policy
        self.foreign_row = foreign_row
        self.now = now
        self.reply_store = replies
        self.comment_store = comments
        self.vote_store = votes
        self.people = people
        self.public_viewer = public_viewer
        self.public_search = public_search
        if public_viewer is not None:
            missing = [
                name for name, store in (("comments", comments), ("votes", votes), ("people", people)) if store is None
            ]
            if missing:
                raise ValueError(f"FeedbackService: public_viewer needs {', '.join(missing)}")
            if not (hasattr(repository, "public_page") and hasattr(repository, "get_public")):
                raise ValueError("FeedbackService: public_viewer needs a repository with public_page and get_public")

    # --- the parts a flow needs ------------------------------------------------------

    def _public_repository(self) -> PublicFeedbackRepository[PublicFeedbackRow]:
        if not hasattr(self.repository, "get_public"):
            raise RuntimeError("FeedbackService: the repository has no public reads (PublicFeedbackRepository)")
        return cast(PublicFeedbackRepository[PublicFeedbackRow], self.repository)

    def _reply_store(self) -> FeedbackReplyStore[Any]:
        if self.reply_store is None:
            raise RuntimeError("FeedbackService: no reply store was given")
        return self.reply_store

    def _comment_store(self) -> FeedbackCommentStore[Any]:
        if self.comment_store is None:
            raise RuntimeError("FeedbackService: no comment store was given")
        return self.comment_store

    def _vote_store(self) -> FeedbackVoteStore:
        if self.vote_store is None:
            raise RuntimeError("FeedbackService: no vote store was given")
        return self.vote_store

    async def _alert(
        self, kind: FeedbackAlertKind, row: RowT, actor: FeedbackActor, category: FeedbackCategory | str | None
    ) -> None:
        """Tell the app's :class:`FeedbackAlerts`, after the write (feedback §11.2 rule 8)."""
        if self.alerts is None:
            return
        alert = FeedbackAlert(
            kind=kind,
            feedback_id=row.id,
            category=category,
            actor_id=actor.id,
            company_id=getattr(row, "company_id", None),
        )
        await self.alerts.filed(alert, row)

    async def _names(self, user_ids: Iterable[int | None]) -> _Names:
        wanted = {user_id for user_id in user_ids if user_id is not None}
        if self.people is None or not wanted:
            return {}
        return await self.people.names(wanted)

    async def create(self, actor: FeedbackActor, payload: FeedbackCreate) -> RowT:
        """File a report (§3.3): refused for a demo, CRASH refused (422), the first status
        the server's (:func:`~eifi1_server_kit.feedback.initial_status`), the identity in
        ``context`` the session's (:func:`~eifi1_server_kit.feedback.stamp_identity`, §2.13),
        ``attachment_urls`` ``None`` for none. With a vote store, the author's vote is
        cast after the report (§9.6). Then the admins' alert (§11, ``"report"``)."""
        refuse_demo(actor.is_demo, "feedback")
        reject_manual_crash(payload.category)
        row = await self.repository.add(
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
        if self.vote_store is not None:
            # The author's report is their vote (§9.6): the count starts at 1.
            await self.vote_store.put(row.id, actor.id)
        await self._alert("report", row, actor, payload.category)
        return row

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
        has just crashed. A NEW row tells the admins (§11, ``"crash"``); a fold does not.
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
                    public=False,
                )
            )
            duplicate = False
            await self._alert("crash", row, actor, FeedbackCategory.CRASH)
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
        the app chose it; :func:`~eifi1_server_kit.feedback.plan_update` (403 or 422), with
        the row's ``filed_private``; then the one write. ``public`` is a 422 where the app
        has no public parts (no ``public_viewer``).

        **The legacy append** (§10.3): an old client's rework append on an answered report
        is a :class:`~eifi1_server_kit.feedback.LegacyReworkAppendError` (coded 422, since
        0.16; 0.13–0.15 wrote it as an entry), raised by ``plan_update`` before any write."""
        refuse_demo(actor.is_demo, "feedback")
        reject_manual_crash(payload.category)
        changes = payload.model_dump(exclude_unset=True)
        if "public" in changes and self.public_viewer is None:
            raise FeedbackValidationError("public is not offered here")
        row = await self.repository.get(feedback_id, viewer=actor)
        if row is None:
            raise FeedbackNotFoundError()
        is_author = row.user_id == actor.id
        if self.foreign_row == "not_found" and not actor.is_admin and not is_author:
            raise FeedbackNotFoundError()
        plan = plan_update(
            status=row.status,
            body=row.body,
            changes=changes,
            is_admin=actor.is_admin,
            is_author=is_author,
            now=self.now(),
            filed_private=bool(getattr(row, "filed_private", False)),
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

        With the public-feedback stores (§9.12): the person's **comments** are scrubbed and
        un-named (kept, shown as "A user"), their **votes** deleted, their **replies** (an
        admin's) and **rework entries** (an author's, §10.3) un-named and scrubbed (kept),
        each rework file taken off its entry and joining ``keys``
        (:func:`~eifi1_server_kit.feedback.anonymise_reply`), and ``hidden_by`` cleared
        where it was them.
        """
        needles = list(identifiers)
        rows = await self.repository.by_author(user_id)
        erased_ids: list[int] = []
        erased_urls: set[str] = set()
        replies = 0
        if self.reply_store is not None:
            for reply in await self.reply_store.by_author(user_id):
                scrubbed = anonymise_reply(body=reply.body, attachment_url=reply.attachment_url, identifiers=needles)
                erased_urls |= scrubbed.urls
                await self.reply_store.apply(reply, scrubbed.changes)
                replies += 1
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
        comments = votes = 0
        if self.comment_store is not None:
            for comment in await self.comment_store.by_author(user_id):
                await self.comment_store.apply(
                    comment, {"body": scrub_text(comment.body, needles), "user_id": None}, actor=None
                )
                comments += 1
            await self.comment_store.clear_hidden_by(user_id)
        if self.vote_store is not None:
            votes = await self.vote_store.delete_by_author(user_id)
        return ErasedFeedback(reports=len(erased_ids), keys=keys, comments=comments, votes=votes, replies=replies)

    # --- public feedback: who sees what (§9.2 decisions 7 and 8, §9.9) ----------------

    def sees_public(self, actor: FeedbackActor) -> bool:
        """Does ``actor`` see the public parts? Never a demo (decision 8) and nobody when
        the app has none (no ``public_viewer``); always an admin; else the app's predicate."""
        if actor.is_demo or self.public_viewer is None:
            return False
        return actor.is_admin or self.public_viewer(actor)

    async def _seen(self, actor: FeedbackActor, feedback_id: int, *, commenting: bool) -> PublicFeedbackRow:
        """The report, when ``actor`` sees it for its comments or votes (§9.9), else 404:
        an admin; its author (for the comments only while they pass the predicate too); or
        anyone passing it when the report is public. Never a demo, never a CRASH."""
        if actor.is_demo:
            raise FeedbackNotFoundError()
        row = await self._public_repository().get_public(feedback_id)
        if row is None or FeedbackCategory(row.category) is FeedbackCategory.CRASH:
            raise FeedbackNotFoundError()
        if actor.is_admin:
            return row
        passes = self.sees_public(actor)
        if row.user_id == actor.id and (passes or not commenting):
            return row
        if passes and is_public(row.category, row.public):
            return row
        raise FeedbackNotFoundError()

    # --- the views: one call per store for a page ------------------------------------

    def _reply_view[ReplyT: FeedbackPublicReplyResponse](
        self, reply: FeedbackReplyRow, names: _Names, *, admin: bool, model: type[ReplyT]
    ) -> ReplyT:
        fields: dict[str, Any] = {
            "id": reply.id,
            "author_name": _full_name(names, reply.author_id) if admin else _first_name(names, reply.author_id),
            "body": reply.body if admin else public_text(reply.body, policy=self.policy),
            "status_to": _status(reply.status_to),
            "created_at": reply.created_at,
            "kind": _kind(reply),
        }
        if admin:
            fields["status_from"] = _status(reply.status_from)
        if issubclass(model, FeedbackReplyResponse):
            # The admin's and the author's views (§10.3); the public detail's model has no
            # such field, so a rework's file never leaves for others.
            fields["attachment_url"] = reply.attachment_url
        return model(**fields)

    async def _views(
        self,
        actor: FeedbackActor,
        pairs: Sequence[tuple[RowT, str | None]],
        *,
        admin: bool,
        to_response: ToResponse | None,
        reply_model: type[FeedbackReplyResponse],
    ) -> list[FeedbackResponse]:
        build = to_response or _default_to_response
        answers = [build(row, address) for row, address in pairs]
        ids = [row.id for row, _ in pairs]
        if not ids:
            return answers
        replies = await self.reply_store.for_reports(ids) if self.reply_store is not None else None
        counts = await self.comment_store.counts(ids, include_hidden=admin) if self.comment_store is not None else None
        tallies = await self.vote_store.counts(ids, viewer_id=actor.id) if self.vote_store is not None else None
        authors = [row.user_id for row, _ in pairs]
        authors += [reply.author_id for rows in (replies or {}).values() for reply in rows]
        names = await self._names(authors)
        out: list[FeedbackResponse] = []
        for (row, _), answer in zip(pairs, answers, strict=True):
            update: dict[str, Any] = {}
            if self.people is not None:
                update["author_name"] = _full_name(names, row.user_id)
            if self.public_viewer is not None:
                update["mine"] = row.user_id == actor.id
            if tallies is not None:
                update["votes"], update["voted"] = tallies.get(row.id, (0, False))
            if counts is not None:
                update["comment_count"] = counts.get(row.id, 0)
            if replies is not None:
                views = [
                    self._reply_view(reply, names, admin=admin, model=reply_model)
                    for reply in _oldest_first(replies.get(row.id, ()))
                ]
                update["replies"] = views
                # §9.7: `outcome` answers as the newest reply's body, so a client from
                # before 0.38 still shows the latest answer; the frozen column only while
                # a report has no reply. A rework entry is no answer (§10.3).
                answered = [view for view in views if view.kind is FeedbackEntryKind.REPLY]
                if answered:
                    update["outcome"] = answered[-1].body
            out.append(answer.model_copy(update=update) if update else answer)
        return out

    async def admin_view(
        self,
        actor: FeedbackActor,
        pairs: Sequence[tuple[RowT, str | None]],
        *,
        to_response: ToResponse | None = None,
        reply_model: type[FeedbackReplyResponse] = FeedbackReplyResponse,
    ) -> list[FeedbackResponse]:
        """A page of reports as the admin sees them (§9.3): each ``(row, address)`` through
        ``to_response`` (default: :class:`FeedbackResponse` with ``user_email`` the
        address), then what the stores given fill — ``author_name`` (the full name),
        ``mine`` (with public parts), ``votes`` / ``voted``, ``comment_count`` (hidden
        ones included) and ``replies`` (oldest first, with ``status_from``, as written) —
        with one call per store for the page."""
        return await self._views(actor, pairs, admin=True, to_response=to_response, reply_model=reply_model)

    async def author_view(
        self,
        actor: FeedbackActor,
        rows: Sequence[RowT],
        *,
        to_response: ToResponse | None = None,
        reply_model: type[FeedbackReplyResponse] = FeedbackReplyResponse,
    ) -> list[FeedbackResponse]:
        """A page of the actor's own reports (§9.3), as :meth:`admin_view` but: no address,
        visible comments only, and the replies as others see them — first names, no
        ``status_from``, every attachment URL removed (:func:`public_text`)."""
        pairs = [(row, None) for row in rows]
        return await self._views(actor, pairs, admin=False, to_response=to_response, reply_model=reply_model)

    async def public_item[ItemT: FeedbackPublicListItem = FeedbackPublicListItem](
        self,
        actor: FeedbackActor,
        entries: Sequence[PublicListEntry[Any]],
        *,
        model: type[ItemT] = FeedbackPublicListItem,  # type: ignore[assignment]
    ) -> list[ItemT]:
        """A page of the public list (§9.3, §9.10): titles only, the author's first name,
        the counts the repository's SQL gave; one call to ``people`` for the page."""
        names = await self._names(entry.row.user_id for entry in entries)
        return [
            model(
                id=entry.row.id,
                title=entry.row.title,
                category=entry.row.category,
                status=entry.row.status,
                created_at=entry.row.created_at,
                updated_at=entry.row.updated_at,
                resolved_at=entry.row.resolved_at,
                author_name=_first_name(names, entry.row.user_id),
                mine=entry.row.user_id == actor.id,
                votes=entry.votes,
                voted=entry.voted,
                comment_count=entry.comment_count,
            )
            for entry in entries
        ]

    async def public_view[DetailT: FeedbackPublicResponse = FeedbackPublicResponse](
        self,
        actor: FeedbackActor,
        row: PublicFeedbackRow,
        *,
        model: type[DetailT] = FeedbackPublicResponse,  # type: ignore[assignment]
    ) -> DetailT:
        """The public detail (§9.3, §9.10): the public body (:func:`public_body`), the
        replies as others see them, the counts — visible comments only — and the first
        name; one call per store."""
        replies = (await self.reply_store.for_reports([row.id])).get(row.id, ()) if self.reply_store is not None else ()
        votes, voted = (await self._vote_store().counts([row.id], viewer_id=actor.id)).get(row.id, (0, False))
        count = (await self._comment_store().counts([row.id], include_hidden=False)).get(row.id, 0)
        names = await self._names([row.user_id, *(reply.author_id for reply in replies)])
        return model(
            id=row.id,
            title=row.title,
            category=row.category,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
            author_name=_first_name(names, row.user_id),
            mine=row.user_id == actor.id,
            votes=votes,
            voted=voted,
            comment_count=count,
            body=public_body(row.body, policy=self.policy),
            replies=[
                self._reply_view(reply, names, admin=False, model=FeedbackPublicReplyResponse)
                for reply in _oldest_first(replies)
            ],
        )

    def _comment_view[CommentT: FeedbackCommentResponse](
        self, actor: FeedbackActor, comment: FeedbackCommentRow, names: _Names, model: type[CommentT]
    ) -> CommentT:
        mine = comment.user_id is not None and comment.user_id == actor.id
        hidden = comment.hidden_at is not None
        privileged = actor.is_admin or mine
        if hidden and not privileged:
            return model(
                id=comment.id,
                author_name=None,
                staff=comment.staff,
                body=None,
                hidden=True,
                created_at=comment.created_at,
            )
        return model(
            id=comment.id,
            author_name=_full_name(names, comment.user_id) if privileged else _first_name(names, comment.user_id),
            mine=mine,
            staff=comment.staff,
            body=comment.body if privileged else public_text(comment.body, policy=self.policy),
            hidden=hidden,
            created_at=comment.created_at,
        )

    # --- public feedback: the flows (§9.6–§9.9) ----------------------------------------

    async def reply(self, actor: FeedbackActor, feedback_id: int, payload: FeedbackReplyCreate) -> RowT:
        """An admin's answer (§9.7): refused for a demo, 403 "Insufficient role" for anyone
        else, 404 for a report the admin cannot see. The reply is added (the store fires
        ``feedback_answered``); with a ``status`` the report moves by
        :func:`~eifi1_server_kit.feedback.plan_reply` — the reply first, the report last.
        With ``status`` REWORK it is how an admin sends any report back (§10.3): still a
        reply, and it notifies. Answers the report; :meth:`admin_view` makes it the answer
        with its replies."""
        refuse_demo(actor.is_demo, "feedback")
        if not actor.is_admin:
            raise FeedbackForbiddenError("Insufficient role")
        store = self._reply_store()
        row = await self.repository.get(feedback_id, viewer=actor)
        if row is None:
            raise FeedbackNotFoundError()
        plan = plan_reply(status=row.status, new_status=payload.status, now=self.now())
        await store.add(
            NewFeedbackReply(
                feedback_id=row.id,
                author_id=actor.id,
                body=payload.body,
                status_from=plan.status_from,
                status_to=plan.status_to,
            ),
            actor=actor,
        )
        if plan.changes:
            row = await self.repository.apply(row, plan.changes, actor=actor)
        return row

    async def rework(self, actor: FeedbackActor, feedback_id: int, payload: FeedbackReworkCreate) -> RowT:
        """The author sends a report back (§10.3): refused for a demo; the file one of this
        app's URLs (422); the report this actor may see, or 404 — a foreign row 404 where
        the app chose it; then :func:`~eifi1_server_kit.feedback.plan_rework` (403 for
        anyone but the author, an admin included, and outside the reworkable statuses).
        The ``rework`` entry is added — the store fires nothing for it — then the report
        moves (REWORK for an admin author, OPEN otherwise): one write per store, the
        report last. Only once the report moved, and only when it landed OPEN, the admins
        are told (§11, ``"rework"``). Answers the report; the router answers it with its
        replies."""
        refuse_demo(actor.is_demo, "feedback")
        store = self._reply_store()
        if payload.attachment_url is not None:
            self.policy.validate_url(payload.attachment_url)
        row = await self.repository.get(feedback_id, viewer=actor)
        if row is None:
            raise FeedbackNotFoundError()
        is_author = row.user_id == actor.id
        if self.foreign_row == "not_found" and not actor.is_admin and not is_author:
            raise FeedbackNotFoundError()
        plan = plan_rework(status=row.status, is_author=is_author, actor_is_admin=actor.is_admin, now=self.now())
        await store.add(
            NewFeedbackReply(
                feedback_id=row.id,
                author_id=actor.id,
                body=payload.body,
                status_from=plan.status_from,
                status_to=plan.status_to,
                kind=FeedbackEntryKind.REWORK,
                attachment_url=payload.attachment_url,
            ),
            actor=actor,
        )
        moved = await self.repository.apply(row, plan.changes, actor=actor)
        if plan.status_to == FeedbackStatus.OPEN:
            await self._alert("rework", moved, actor, getattr(moved, "category", None))
        return moved

    async def list_public(
        self,
        actor: FeedbackActor,
        *,
        sort: PublicSort = "newest",
        status: FeedbackStatus | None = None,
        category: FeedbackCategory | None = None,
        q: str | None = None,
        limit: int = PUBLIC_PAGE_SIZE,
        offset: int = 0,
    ) -> Sequence[PublicListEntry[Any]]:
        """The public list's page (§9.9): 404 for anyone outside the predicate (a demo
        included); ``q`` trimmed, a 422 where titles are not searchable
        (``public_search=False``); ``limit`` 1–100, ``offset`` 0 or more (422); a CRASH
        filter finds nothing. :meth:`public_item` makes the answer."""
        if not self.sees_public(actor):
            raise FeedbackNotFoundError()
        text = (q or "").strip() or None
        if text is not None and not self.public_search:
            raise FeedbackValidationError("q is not offered here: titles are not searchable")
        if not 1 <= limit <= PUBLIC_PAGE_MAX or offset < 0:
            raise FeedbackValidationError(f"limit is 1–{PUBLIC_PAGE_MAX} and offset 0 or more")
        if category is FeedbackCategory.CRASH:
            return []
        return await self._public_repository().public_page(
            viewer_id=actor.id, sort=sort, status=status, category=category, q=text, limit=limit, offset=offset
        )

    async def get_public(self, actor: FeedbackActor, feedback_id: int) -> PublicFeedbackRow:
        """The report for the public detail (§9.9), or 404: the actor outside the
        predicate, a missing report, a CRASH, or one that is not public — unless the actor
        is its author or an admin. :meth:`public_view` makes the answer."""
        if not self.sees_public(actor):
            raise FeedbackNotFoundError()
        row = await self._public_repository().get_public(feedback_id)
        if row is None or FeedbackCategory(row.category) is FeedbackCategory.CRASH:
            raise FeedbackNotFoundError()
        if not (actor.is_admin or row.user_id == actor.id or is_public(row.category, row.public)):
            raise FeedbackNotFoundError()
        return row

    async def comments[CommentT: FeedbackCommentResponse = FeedbackCommentResponse](
        self,
        actor: FeedbackActor,
        feedback_id: int,
        *,
        model: type[CommentT] = FeedbackCommentResponse,  # type: ignore[assignment]
    ) -> list[CommentT]:
        """A report's comments, oldest first, for whoever sees it (§9.8, §9.9; else 404).
        A hidden one: its writer and an admin read it (``hidden`` true); anyone else gets
        no body and no name. Others' bodies pass :func:`public_text`; an admin and the
        writer see the full name, others the first."""
        await self._seen(actor, feedback_id, commenting=True)
        rows = _oldest_first(await self._comment_store().for_report(feedback_id))
        names = await self._names(row.user_id for row in rows)
        return [self._comment_view(actor, row, names, model) for row in rows]

    async def comment[CommentT: FeedbackCommentResponse = FeedbackCommentResponse](
        self,
        actor: FeedbackActor,
        feedback_id: int,
        payload: FeedbackCommentCreate,
        *,
        limiter: SlidingWindowRateLimiter | None = None,
        model: type[CommentT] = FeedbackCommentResponse,  # type: ignore[assignment]
    ) -> CommentT:
        """A comment (§9.8): refused for a demo; the window charged before the work
        (429); 404 unless the actor sees the report for its comments; on any status, never
        on a CRASH. The store's ``add`` fires ``feedback_comment``."""
        refuse_demo(actor.is_demo, "feedback")
        _throttle(limiter, actor.id, COMMENTS_THROTTLED_DETAIL)
        store = self._comment_store()
        await self._seen(actor, feedback_id, commenting=True)
        row = await store.add(
            NewFeedbackComment(feedback_id=feedback_id, user_id=actor.id, body=payload.body), actor=actor
        )
        return self._comment_view(actor, row, await self._names([actor.id]), model)

    async def hide_comment[CommentT: FeedbackCommentResponse = FeedbackCommentResponse](
        self,
        actor: FeedbackActor,
        feedback_id: int,
        comment_id: int,
        payload: FeedbackCommentHide,
        *,
        model: type[CommentT] = FeedbackCommentResponse,  # type: ignore[assignment]
    ) -> CommentT:
        """Hide or unhide a comment (§9.8), an admin only (403 "Insufficient role"; a demo
        refused first); 404 "Comment not found". Sets or clears ``hidden_at`` /
        ``hidden_by``; asking for the state it is in writes nothing."""
        refuse_demo(actor.is_demo, "feedback")
        if not actor.is_admin:
            raise FeedbackForbiddenError("Insufficient role")
        store = self._comment_store()
        row = await store.get(feedback_id, comment_id)
        if row is None:
            raise FeedbackNotFoundError("Comment not found")
        if (row.hidden_at is not None) != payload.hidden:
            changes = (
                {"hidden_at": self.now(), "hidden_by": actor.id}
                if payload.hidden
                else {"hidden_at": None, "hidden_by": None}
            )
            row = await store.apply(row, changes, actor=actor)
        return self._comment_view(actor, row, await self._names([row.user_id]), model)

    async def delete_comment(self, actor: FeedbackActor, feedback_id: int, comment_id: int) -> None:
        """Its writer deletes their own comment (§9.8), a hard delete; anyone else — an
        admin too, who hides instead — gets 404 "Comment not found". Refused for a demo."""
        refuse_demo(actor.is_demo, "feedback")
        store = self._comment_store()
        row = await store.get(feedback_id, comment_id)
        if row is None or row.user_id is None or row.user_id != actor.id:
            raise FeedbackNotFoundError("Comment not found")
        await store.delete(row)

    async def _tally(self, actor: FeedbackActor, feedback_id: int) -> FeedbackVoteResponse:
        votes, voted = (await self._vote_store().counts([feedback_id], viewer_id=actor.id)).get(feedback_id, (0, False))
        return FeedbackVoteResponse(votes=votes, voted=voted)

    async def vote(
        self, actor: FeedbackActor, feedback_id: int, *, limiter: SlidingWindowRateLimiter | None = None
    ) -> FeedbackVoteResponse:
        """ "I want this too" (§9.6): refused for a demo; the window charged first (429);
        404 unless the actor sees the report; idempotent; any status; never a CRASH."""
        refuse_demo(actor.is_demo, "feedback")
        _throttle(limiter, actor.id, VOTES_THROTTLED_DETAIL)
        store = self._vote_store()
        await self._seen(actor, feedback_id, commenting=False)
        await store.put(feedback_id, actor.id)
        return await self._tally(actor, feedback_id)

    async def unvote(
        self, actor: FeedbackActor, feedback_id: int, *, limiter: SlidingWindowRateLimiter | None = None
    ) -> FeedbackVoteResponse:
        """Take a vote back (§9.6), as :meth:`vote`; the author cannot — 409 "Your report
        counts as your vote". None to take back is the same answer."""
        refuse_demo(actor.is_demo, "feedback")
        _throttle(limiter, actor.id, VOTES_THROTTLED_DETAIL)
        store = self._vote_store()
        row = await self._seen(actor, feedback_id, commenting=False)
        if row.user_id == actor.id:
            raise FeedbackConflictError("Your report counts as your vote")
        await store.delete(feedback_id, actor.id)
        return await self._tally(actor, feedback_id)


def throttle_upload(limiter: SlidingWindowRateLimiter, user_id: int) -> None:
    """Charge one upload to ``user_id``'s window, or refuse with
    :class:`~eifi1_server_kit.feedback.FeedbackThrottledError` (429, ``Retry-After``).

    keksdose's support door calls it too, on the same ``.upload`` window, so the 429's
    sentence is written once."""
    _throttle(limiter, user_id, THROTTLED_DETAIL)


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
