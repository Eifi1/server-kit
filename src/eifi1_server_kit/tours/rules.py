"""The merge rule of tour progress: per tour, the later completion wins (tours §10.2.3).

Pure, and the one place the rule lives: the service runs it in Python rather than as SQL
``GREATEST``, so the in-memory store and every app's backend agree, and the rule is
tested once, here. For each id sent: an instant beats ``""``, ``""`` beats no row, and of
two instants the later is kept; an instant in the future is stored as ``now``. A forget is
a delete, and a completion sent later (an offline device) brings the tour back: it was
taken.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import NamedTuple

from eifi1_server_kit.tours.errors import TOUR_PROGRESS_CONFLICT, TOUR_PROGRESS_FULL, TourProgressError
from eifi1_server_kit.tours.schemas import TOUR_PROGRESS_LIMIT, TourProgressPatch, TourProgressResponse

__all__ = ["TourProgressMerge", "merge_completions", "progress_response"]


class TourProgressMerge(NamedTuple):
    """What :func:`merge_completions` decides: the rows to write, the ids to delete, the answer."""

    #: ``{tour_id: completed_at}`` to insert or overwrite; ``None`` is ``""`` (no date).
    upserts: dict[str, datetime | None]
    #: The stored ids the patch forgets, sorted.
    deletes: tuple[str, ...]
    #: The account's whole record after the write.
    answer: TourProgressResponse


def progress_response(rows: Mapping[str, datetime | None]) -> TourProgressResponse:
    """The wire shape of stored rows: by id, instants in UTC, ``None`` as ``""``."""
    return TourProgressResponse(
        completed={tour_id: "" if at is None else at.astimezone(UTC) for tour_id, at in sorted(rows.items())}
    )


def merge_completions(
    stored: Mapping[str, datetime | None], patch: TourProgressPatch, *, now: datetime
) -> TourProgressMerge:
    """Merge ``patch`` into the account's ``stored`` rows (``None``: completed, no date).

    A :class:`~eifi1_server_kit.tours.TourProgressError` ``tour_progress_conflict`` for an
    id in both ``completed`` and ``forget``, ``tour_progress_full`` for a body of more than
    :data:`~eifi1_server_kit.tours.TOUR_PROGRESS_LIMIT` entries or an account that would
    grow past it. Only rows that change are in ``upserts``.
    """
    if set(patch.completed) & set(patch.forget):
        raise TourProgressError(TOUR_PROGRESS_CONFLICT)
    if len(patch.completed) + len(patch.forget) > TOUR_PROGRESS_LIMIT:
        raise TourProgressError(TOUR_PROGRESS_FULL)
    rows = dict(stored)
    deletes = tuple(sorted({tour_id for tour_id in patch.forget if tour_id in rows}))
    for tour_id in deletes:
        del rows[tour_id]
    upserts: dict[str, datetime | None] = {}
    for tour_id, sent in patch.completed.items():
        instant = min(sent.astimezone(UTC), now) if isinstance(sent, datetime) else None
        if tour_id in rows:
            kept = rows[tour_id]
            if instant is None or (kept is not None and kept >= instant):
                continue
        rows[tour_id] = upserts[tour_id] = instant
    if len(rows) > TOUR_PROGRESS_LIMIT and len(rows) > len(stored):
        raise TourProgressError(TOUR_PROGRESS_FULL)
    return TourProgressMerge(upserts, deletes, progress_response(rows))
