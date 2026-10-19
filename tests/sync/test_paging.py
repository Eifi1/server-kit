"""The paged pull's cursor (keksdose's LP-3 fix) over rows in memory."""

from __future__ import annotations

import dataclasses

from eifi1_server_kit.sync import (
    SYNC_PAGE_LIMIT,
    PageStart,
    SyncPageCursor,
    begin_page,
    cursor_at_start,
    cut_page,
    is_after,
    page_of,
    page_order,
)

ENTITIES = ("accounts", "transactions")


@dataclasses.dataclass(frozen=True)
class Row:
    id: str
    knowledge: int


def _rows() -> dict[str, list[Row]]:
    return {
        "accounts": [Row("a2", 3), Row("a1", 3), Row("a0", 1)],
        # One flush stamps many rows with ONE knowledge.
        "transactions": [Row(f"t{i}", 5) for i in range(5)] + [Row("t9", 9)],
    }


def _walk(rows: dict[str, list[Row]], *, last: int, server: int, limit: int) -> list[list[tuple[str, str]]]:
    pages: list[list[tuple[str, str]]] = []
    cursor: SyncPageCursor | None = None
    while True:
        page, cursor = page_of(
            rows, entities=ENTITIES, cursor=cursor, last_knowledge=last, server_knowledge=server, limit=limit
        )
        pages.append([(entity, row.id) for entity, row in page])
        if cursor is None:
            return pages


def test_the_walk_cuts_inside_a_knowledge_value_without_losing_rows() -> None:
    pages = _walk(_rows(), last=0, server=9, limit=2)
    flat = [pair for page in pages for pair in page]
    assert flat == [
        ("accounts", "a0"),
        ("accounts", "a1"),
        ("accounts", "a2"),
        *[("transactions", f"t{i}") for i in range(5)],
        ("transactions", "t9"),
    ]
    assert [len(page) for page in pages] == [2, 2, 2, 2, 1]


def test_the_window_is_pinned_and_last_knowledge_filters() -> None:
    """Rows above ``server_knowledge`` (written mid-walk) wait for the next sync."""
    pages = _walk(_rows(), last=1, server=5, limit=SYNC_PAGE_LIMIT)
    assert pages == [[("accounts", "a1"), ("accounts", "a2"), *[("transactions", f"t{i}") for i in range(5)]]]


def test_a_current_device_gets_nothing() -> None:
    assert page_of(_rows(), entities=ENTITIES, cursor=None, last_knowledge=9, server_knowledge=9) == ([], None)
    assert begin_page(None, entities=ENTITIES, last_knowledge=9, server_knowledge=9) is None


def test_a_page_full_on_an_entity_boundary_resumes_at_its_start() -> None:
    page, cursor = page_of(_rows(), entities=ENTITIES, cursor=None, last_knowledge=0, server_knowledge=9, limit=3)
    assert len(page) == 3 and cursor == SyncPageCursor(entity="transactions", window_end=9)
    assert cursor == cursor_at_start("transactions", 9)
    start = begin_page(cursor, entities=ENTITIES, last_knowledge=0, server_knowledge=99)
    assert start == PageStart(start_index=1, window_end=9)


def test_an_unknown_entity_restarts_the_walk_but_keeps_the_window() -> None:
    stale = SyncPageCursor(entity="gone", knowledge=4, id="x", window_end=7)
    assert begin_page(stale, entities=ENTITIES, last_knowledge=0, server_knowledge=99) == PageStart(0, 7)


def test_the_resume_point_applies_to_its_own_entity_only() -> None:
    cursor = SyncPageCursor(entity="transactions", knowledge=5, id="t2", window_end=9)
    start = begin_page(cursor, entities=ENTITIES, last_knowledge=0, server_knowledge=9)
    assert start is not None and start.resume == (5, "t2")
    assert start.resume_in(1) == (5, "t2") and start.resume_in(0) is None
    # An entity-start cursor carries no resume point.
    assert begin_page(
        SyncPageCursor(entity="transactions", knowledge=5, window_end=9),
        entities=ENTITIES,
        last_knowledge=0,
        server_knowledge=9,
    ) == PageStart(1, 9)


def test_ordering_and_after() -> None:
    assert page_order(5, "b") < page_order(5, "c") < page_order(6, "a")
    assert is_after(5, "c", (5, "b")) and is_after(6, "a", (5, "z"))
    assert not is_after(5, "b", (5, "b")) and not is_after(4, "z", (5, "a"))
    assert is_after(0, "a", None)


def test_cut_page_with_custom_accessors() -> None:
    rows = [(1, "x"), (2, "y"), (2, "z")]
    kept, cursor = cut_page(
        "photos", rows, remaining=2, window_end=2, knowledge_of=lambda r: r[0], id_of=lambda r: r[1]
    )
    assert kept == rows[:2] and cursor == SyncPageCursor(entity="photos", knowledge=2, id="y", window_end=2)
    assert cut_page("photos", rows, remaining=3, window_end=2, knowledge_of=lambda r: r[0]) == (rows, None)


def test_an_entity_without_rows_is_skipped() -> None:
    page, cursor = page_of(
        {"transactions": [Row("t1", 2)]}, entities=ENTITIES, cursor=None, last_knowledge=0, server_knowledge=2
    )
    assert [(e, r.id) for e, r in page] == [("transactions", "t1")] and cursor is None
