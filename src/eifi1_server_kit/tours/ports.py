"""The port tour progress runs on: the app's ``tour_completions`` rows (tours §10.2.1).

Layer 2. Each app implements :class:`TourProgressStore` over its own table — ``user_id``
(FK ``users`` ON DELETE CASCADE), ``tour_id varchar(64)``, ``completed_at timestamptz
NULL``, primary key ``(user_id, tour_id)`` — on the request's session. No ``company_id``:
the progress is the person's. The store is async and runs in the caller's transaction:
**``write`` flushes and never commits**.

A naive ``timestamp`` column works too: the kit reads a naive ``completed_at`` as UTC, and
its upserts are aware UTC, so such a store writes ``value.replace(tzinfo=None)`` (asyncpg
refuses an aware value for a naive parameter).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Protocol

__all__ = ["TourProgressStore"]


class TourProgressStore(Protocol):
    """One account's tour completions, by ``user_id`` (the session's own, never another's)."""

    async def completions(self, user_id: int) -> Mapping[str, datetime | None]:
        """``{tour_id: completed_at}`` of this account; ``None`` is a completion with no date."""

    async def write(self, user_id: int, upserts: Mapping[str, datetime | None], deletes: Collection[str]) -> None:
        """Insert or overwrite each upsert's row, delete each id's row (absent: no-op), and
        flush; never commit. The kit sends each id once, never in both."""
