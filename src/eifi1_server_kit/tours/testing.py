"""A fake :class:`~eifi1_server_kit.tours.TourProgressStore` for the kit's and the apps' tests.

No FastAPI, no database: the rows in a dict per account, and every read and write logged,
so a test sees which account was read and that a refusal wrote nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime

__all__ = ["InMemoryTourProgressStore"]


class InMemoryTourProgressStore:
    """``tour_completions`` in memory: ``rows[user_id][tour_id] = completed_at`` (``None``:
    no date). ``reads`` lists each account read, ``writes`` each write's
    ``(user_id, upserts, deletes)``. Nothing to commit."""

    def __init__(self, rows: Mapping[int, Mapping[str, datetime | None]] | None = None) -> None:
        self.rows: dict[int, dict[str, datetime | None]] = {
            user_id: dict(completions) for user_id, completions in (rows or {}).items()
        }
        self.reads: list[int] = []
        self.writes: list[tuple[int, dict[str, datetime | None], tuple[str, ...]]] = []

    async def completions(self, user_id: int) -> Mapping[str, datetime | None]:
        self.reads.append(user_id)
        return dict(self.rows.get(user_id, {}))

    async def write(self, user_id: int, upserts: Mapping[str, datetime | None], deletes: Collection[str]) -> None:
        self.writes.append((user_id, dict(upserts), tuple(deletes)))
        mine = self.rows.setdefault(user_id, {})
        for tour_id in deletes:
            mine.pop(tour_id, None)
        mine.update(upserts)
