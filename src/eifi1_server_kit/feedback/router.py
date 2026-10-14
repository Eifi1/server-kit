"""The feedback routes as one router factory: ``feedback_router(FeedbackDeps(...))``.

Layer 2 (``docs/server-services-harmonization.md`` §2.8–§2.10, §4.1 in ``Eifi1/ui-kit``),
behind the ``fastapi`` extra (``eifi1-server-kit[fastapi]``). The app passes its own
dependencies and shapes — the actor, the repository on the request's session, the
storage, its demo guard factory and its admin guard, its attachment URL policy and its
schema subclasses — and includes the router as it includes its own::

    router = feedback_router(FeedbackDeps(
        actor=feedback_actor, repository=feedback_repository, storage=feedback_storage,
        demo_guard=not_in_the_demo, admin_guard=require_role(UserRole.ADMIN),
        policy=FEEDBACK_ATTACHMENTS, create_model=FeedbackCreate, decode_images=True,
    ))
    app.include_router(router, prefix="/api/v1")
    app.state.feedback_limiters = FeedbackLimiters()
    install_contract_error_handlers(app)

**The guards are the app's** (§2.8), first in each route's ``dependencies=[…]``, so
FastAPI runs them before the endpoint's parameters and before the body is validated (a
demo's ``json={}`` is a 403 ``demo_refused``, not a 422), and the apps' route walks find
them by their own names. ``demo_guard("uploads")`` guards the upload,
``demo_guard("feedback")`` the create and the PATCH, ``admin_guard`` the inbox; the crash
route takes none (its service answers a demo ``stored: false``).

**The endpoints carry keksdose's names** (``upload_attachment``, ``download_attachment``,
``create``, ``report_crash``, ``list_my``, ``list_all``, ``update``), the upload's field is
``file``, the inbox's query ``status_filter`` and the path parameter ``{feedback_id}``, so
keksdose's operation ids and its ``Body_upload_attachment_api_v1_feedback_attachments_post``
component stay as they are. An app's own extra routes go AFTER this router: a
``GET /feedback/{feedback_id}`` included first would take ``GET /feedback/my``.

**The router raises no** ``HTTPException``: past the app's guards every refusal is a kit
refusal, answered by :func:`~eifi1_server_kit.errors.install_contract_error_handlers`.
Each request builds a :class:`~eifi1_server_kit.feedback.FeedbackService` on the
request's repository; each flow makes at most one repository write, as its last step, and
the kit never commits (§2.7).

**Public feedback** (0.11, ``docs/feedback-harmonization.md`` §9.9): with ``deps.replies``
the router adds ``POST /{feedback_id}/replies`` (``create_reply``); with
``deps.public_viewer`` (which needs ``comments``, ``votes`` and ``people``) eight more —
``list_public``, ``get_public``, ``list_comments``, ``create_comment``, ``hide_comment``,
``delete_comment``, ``vote``, ``unvote``. They go after the inbox and before the PATCH, so
``/public`` comes before any ``/{feedback_id}`` GET the app adds after this router. The
existing answers fill the new fields from the stores given (one call per store a page).

No ``from __future__ import annotations`` here, on purpose: the endpoints' parameter
types are the app's models, closed over in the factory, and FastAPI cannot resolve a
string annotation that names a local.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from eifi1_server_kit.feedback import service as _service
from eifi1_server_kit.feedback.attachments import AttachmentUrlPolicy
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackStatus
from eifi1_server_kit.feedback.ports import (
    AttachmentStorage,
    Clock,
    FeedbackActor,
    FeedbackRow,
    utc_now,
)
from eifi1_server_kit.feedback.public import PUBLIC_PAGE_MAX, PUBLIC_PAGE_SIZE
from eifi1_server_kit.feedback.schemas import (
    CrashReportCreate,
    CrashReportResponse,
    FeedbackAttachmentResponse,
    FeedbackCommentCreate,
    FeedbackCommentHide,
    FeedbackCommentResponse,
    FeedbackCreate,
    FeedbackPublicListItem,
    FeedbackPublicResponse,
    FeedbackReplyCreate,
    FeedbackReplyResponse,
    FeedbackResponse,
    FeedbackUpdate,
    FeedbackVoteResponse,
    PublicSort,
)
from eifi1_server_kit.limiter import FeedbackLimiters

try:
    from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.feedback.router needs FastAPI: install the kit with its fastapi extra, "
        "eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["FeedbackDeps", "app_state_limiters", "feedback_router"]


def app_state_limiters(request: Request) -> FeedbackLimiters:
    """The app's :class:`~eifi1_server_kit.limiter.FeedbackLimiters`, read per request from
    ``app.state.feedback_limiters`` — built once in ``create_app``, so a test that builds an
    app per test starts with empty windows."""
    limiters: FeedbackLimiters = request.app.state.feedback_limiters
    return limiters


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedbackDeps[RowT: FeedbackRow]:
    """What an app hands :func:`feedback_router` (§2.9): its dependencies, shapes and choices."""

    #: The app's dependency answering a :class:`~eifi1_server_kit.feedback.FeedbackActor`.
    actor: Callable[..., Any]
    #: The app's dependency answering a ``FeedbackRepository[RowT]`` on the request's session.
    repository: Callable[..., Any]
    #: The app's dependency answering an :class:`~eifi1_server_kit.feedback.AttachmentStorage`.
    storage: Callable[..., Any]
    #: The app's factory: ``demo_guard("uploads")``, ``demo_guard("feedback")``.
    demo_guard: Callable[[str], Callable[..., Any]]
    #: The app's dependency on ``GET /feedback``.
    admin_guard: Callable[..., Any]
    #: The app's keys and URLs; ``policy.prefix`` must be the mounted path.
    policy: AttachmentUrlPolicy
    #: The app's subclass: its names and fields. Its ``attachment_url_policy`` must be ``policy``.
    create_model: type[FeedbackCreate] = FeedbackCreate
    update_model: type[FeedbackUpdate] = FeedbackUpdate
    response_model: type[FeedbackResponse] = FeedbackResponse
    #: ``(row, address)`` → the answer; ``None``: ``response_model.model_validate(row)``
    #: with ``user_email`` the address (the inbox's; ``None`` everywhere else).
    to_response: Callable[[RowT, str | None], FeedbackResponse] | None = None
    #: A non-admin's PATCH on another person's row: 403 (keksdose, kastlan) or 404 (Kurvenschmiede).
    foreign_row: Literal["forbidden", "not_found"] = "forbidden"
    #: :func:`~eifi1_server_kit.uploads.ensure_decodable_image` on every upload (the ``images`` extra; kk, ka).
    decode_images: bool = False
    #: Text is text only when UTF-8 (Kurvenschmiede).
    require_utf8_text: bool = False
    limiters: Callable[[Request], FeedbackLimiters] = app_state_limiters
    now: Clock = utc_now
    # Public feedback (0.11, feedback §9.9, §9.14).
    #: The app's dependency answering a ``FeedbackReplyStore``: mounts ``create_reply``.
    replies: Callable[..., Any] | None = None
    #: The app's dependencies answering a ``FeedbackCommentStore``, a ``FeedbackVoteStore``
    #: and a ``PeopleLookup``: each required with ``public_viewer``.
    comments: Callable[..., Any] | None = None
    votes: Callable[..., Any] | None = None
    people: Callable[..., Any] | None = None
    #: Who sees the public parts, on the viewer (an admin always does, a demo never):
    #: keksdose every signed-in user, Kurvenschmiede ADMIN / MEMBER / REVIEWER. ``None``
    #: mounts no public route (kastlan).
    public_viewer: Callable[[FeedbackActor], bool] | None = None
    #: ``q`` searches the title; False where titles are sealed (Kurvenschmiede): a 422.
    public_search: bool = True
    reply_response_model: type[FeedbackReplyResponse] = FeedbackReplyResponse
    comment_response_model: type[FeedbackCommentResponse] = FeedbackCommentResponse
    public_item_model: type[FeedbackPublicListItem] = FeedbackPublicListItem
    public_response_model: type[FeedbackPublicResponse] = FeedbackPublicResponse


async def _absent() -> None:
    """A store the app did not give."""
    return None


def _default_response(model: type[FeedbackResponse]) -> Callable[[Any, str | None], FeedbackResponse]:
    def to_response(row: Any, address: str | None) -> FeedbackResponse:
        return model.model_validate(row).model_copy(update={"user_email": address})

    return to_response


def feedback_router[RowT: FeedbackRow](
    deps: FeedbackDeps[RowT], *, prefix: str = "/feedback", tags: Sequence[str] = ("feedback",)
) -> APIRouter:
    """The contract's seven routes (§3.3), on ``APIRouter(prefix=prefix, tags=tags)``, in
    this order: ``POST /attachments``, ``GET /attachments/{key}``, ``POST ""``,
    ``POST /crash``, ``GET /my``, ``GET ""``, then public feedback's routes (§9.9: the
    eight with ``public_viewer``, ``POST /{feedback_id}/replies`` with ``replies``), and
    ``PATCH /{feedback_id}`` last.

    Raises :class:`ValueError` when ``deps.create_model.attachment_url_policy`` is not
    ``deps.policy``: two policies would let a report name a key the download refuses; and
    when ``deps.public_viewer`` is set without ``comments``, ``votes`` or ``people``.
    """
    if deps.create_model.attachment_url_policy != deps.policy:
        raise ValueError(
            "feedback_router: create_model.attachment_url_policy is not deps.policy, so a report "
            "could name a key the download refuses"
        )
    if deps.public_viewer is not None:
        missing = [name for name in ("comments", "votes", "people") if getattr(deps, name) is None]
        if missing:
            raise ValueError(f"feedback_router: public_viewer needs {', '.join(missing)}")
    create_model = deps.create_model
    update_model = deps.update_model
    response_model = deps.response_model
    to_response = deps.to_response or _default_response(response_model)

    async def get_service(
        repository: Annotated[Any, Depends(deps.repository)],
        replies: Annotated[Any, Depends(deps.replies or _absent)],
        comments: Annotated[Any, Depends(deps.comments or _absent)],
        votes: Annotated[Any, Depends(deps.votes or _absent)],
        people: Annotated[Any, Depends(deps.people or _absent)],
    ) -> _service.FeedbackService[RowT]:
        return _service.FeedbackService(
            repository,
            policy=deps.policy,
            foreign_row=deps.foreign_row,
            now=deps.now,
            replies=replies,
            comments=comments,
            votes=votes,
            people=people,
            public_viewer=deps.public_viewer,
            public_search=deps.public_search,
        )

    Service = Annotated[_service.FeedbackService[Any], Depends(get_service)]  # noqa: N806

    async def answer(
        actor: FeedbackActor, service: _service.FeedbackService[RowT], pairs: Sequence[tuple[RowT, str | None]]
    ) -> list[FeedbackResponse]:
        """The admin's view for an admin, the author's for anyone else (§9.3)."""
        if actor.is_admin:
            return await service.admin_view(
                actor, pairs, to_response=to_response, reply_model=deps.reply_response_model
            )
        return await service.author_view(
            actor, [row for row, _ in pairs], to_response=to_response, reply_model=deps.reply_response_model
        )

    router = APIRouter(prefix=prefix, tags=list(tags))

    @router.post(
        "/attachments",
        response_model=FeedbackAttachmentResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(deps.demo_guard("uploads"))],
    )
    async def upload_attachment(
        request: Request,
        file: Annotated[UploadFile, File()],
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        storage: Annotated[AttachmentStorage, Depends(deps.storage)],
    ) -> FeedbackAttachmentResponse:
        """Store one file for a report not filed yet, and answer the URL to name it by."""
        url = await _service.upload_attachment(
            actor,
            file,
            declared_type=file.content_type,
            filename=file.filename,
            storage=storage,
            policy=deps.policy,
            limiter=deps.limiters(request).upload,
            decode_images=deps.decode_images,
            require_utf8_text=deps.require_utf8_text,
        )
        return FeedbackAttachmentResponse(url=url)

    @router.get("/attachments/{key}")
    async def download_attachment(
        key: str,
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        storage: Annotated[AttachmentStorage, Depends(deps.storage)],
    ) -> Response:
        """One file: the type its bytes give, ``Content-Disposition`` always, ``nosniff``."""
        found = await _service.download_attachment(
            actor, key, storage=storage, policy=deps.policy, require_utf8_text=deps.require_utf8_text
        )
        return Response(found.data, media_type=found.media_type, headers=found.headers)

    @router.post(
        "",
        response_model=response_model,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def create(
        payload: create_model,  # type: ignore[valid-type]
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        service: Service,
    ) -> FeedbackResponse:
        """File a report: the first status and the identity in ``context`` are the server's."""
        row = await service.create(actor, payload)
        return (await answer(actor, service, [(row, None)]))[0]

    @router.post("/crash", response_model=CrashReportResponse, status_code=status.HTTP_202_ACCEPTED)
    async def report_crash(
        payload: CrashReportCreate,
        request: Request,
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        service: Service,
    ) -> CrashReportResponse:
        """File a crash the client's error boundary caught: always a 202, ``stored: false``
        for a demo or a spent window."""
        return await service.report_crash(actor, payload, limiter=deps.limiters(request).crash)

    @router.get("/my", response_model=list[response_model])  # type: ignore[valid-type]
    async def list_my(
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        service: Service,
    ) -> list[FeedbackResponse]:
        """The caller's own reports, newest first."""
        return await answer(actor, service, [(row, None) for row in await service.list_mine(actor)])

    @router.get(
        "",
        response_model=list[response_model],  # type: ignore[valid-type]
        dependencies=[Depends(deps.admin_guard)],
    )
    async def list_all(
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        service: Service,
        status_filter: FeedbackStatus | None = None,
    ) -> list[FeedbackResponse]:
        """Every report, newest first, each with its author's address — the inbox."""
        rows = await service.inbox(actor, status_filter)
        return await service.admin_view(actor, rows, to_response=to_response, reply_model=deps.reply_response_model)

    if deps.public_viewer is not None:
        _public_routes(router, deps, Service)

    if deps.replies is not None:

        @router.post(
            "/{feedback_id}/replies",
            response_model=response_model,
            status_code=status.HTTP_201_CREATED,
            dependencies=[Depends(deps.admin_guard), Depends(deps.demo_guard("feedback"))],
        )
        async def create_reply(
            feedback_id: int,
            payload: FeedbackReplyCreate,
            actor: Annotated[FeedbackActor, Depends(deps.actor)],
            service: Service,
        ) -> FeedbackResponse:
            """An admin's answer, with the status it moves the report to, if any: the
            report comes back with its replies."""
            row = await service.reply(actor, feedback_id, payload)
            return (await answer(actor, service, [(row, None)]))[0]

    @router.patch(
        "/{feedback_id}",
        response_model=response_model,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def update(
        feedback_id: int,
        payload: update_model,  # type: ignore[valid-type]
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        service: Service,
    ) -> FeedbackResponse:
        """Triage a report, edit one's own, or send one back for rework."""
        row = await service.update(actor, feedback_id, payload)
        return (await answer(actor, service, [(row, None)]))[0]

    return router


def _public_routes(router: APIRouter, deps: FeedbackDeps[Any], Service: Any) -> None:  # noqa: N803
    """The eight public routes of §9.9, in their order, on ``router``."""
    item_model = deps.public_item_model
    detail_model = deps.public_response_model
    comment_model = deps.comment_response_model
    Actor = Annotated[FeedbackActor, Depends(deps.actor)]  # noqa: N806

    @router.get("/public", response_model=list[item_model])  # type: ignore[valid-type]
    async def list_public(
        actor: Actor,
        service: Service,
        sort: PublicSort = "newest",
        status_filter: FeedbackStatus | None = None,
        category: FeedbackCategory | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=PUBLIC_PAGE_MAX)] = PUBLIC_PAGE_SIZE,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[FeedbackPublicListItem]:
        """The public reports, titles only: newest first, most wanted, or recently updated."""
        entries = await service.list_public(
            actor, sort=sort, status=status_filter, category=category, q=q, limit=limit, offset=offset
        )
        return list(await service.public_item(actor, entries, model=item_model))

    @router.get("/public/{feedback_id}", response_model=detail_model)
    async def get_public(feedback_id: int, actor: Actor, service: Service) -> FeedbackPublicResponse:
        """One public report: the public body and the replies."""
        row = await service.get_public(actor, feedback_id)
        result: FeedbackPublicResponse = await service.public_view(actor, row, model=detail_model)
        return result

    @router.get("/{feedback_id}/comments", response_model=list[comment_model])  # type: ignore[valid-type]
    async def list_comments(feedback_id: int, actor: Actor, service: Service) -> list[FeedbackCommentResponse]:
        """A report's comments, oldest first."""
        return list(await service.comments(actor, feedback_id, model=comment_model))

    @router.post(
        "/{feedback_id}/comments",
        response_model=comment_model,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def create_comment(
        feedback_id: int, payload: FeedbackCommentCreate, request: Request, actor: Actor, service: Service
    ) -> FeedbackCommentResponse:
        """Comment on a report one sees."""
        result: FeedbackCommentResponse = await service.comment(
            actor, feedback_id, payload, limiter=deps.limiters(request).comment, model=comment_model
        )
        return result

    @router.patch(
        "/{feedback_id}/comments/{comment_id}",
        response_model=comment_model,
        dependencies=[Depends(deps.admin_guard), Depends(deps.demo_guard("feedback"))],
    )
    async def hide_comment(
        feedback_id: int, comment_id: int, payload: FeedbackCommentHide, actor: Actor, service: Service
    ) -> FeedbackCommentResponse:
        """Hide or unhide a comment (an admin)."""
        result: FeedbackCommentResponse = await service.hide_comment(
            actor, feedback_id, comment_id, payload, model=comment_model
        )
        return result

    @router.delete(
        "/{feedback_id}/comments/{comment_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def delete_comment(feedback_id: int, comment_id: int, actor: Actor, service: Service) -> Response:
        """Delete one's own comment."""
        await service.delete_comment(actor, feedback_id, comment_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.put(
        "/{feedback_id}/vote",
        response_model=FeedbackVoteResponse,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def vote(feedback_id: int, request: Request, actor: Actor, service: Service) -> FeedbackVoteResponse:
        """ "I want this too": idempotent."""
        result: FeedbackVoteResponse = await service.vote(actor, feedback_id, limiter=deps.limiters(request).vote)
        return result

    @router.delete(
        "/{feedback_id}/vote",
        response_model=FeedbackVoteResponse,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def unvote(feedback_id: int, request: Request, actor: Actor, service: Service) -> FeedbackVoteResponse:
        """Take one's vote back; the author's report stays their vote (409)."""
        result: FeedbackVoteResponse = await service.unvote(actor, feedback_id, limiter=deps.limiters(request).vote)
        return result
