"""Tour progress's two coded refusals (tours §10.2.2), both 422.

The codes are the contract's; an app with coded refusals (Kurvenschmiede) maps each to
its catalogue. :func:`~eifi1_server_kit.errors.install_contract_error_handlers` answers
:class:`TourProgressError` as ``{"detail": …, "code": …}`` at 422.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

__all__ = [
    "TOUR_PROGRESS_CONFLICT",
    "TOUR_PROGRESS_FULL",
    "TourProgressError",
    "TourProgressErrorCode",
]

#: One id both in ``completed`` and in ``forget``.
TOUR_PROGRESS_CONFLICT: Final = "tour_progress_conflict"
#: More than 500 entries in one body, or more than 500 tours on the account.
TOUR_PROGRESS_FULL: Final = "tour_progress_full"

type TourProgressErrorCode = Literal["tour_progress_conflict", "tour_progress_full"]

_DETAIL: Mapping[str, str] = {
    TOUR_PROGRESS_CONFLICT: "A tour cannot be both completed and forgotten in one request",
    TOUR_PROGRESS_FULL: "Too many tours: at most 500 per request and per account",
}


class TourProgressError(ValueError):
    """A coded 422: ``code`` is :data:`TOUR_PROGRESS_CONFLICT` or :data:`TOUR_PROGRESS_FULL`."""

    status_code = 422

    def __init__(self, code: TourProgressErrorCode) -> None:
        super().__init__(_DETAIL[code])
        self.code: TourProgressErrorCode = code
