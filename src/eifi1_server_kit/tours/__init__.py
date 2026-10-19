"""Tour progress on the account (``docs/tours-harmonization.md`` §10 in ``Eifi1/ui-kit``, 0.15).

ui-kit's ``useTourProgress`` keeps the record on the device; with an ``account`` it loads
and saves it here, and the device record becomes the account's cache. Layer 1: the wire
shapes (:class:`TourProgressResponse`, :class:`TourProgressPatch`, :data:`TOUR_ID_PATTERN`,
:data:`TOUR_PROGRESS_LIMIT`), the pure merge rule (:func:`merge_completions`: per tour,
the later completion wins) and the two coded 422s. Layer 2: the store port
:class:`TourProgressStore` each app implements over its ``tour_completions`` table and
:class:`TourProgressService`, re-exported here without FastAPI. The routes are
``eifi1_server_kit.tours.router.tour_progress_router`` (the ``fastapi`` extra) and the fake
``eifi1_server_kit.tours.testing.InMemoryTourProgressStore``; neither is re-exported.
"""

from __future__ import annotations

from eifi1_server_kit.tours.errors import (
    TOUR_PROGRESS_CONFLICT,
    TOUR_PROGRESS_FULL,
    TourProgressError,
    TourProgressErrorCode,
)
from eifi1_server_kit.tours.ports import TourProgressStore
from eifi1_server_kit.tours.rules import TourProgressMerge, merge_completions, progress_response
from eifi1_server_kit.tours.schemas import (
    TOUR_ID_PATTERN,
    TOUR_PROGRESS_LIMIT,
    TourId,
    TourInstant,
    TourProgressPatch,
    TourProgressResponse,
)
from eifi1_server_kit.tours.service import TOUR_PROGRESS_EXPORT_KEY, TourProgressService

__all__ = [
    "TOUR_ID_PATTERN",
    "TOUR_PROGRESS_CONFLICT",
    "TOUR_PROGRESS_EXPORT_KEY",
    "TOUR_PROGRESS_FULL",
    "TOUR_PROGRESS_LIMIT",
    "TourId",
    "TourInstant",
    "TourProgressError",
    "TourProgressErrorCode",
    "TourProgressMerge",
    "TourProgressPatch",
    "TourProgressResponse",
    "TourProgressService",
    "TourProgressStore",
    "merge_completions",
    "progress_response",
]
