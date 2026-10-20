"""The inbox's routes and the queue's jobs route (§10.1 rule 5, §10.4 rule 4; 0.16).

Layer 2, behind the ``fastapi`` extra (``eifi1-server-kit[fastapi]``). The app passes its
own dependencies and includes the routers as its own::

    app.include_router(inbox_router(InboxDeps(
        store=inbox_store, current_user_id=current_user_id, guards=(csrf,),
    )), prefix="/api/v1")
    app.include_router(notifications_jobs_router(
        queue_service, require_jobs_token(lambda: settings.jobs_token),
    ), prefix="/api/v1")

* ``GET /notifications/inbox?limit&offset`` → :class:`~.inbox.InboxResponse` (``limit``
  1–:data:`~.inbox.INBOX_PAGE_MAX`, 50 by default; ``offset`` 0 or more);
* ``POST /notifications/inbox/read {ids?}`` → :class:`~.inbox.InboxMarkReadResponse`;
* ``DELETE /notifications/inbox`` → 204.

The account is the session's own; no route takes a user id, and the store never touches
another account's rows. The demo reads its inbox (settings §4.1), so no demo guard is
implied: pass the app's own if it wants one. The kit never commits.

``POST /ops/notifications/flush`` → :class:`~.queue.FlushResult` is OPTIONAL: an app
calls :func:`~.queue.notifications_tick` from its own job path instead (keksdose's tick
job, kastlan's ``/ops/jobs/{name}``).

No ``from __future__ import annotations`` here, on purpose: the parameters'
``Depends(deps.store)`` name the factory's locals, which FastAPI cannot resolve from a
string.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.notifications.inbox import (
    INBOX_PAGE_DEFAULT,
    INBOX_PAGE_MAX,
    InboxItem,
    InboxMarkRead,
    InboxMarkReadResponse,
    InboxResponse,
    InboxStore,
)
from eifi1_server_kit.notifications.queue import FlushResult, QueueService, notifications_tick

try:
    from fastapi import APIRouter, Depends, Query, Response
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.notifications.router needs FastAPI: install the kit with its fastapi extra, "
        "eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["InboxDeps", "inbox_router", "notifications_jobs_router"]


@dataclass(frozen=True, slots=True, kw_only=True)
class InboxDeps:
    """What an app hands :func:`inbox_router` (§10.1 rule 5)."""

    #: The app's dependency answering an :class:`~.inbox.InboxStore` on the request's session.
    store: Callable[..., Any]
    #: The app's dependency answering the signed-in account's id.
    current_user_id: Callable[..., Any]
    #: The app's dependencies run before anything else on every route: its limiter, its
    #: CSRF check. A refusal they raise is the answer.
    guards: Sequence[Callable[..., Any]] = ()


def inbox_router(
    deps: InboxDeps, *, prefix: str = "/notifications/inbox", tags: Sequence[str] = ("notifications",)
) -> APIRouter:
    """``GET ""``, ``POST /read`` and ``DELETE ""`` on ``APIRouter(prefix=prefix, tags=tags)``,
    with ``deps.guards`` as the router's dependencies."""

    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(guard) for guard in deps.guards])

    @router.get("", response_model=InboxResponse)
    async def read_inbox(
        user_id: Annotated[int, Depends(deps.current_user_id)],
        store: Annotated[InboxStore, Depends(deps.store)],
        limit: Annotated[int, Query(ge=1, le=INBOX_PAGE_MAX)] = INBOX_PAGE_DEFAULT,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> InboxResponse:
        """A page of the account's inbox, newest first, and its unread count over all rows."""
        items, unread = await store.page(user_id, limit, offset)
        return InboxResponse(items=[InboxItem.model_validate(item) for item in items], unread_count=unread)

    @router.post("/read", response_model=InboxMarkReadResponse)
    async def mark_inbox_read(
        payload: InboxMarkRead,
        user_id: Annotated[int, Depends(deps.current_user_id)],
        store: Annotated[InboxStore, Depends(deps.store)],
    ) -> InboxMarkReadResponse:
        """Mark these rows read, or with ``ids`` null or left out the whole inbox."""
        return InboxMarkReadResponse(updated=await store.mark_read(user_id, payload.ids))

    @router.delete("", status_code=204)
    async def clear_inbox(
        user_id: Annotated[int, Depends(deps.current_user_id)],
        store: Annotated[InboxStore, Depends(deps.store)],
    ) -> Response:
        """Delete every row of the account's inbox."""
        await store.clear(user_id)
        return Response(status_code=204)

    return router


def notifications_jobs_router(
    service: Callable[..., Any],
    require_jobs_token: Callable[..., Any],
    *,
    now: Clock = utc_now,
    prefix: str = "/ops/notifications",
    tags: Sequence[str] = ("ops",),
) -> APIRouter:
    """``POST /flush`` → :class:`~.queue.FlushResult`: the tick (flush, then purge) behind
    the app's jobs token. ``service`` is the app's dependency answering a
    :class:`~.queue.QueueService` on the job's session; ``require_jobs_token`` the
    dependency :func:`~eifi1_server_kit.jobs.require_jobs_token` built. Optional: an app
    may call :func:`~.queue.notifications_tick` from its own job path instead."""

    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(require_jobs_token)])

    @router.post("/flush", response_model=FlushResult)
    async def flush_notifications(queue: Annotated[QueueService, Depends(service)]) -> FlushResult:
        """Send the held pushes and bundled mails that are due, then purge."""
        return await notifications_tick(queue, now())

    return router
