"""``tours.testing``: the in-memory store keeps each account apart and logs its I/O."""

from __future__ import annotations

from datetime import UTC, datetime

from eifi1_server_kit.tours import TourProgressStore
from eifi1_server_kit.tours.testing import InMemoryTourProgressStore

AT = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


async def test_the_store_writes_and_reads_one_account() -> None:
    store = InMemoryTourProgressStore({1: {"a": AT, "b": None}, 2: {"a": None}})
    port: TourProgressStore = store
    await port.write(1, {"c": AT, "a": None}, ["b", "absent"])
    assert await port.completions(1) == {"a": None, "c": AT}
    assert await port.completions(2) == {"a": None}, "another account untouched"
    assert await port.completions(3) == {}
    assert store.reads == [1, 2, 3]
    assert store.writes == [(1, {"c": AT, "a": None}, ("b", "absent"))]


async def test_a_new_account_is_written_and_reads_are_copies() -> None:
    store = InMemoryTourProgressStore()
    await store.write(7, {"a": AT}, ())
    read = await store.completions(7)
    assert read == {"a": AT}
    read["b"] = None  # type: ignore[index]
    assert store.rows == {7: {"a": AT}}, "a read is a copy"
