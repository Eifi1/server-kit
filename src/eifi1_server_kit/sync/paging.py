"""The paged pull's cursor, as pure functions over rows (``docs/local-first-harmonization.md``
§1.6 in ``Eifi1/ui-kit``; keksdose ``sync_service.pull_changes_page``, its LP-3 fix).

Three properties make a split pull safe, and each is one a plain row cap gets wrong:

* **The cut never lands inside a knowledge value.** Rows are ordered ``(knowledge, id)``
  per entity and the cursor carries both, so a resume is "strictly after this ROW".
* **Entities stay in order across pages.** The walk resumes at the cursor's entity and
  finishes it before the next, so a parent reaches the device before its children.
* **The window is pinned** at the tenant knowledge when the walk began
  (:attr:`~eifi1_server_kit.sync.SyncPageCursor.window_end`); the client records it as its
  cursor only when the walk returns no cursor.

A database-backed pull uses the pieces per entity (:func:`begin_page`,
:meth:`PageStart.resume_in` for its ``WHERE``, ordered by ``(knowledge, id)``, fetched with
``LIMIT remaining + 1``, then :func:`cut_page`); :func:`page_of` is the same walk over rows
in memory. Ids are compared as text: the hyphenated lowercase form of a UUID orders as the
database orders the UUID.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from eifi1_server_kit.sync.schemas import SyncPageCursor

__all__ = [
    "SYNC_PAGE_LIMIT",
    "PageStart",
    "begin_page",
    "cursor_at_start",
    "cut_page",
    "is_after",
    "page_of",
    "page_order",
]

#: keksdose's page size, in rows.
SYNC_PAGE_LIMIT: Final = 2000


def _knowledge(row: Any) -> int:
    return int(row.knowledge)


def _id(row: Any) -> object:
    return row.id


@dataclasses.dataclass(frozen=True, slots=True)
class PageStart:
    """Where a page begins: the entity index, the pinned window and, inside that entity,
    the row to resume strictly after."""

    start_index: int
    window_end: int
    #: ``(knowledge, id)`` of the last row sent from the entity at ``start_index``; ``None``
    #: to start at its beginning.
    resume: tuple[int, str] | None = None

    def resume_in(self, index: int) -> tuple[int, str] | None:
        """The resume point for the entity at ``index``: only the one the cursor stopped
        inside has one."""
        return self.resume if index == self.start_index else None


def page_order(knowledge: int, row_id: object) -> tuple[int, str]:
    """The order rows are walked in within an entity."""
    return knowledge, str(row_id)


def is_after(knowledge: int, row_id: object, resume: tuple[int, str] | None) -> bool:
    """Whether a row comes strictly after ``resume`` (always, without one): a higher
    knowledge, or the same knowledge and a higher id."""
    return resume is None or page_order(knowledge, row_id) > resume


def begin_page(
    cursor: SyncPageCursor | None, *, entities: Sequence[str], last_knowledge: int, server_knowledge: int
) -> PageStart | None:
    """Where the next page starts; ``None`` when there is nothing to pull (no cursor and the
    device is current).

    A cursor naming an entity this build does not know (a rolling deploy, a rollback)
    discards its POSITION and keeps its window: the walk restarts at the first entity.
    Applying its row tiebreak to another entity would silently skip rows below the cursor
    the device is about to record, for good."""
    if cursor is None:
        if last_knowledge >= server_knowledge:
            return None
        return PageStart(start_index=0, window_end=server_knowledge)
    if cursor.entity not in entities:
        return PageStart(start_index=0, window_end=cursor.window_end)
    resume = (cursor.knowledge, cursor.id) if cursor.knowledge is not None and cursor.id is not None else None
    return PageStart(start_index=entities.index(cursor.entity), window_end=cursor.window_end, resume=resume)


def cursor_at_start(entity: str, window_end: int) -> SyncPageCursor:
    """The cursor for a page that filled up before ``entity`` was touched: resume AT its
    start rather than after an invented row."""
    return SyncPageCursor(entity=entity, window_end=window_end)


def cut_page[R](
    entity: str,
    rows: Sequence[R],
    *,
    remaining: int,
    window_end: int,
    knowledge_of: Callable[[R], int] = _knowledge,
    id_of: Callable[[R], object] = _id,
) -> tuple[list[R], SyncPageCursor | None]:
    """Cut one entity's ordered rows, fetched with ``LIMIT remaining + 1``, to the page's
    budget. One row more than ``remaining`` means a next page, and the cursor points at the
    last row kept; otherwise the entity is done (``None``)."""
    if len(rows) <= remaining:
        return list(rows), None
    kept = list(rows[:remaining])
    last = kept[-1]
    return kept, SyncPageCursor(entity=entity, knowledge=knowledge_of(last), id=str(id_of(last)), window_end=window_end)


def page_of[R](
    rows_by_entity: Mapping[str, Iterable[R]],
    *,
    entities: Sequence[str],
    cursor: SyncPageCursor | None,
    last_knowledge: int,
    server_knowledge: int,
    limit: int = SYNC_PAGE_LIMIT,
    knowledge_of: Callable[[R], int] = _knowledge,
    id_of: Callable[[R], object] = _id,
) -> tuple[list[tuple[str, R]], SyncPageCursor | None]:
    """One page of the delta over rows in memory, as ``(entity, row)`` pairs, and where to
    resume (``None`` when the walk is done). The rows of each entity may come unordered and
    unfiltered: the window ``last_knowledge < knowledge <= window_end`` and the resume point
    are applied here."""
    start = begin_page(cursor, entities=entities, last_knowledge=last_knowledge, server_knowledge=server_knowledge)
    if start is None:
        return [], None
    page: list[tuple[str, R]] = []
    for index in range(start.start_index, len(entities)):
        entity = entities[index]
        if len(page) >= limit:
            return page, cursor_at_start(entity, start.window_end)
        resume = start.resume_in(index)
        candidates = sorted(
            (
                row
                for row in rows_by_entity.get(entity, ())
                if last_knowledge < knowledge_of(row) <= start.window_end
                and is_after(knowledge_of(row), id_of(row), resume)
            ),
            key=lambda row: page_order(knowledge_of(row), id_of(row)),
        )
        remaining = limit - len(page)
        kept, next_cursor = cut_page(
            entity,
            candidates[: remaining + 1],
            remaining=remaining,
            window_end=start.window_end,
            knowledge_of=knowledge_of,
            id_of=id_of,
        )
        page.extend((entity, row) for row in kept)
        if next_cursor is not None:
            return page, next_cursor
    return page, None
