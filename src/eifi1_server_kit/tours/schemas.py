"""Tour progress on the wire: ``GET`` and ``PATCH /tours/progress`` (tours §10.2.2).

``docs/tours-harmonization.md`` §10 in ``Eifi1/ui-kit`` (server-kit 0.15). The stored
shape of the device record is the wire shape too: ``{completed: {[id]: ISO | ""}}``,
where ``""`` is "completed, no date known" (a record migrated from an id list). Never
``null``, so the settings rule's "null clears" never meets it. The table stores ``""`` as
a NULL ``completed_at``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

__all__ = [
    "TOUR_ID_PATTERN",
    "TOUR_PROGRESS_LIMIT",
    "TourId",
    "TourInstant",
    "TourProgressPatch",
    "TourProgressResponse",
]

#: A tour's id (§10.2.2): a letter or digit, then up to 63 of letters, digits, ``.``,
#: ``_`` and ``-``. kastlan's ids are camelCase (``addBuilding``). The server knows no
#: tour list, so any well-formed id is taken.
TOUR_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

#: At most this many entries in one body (``completed`` and ``forget`` together) and this
#: many tours on one account; beyond either, 422 ``tour_progress_full``.
TOUR_PROGRESS_LIMIT = 500


def _instant(value: object) -> object:
    """``""`` as it is; an ISO 8601 string parsed (a zone is checked after); a ``datetime``
    as it is. Anything else (a number, ``null``) is refused: a number is no ISO instant."""
    if value == "" or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError('a completion is an ISO 8601 instant with a zone, or ""')


#: A tour id as the body names it.
type TourId = Annotated[str, StringConstraints(pattern=TOUR_ID_PATTERN)]
#: A completion as the body sends it: an instant WITH a zone, or ``""`` (no date known).
type TourInstant = Annotated[AwareDatetime | Literal[""], BeforeValidator(_instant)]


class TourProgressResponse(BaseModel):
    """``{completed: {[id]: ISO | ""}}``: the account's whole record, the answer of both
    routes. Instants are UTC (``2026-10-19T14:05:00Z``)."""

    model_config = ConfigDict(extra="forbid")

    completed: dict[str, datetime | Literal[""]] = Field(default_factory=dict)


class TourProgressPatch(BaseModel):
    """``{completed?: {[id]: ISO | ""}, forget?: [id]}`` (§10.2.2). Unknown fields are a 422.

    ``completed`` merges by :func:`~eifi1_server_kit.tours.merge_completions` (the later
    completion wins); ``forget`` is "Mark as not done". An id in both is 422
    ``tour_progress_conflict``; more than :data:`TOUR_PROGRESS_LIMIT` entries 422
    ``tour_progress_full`` — both checked by the merge, so they carry their code.
    """

    model_config = ConfigDict(extra="forbid")

    completed: dict[TourId, TourInstant] = Field(default_factory=dict)
    forget: list[TourId] = Field(default_factory=list)
