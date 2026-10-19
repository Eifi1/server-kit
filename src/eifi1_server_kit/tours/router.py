"""The tour progress routes as one router factory: ``tour_progress_router(TourProgressDeps(...))``.

Layer 2 (``docs/tours-harmonization.md`` §10.2.2, §10.3 in ``Eifi1/ui-kit``), behind the
``fastapi`` extra (``eifi1-server-kit[fastapi]``). The app passes its own dependencies —
the store on the request's session, the signed-in account's id, and its guards — and
includes the router as it includes its own::

    router = tour_progress_router(TourProgressDeps(
        store=tour_progress_store, current_user_id=current_user_id, guards=(refuse_demo,),
    ))
    app.include_router(router, prefix="/api/v1")
    install_contract_error_handlers(app)

``GET /tours/progress`` and ``PATCH /tours/progress``, both answering
:class:`~eifi1_server_kit.tours.TourProgressResponse`. **The guards run first**, before
the account and the body, on both routes, so an app's demo refusal (kastlan's
``demo_read_only``, Kurvenschmiede's ``demo_refused``) passes through untouched. The app's
limiter and CSRF go there too, as for its other writes. **The account is the session's
own**: no route takes a user id. The kit never commits.

No ``from __future__ import annotations`` here, on purpose: the parameters'
``Depends(deps.store)`` name the factory's locals, which FastAPI cannot resolve from a
string.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.tours.ports import TourProgressStore
from eifi1_server_kit.tours.schemas import TourProgressPatch, TourProgressResponse
from eifi1_server_kit.tours.service import TourProgressService

try:
    from fastapi import APIRouter, Depends
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.tours.router needs FastAPI: install the kit with its fastapi extra, eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["TourProgressDeps", "tour_progress_router"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TourProgressDeps:
    """What an app hands :func:`tour_progress_router` (§10.3)."""

    #: The app's dependency answering a :class:`~eifi1_server_kit.tours.TourProgressStore`
    #: on the request's session.
    store: Callable[..., Any]
    #: The app's dependency answering the signed-in account's id (on its ``get_current_user``).
    current_user_id: Callable[..., Any]
    #: The app's dependencies run before anything else on both routes: its demo refusal,
    #: its limiter, its CSRF check. A refusal they raise is the answer.
    guards: Sequence[Callable[..., Any]] = ()
    now: Clock = utc_now


def tour_progress_router(
    deps: TourProgressDeps, *, prefix: str = "/tours", tags: Sequence[str] = ("tours",)
) -> APIRouter:
    """``GET /progress`` and ``PATCH /progress`` on ``APIRouter(prefix=prefix, tags=tags)``,
    with ``deps.guards`` as the router's dependencies."""

    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(guard) for guard in deps.guards])

    @router.get("/progress", response_model=TourProgressResponse)
    async def read_tour_progress(
        user_id: Annotated[int, Depends(deps.current_user_id)],
        store: Annotated[TourProgressStore, Depends(deps.store)],
    ) -> TourProgressResponse:
        """The signed-in account's tour progress: ``{completed: {[id]: ISO | ""}}``."""
        return await TourProgressService(store, now=deps.now).progress(user_id)

    @router.patch("/progress", response_model=TourProgressResponse)
    async def update_tour_progress(
        payload: TourProgressPatch,
        user_id: Annotated[int, Depends(deps.current_user_id)],
        store: Annotated[TourProgressStore, Depends(deps.store)],
    ) -> TourProgressResponse:
        """Merge completions (the later wins) and forget tours ("Mark as not done"); the
        whole record back. 422 ``tour_progress_conflict`` / ``tour_progress_full``."""
        return await TourProgressService(store, now=deps.now).update(user_id, payload)

    return router
