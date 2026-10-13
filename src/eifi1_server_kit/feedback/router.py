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

No ``from __future__ import annotations`` here, on purpose: the endpoints' parameter
types are the app's models, closed over in the factory, and FastAPI cannot resolve a
string annotation that names a local.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from eifi1_server_kit.feedback import service as _service
from eifi1_server_kit.feedback.attachments import AttachmentUrlPolicy
from eifi1_server_kit.feedback.enums import FeedbackStatus
from eifi1_server_kit.feedback.ports import (
    AttachmentStorage,
    Clock,
    FeedbackActor,
    FeedbackRepository,
    FeedbackRow,
    utc_now,
)
from eifi1_server_kit.feedback.schemas import (
    CrashReportCreate,
    CrashReportResponse,
    FeedbackAttachmentResponse,
    FeedbackCreate,
    FeedbackResponse,
    FeedbackUpdate,
)
from eifi1_server_kit.limiter import FeedbackLimiters

try:
    from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, status
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


def _default_response(model: type[FeedbackResponse]) -> Callable[[Any, str | None], FeedbackResponse]:
    def to_response(row: Any, address: str | None) -> FeedbackResponse:
        return model.model_validate(row).model_copy(update={"user_email": address})

    return to_response


def feedback_router[RowT: FeedbackRow](
    deps: FeedbackDeps[RowT], *, prefix: str = "/feedback", tags: Sequence[str] = ("feedback",)
) -> APIRouter:
    """The contract's seven routes (§3.3), on ``APIRouter(prefix=prefix, tags=tags)``, in
    this order: ``POST /attachments``, ``GET /attachments/{key}``, ``POST ""``,
    ``POST /crash``, ``GET /my``, ``GET ""``, ``PATCH /{feedback_id}``.

    Raises :class:`ValueError` when ``deps.create_model.attachment_url_policy`` is not
    ``deps.policy``: two policies would let a report name a key the download refuses.
    """
    if deps.create_model.attachment_url_policy != deps.policy:
        raise ValueError(
            "feedback_router: create_model.attachment_url_policy is not deps.policy, so a report "
            "could name a key the download refuses"
        )
    create_model = deps.create_model
    update_model = deps.update_model
    response_model = deps.response_model
    to_response = deps.to_response or _default_response(response_model)

    def service(repository: FeedbackRepository[RowT]) -> _service.FeedbackService[RowT]:
        return _service.FeedbackService(repository, policy=deps.policy, foreign_row=deps.foreign_row, now=deps.now)

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
        repository: Annotated[FeedbackRepository[RowT], Depends(deps.repository)],
    ) -> FeedbackResponse:
        """File a report: the first status and the identity in ``context`` are the server's."""
        return to_response(await service(repository).create(actor, payload), None)

    @router.post("/crash", response_model=CrashReportResponse, status_code=status.HTTP_202_ACCEPTED)
    async def report_crash(
        payload: CrashReportCreate,
        request: Request,
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        repository: Annotated[FeedbackRepository[RowT], Depends(deps.repository)],
    ) -> CrashReportResponse:
        """File a crash the client's error boundary caught: always a 202, ``stored: false``
        for a demo or a spent window."""
        return await service(repository).report_crash(actor, payload, limiter=deps.limiters(request).crash)

    @router.get("/my", response_model=list[response_model])  # type: ignore[valid-type]
    async def list_my(
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        repository: Annotated[FeedbackRepository[RowT], Depends(deps.repository)],
    ) -> list[FeedbackResponse]:
        """The caller's own reports, newest first."""
        return [to_response(row, None) for row in await service(repository).list_mine(actor)]

    @router.get(
        "",
        response_model=list[response_model],  # type: ignore[valid-type]
        dependencies=[Depends(deps.admin_guard)],
    )
    async def list_all(
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        repository: Annotated[FeedbackRepository[RowT], Depends(deps.repository)],
        status_filter: FeedbackStatus | None = None,
    ) -> list[FeedbackResponse]:
        """Every report, newest first, each with its author's address — the inbox."""
        rows = await service(repository).inbox(actor, status_filter)
        return [to_response(row, address) for row, address in rows]

    @router.patch(
        "/{feedback_id}",
        response_model=response_model,
        dependencies=[Depends(deps.demo_guard("feedback"))],
    )
    async def update(
        feedback_id: int,
        payload: update_model,  # type: ignore[valid-type]
        actor: Annotated[FeedbackActor, Depends(deps.actor)],
        repository: Annotated[FeedbackRepository[RowT], Depends(deps.repository)],
    ) -> FeedbackResponse:
        """Triage a report, edit one's own, or send one back for rework."""
        return to_response(await service(repository).update(actor, feedback_id, payload), None)

    return router
