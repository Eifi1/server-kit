"""The tour progress service: read, merge and write one account's record (tours §10.2).

Layer 2, no FastAPI. Each flow reads the account's rows once and makes at most one store
write, its last step, and only when something changed; the kit never commits::

    service = TourProgressService(store)
    answer = await service.update(user.id, patch)

The account is always the session's own: no admin route reads or writes someone else's.
"""

from __future__ import annotations

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.sync.merge import as_utc
from eifi1_server_kit.tours.ports import TourProgressStore
from eifi1_server_kit.tours.rules import merge_completions, progress_response
from eifi1_server_kit.tours.schemas import TourProgressPatch, TourProgressResponse

__all__ = ["TOUR_PROGRESS_EXPORT_KEY", "TourProgressService"]

#: Where an app's account export puts :meth:`TourProgressService.export` (§10.2.9):
#: ``data["tour_progress"]``.
TOUR_PROGRESS_EXPORT_KEY = "tour_progress"


class TourProgressService:
    """``GET`` and ``PATCH /tours/progress`` over the app's :class:`~eifi1_server_kit.tours.TourProgressStore`."""

    def __init__(self, store: TourProgressStore, *, now: Clock = utc_now) -> None:
        self.store = store
        self.now = now

    async def progress(self, user_id: int) -> TourProgressResponse:
        """The account's whole record."""
        return progress_response(await self.store.completions(user_id))

    async def update(self, user_id: int, patch: TourProgressPatch) -> TourProgressResponse:
        """Merge ``patch`` (:func:`~eifi1_server_kit.tours.merge_completions`), write what
        changed, and answer the whole record. A refusal writes nothing."""
        merged = merge_completions(await self.store.completions(user_id), patch, now=self.now())
        if merged.upserts or merged.deletes:
            await self.store.write(user_id, merged.upserts, merged.deletes)
        return merged.answer

    async def export(self, user_id: int) -> list[dict[str, str | None]]:
        """The account export's rows (§10.2.9): ``[{tour_id, completed_at}]`` by
        ``tour_id``, ``completed_at`` an ISO instant in UTC (a naive one read as UTC) or ``None`` for ``""``."""
        rows = await self.store.completions(user_id)
        return [
            {"tour_id": tour_id, "completed_at": None if at is None else as_utc(at).isoformat()}
            for tour_id, at in sorted(rows.items())
        ]
