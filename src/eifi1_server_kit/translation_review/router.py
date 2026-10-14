"""The translation review routes as one router factory: ``translation_review_router(TranslationReviewDeps(...))``.

Layer 2 (``docs/server-services-harmonization.md`` §6.2.6–§6.2.8 in ``Eifi1/ui-kit``),
behind the ``fastapi`` extra (``eifi1-server-kit[fastapi]``). The app passes its own
dependencies — the reviewer view on its ``get_current_user`` (keksdose's also reads a
review token), the store on the request's session — and its UI locales, and includes the
router as it includes its own::

    router = translation_review_router(TranslationReviewDeps(
        reviewer=translation_reviewer, store=translation_review_store,
        locale_codes=UI_LOCALE_CODES,
    ))
    app.include_router(router, prefix="/api/v1")
    install_contract_error_handlers(app)

Mounted at ``/api/v1``, the paths are :data:`~eifi1_server_kit.cors.TRANSLATION_REVIEW_ROUTES`'s.

**No route guard** (§6.2.6): the three routes are "any signed-in", and the viewer's grant
gates each — a ``GET`` with no locale is 403, a write outside the grant 403. A demo is
granted nothing. **The endpoints carry keksdose's names** (``list_translation_reviews``,
``save_translation_reviews``, ``clear_translation_reviews``), so its and kastlan's
operation ids stay. An app's own routes (keksdose's review tokens, kastlan's
``GET /translations/documents``) go AFTER this router, on a router of their own.

**The router raises no** ``HTTPException`` (§6.2.8): past the app's ``reviewer``
dependency every refusal is layer 1's, answered by
:func:`~eifi1_server_kit.errors.install_contract_error_handlers`. The bodies are the
kit's models, which refuse unknown fields (T5), so a misspelt field is a 422 for every
app. Each request builds a
:class:`~eifi1_server_kit.translation_review.TranslationReviewService` on the request's
store; each makes at most one store write, as its last step, and the kit never commits.

No ``from __future__ import annotations`` here, on purpose: the parameters'
``Depends(deps.reviewer)`` and ``Depends(deps.store)`` name the factory's locals, and
FastAPI cannot resolve a string annotation that names a local.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.translation_review.ports import TranslationReviewer, TranslationReviewRow, TranslationReviewStore
from eifi1_server_kit.translation_review.schemas import (
    TranslationReviewBatch,
    TranslationReviewClear,
    TranslationReviewClearResponse,
    TranslationReviewsResponse,
    TranslationReviewWriteResponse,
)
from eifi1_server_kit.translation_review.service import TranslationReviewService

try:
    from fastapi import APIRouter, Depends
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.translation_review.router needs FastAPI: install the kit with its fastapi extra, "
        "eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["TranslationReviewDeps", "translation_review_router"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslationReviewDeps[RowT: TranslationReviewRow]:
    """What an app hands :func:`translation_review_router` (§6.2.7): its dependencies and vocabulary."""

    #: The app's dependency answering a
    #: :class:`~eifi1_server_kit.translation_review.TranslationReviewer`, on its
    #: ``get_current_user`` (keksdose's: or a review token).
    reviewer: Callable[..., Any]
    #: The app's dependency answering a ``TranslationReviewStore[RowT]`` on the request's session.
    store: Callable[..., Any]
    #: The app's UI locales, in its order.
    locale_codes: Sequence[str]
    now: Clock = utc_now


def translation_review_router[RowT: TranslationReviewRow](
    deps: TranslationReviewDeps[RowT], *, prefix: str = "/translations", tags: Sequence[str] = ("translations",)
) -> APIRouter:
    """H1's three routes (§6.3), on ``APIRouter(prefix=prefix, tags=tags)``, in this order:
    ``GET /reviews``, ``PUT /reviews``, ``POST /reviews/clear``."""

    def service(store: TranslationReviewStore[RowT]) -> TranslationReviewService[RowT]:
        return TranslationReviewService(store, locale_codes=deps.locale_codes, now=deps.now)

    router = APIRouter(prefix=prefix, tags=list(tags))

    @router.get("/reviews", response_model=TranslationReviewsResponse)
    async def list_translation_reviews(
        reviewer: Annotated[TranslationReviewer, Depends(deps.reviewer)],
        store: Annotated[TranslationReviewStore[RowT], Depends(deps.store)],
    ) -> TranslationReviewsResponse:
        """Every verdict in the caller's locales (and areas), and which locales those are.
        403 for a caller granted no locale."""
        return await service(store).list_reviews(reviewer)

    @router.put("/reviews", response_model=TranslationReviewWriteResponse)
    async def save_translation_reviews(
        payload: TranslationReviewBatch,
        reviewer: Annotated[TranslationReviewer, Depends(deps.reviewer)],
        store: Annotated[TranslationReviewStore[RowT], Depends(deps.store)],
    ) -> TranslationReviewWriteResponse:
        """Judge one string or a whole selection: APPROVED, or NEEDS_CHANGE with a note."""
        return TranslationReviewWriteResponse(reviews=await service(store).save(reviewer, payload.items))

    @router.post("/reviews/clear", response_model=TranslationReviewClearResponse)
    async def clear_translation_reviews(
        payload: TranslationReviewClear,
        reviewer: Annotated[TranslationReviewer, Depends(deps.reviewer)],
        store: Annotated[TranslationReviewStore[RowT], Depends(deps.store)],
    ) -> TranslationReviewClearResponse:
        """Undo: the strings go back to unreviewed. A POST because it takes a list."""
        cleared = await service(store).clear(reviewer, [(item.locale, item.key) for item in payload.items])
        return TranslationReviewClearResponse(cleared=cleared)

    return router
