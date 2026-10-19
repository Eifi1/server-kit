"""The merge table of tour progress (tours §10.2.2–§10.2.3): the later completion wins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from eifi1_server_kit.tours import (
    TOUR_ID_PATTERN,
    TOUR_PROGRESS_CONFLICT,
    TOUR_PROGRESS_FULL,
    TOUR_PROGRESS_LIMIT,
    TourProgressError,
    TourProgressPatch,
    TourProgressResponse,
    merge_completions,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=2)
LATER = NOW - timedelta(hours=1)


def patch(**body: Any) -> TourProgressPatch:
    """The body as the route reads it: JSON values, validated."""
    return TourProgressPatch.model_validate(body)


def iso(at: datetime) -> str:
    return at.isoformat()


def test_an_instant_beats_no_date_and_no_date_beats_no_row() -> None:
    merged = merge_completions(
        {"dated": None, "kept": EARLIER},
        patch(completed={"dated": iso(LATER), "kept": "", "fresh": ""}),
        now=NOW,
    )
    assert merged.upserts == {"dated": LATER, "fresh": None}, "a '' never overwrites an instant"
    assert merged.deletes == ()
    assert merged.answer.completed == {"dated": LATER, "fresh": "", "kept": EARLIER}


def test_no_date_over_no_date_writes_nothing() -> None:
    merged = merge_completions({"a": None}, patch(completed={"a": ""}), now=NOW)
    assert merged.upserts == {} and merged.answer.completed == {"a": ""}


def test_the_later_instant_wins_either_way() -> None:
    later = merge_completions({"a": EARLIER}, patch(completed={"a": iso(LATER)}), now=NOW)
    assert later.upserts == {"a": LATER}
    earlier = merge_completions({"a": LATER}, patch(completed={"a": iso(EARLIER)}), now=NOW)
    assert earlier.upserts == {} and earlier.answer.completed == {"a": LATER}
    same = merge_completions({"a": LATER}, patch(completed={"a": iso(LATER)}), now=NOW)
    assert same.upserts == {}


def test_a_future_instant_is_stored_as_now_and_any_zone_as_utc() -> None:
    merged = merge_completions({}, patch(completed={"a": iso(NOW + timedelta(days=3))}), now=NOW)
    assert merged.upserts == {"a": NOW}
    zurich = datetime(2026, 10, 19, 13, 0, tzinfo=timezone(timedelta(hours=2)))
    shifted = merge_completions({}, patch(completed={"b": "2026-10-19T13:00:00+02:00"}), now=NOW)
    stored = shifted.upserts["b"]
    assert stored == zurich and stored is not None and stored.utcoffset() == timedelta(0)
    assert shifted.answer.model_dump(mode="json") == {"completed": {"b": "2026-10-19T11:00:00Z"}}


def test_forget_deletes_and_a_later_completion_brings_it_back() -> None:
    forgotten = merge_completions({"a": EARLIER, "b": None}, patch(forget=["a", "unknown"]), now=NOW)
    assert forgotten.deletes == ("a",), "an id with no row deletes nothing"
    assert forgotten.upserts == {} and forgotten.answer.completed == {"b": ""}
    # An offline device sends its completion after the forget: it was taken.
    back = merge_completions({"b": None}, patch(completed={"a": iso(EARLIER)}), now=NOW)
    assert back.upserts == {"a": EARLIER}


def test_the_answer_is_sorted_and_never_null() -> None:
    answer = merge_completions({"z": None, "a": EARLIER}, patch(), now=NOW).answer
    assert list(answer.completed) == ["a", "z"]
    assert answer.model_dump(mode="json") == {"completed": {"a": "2026-10-17T12:00:00Z", "z": ""}}


@pytest.mark.parametrize(
    "value",
    ["2026-10-19T12:00:00", "2026-10-19", "yesterday", 1760875200, None, "null"],
)
def test_an_instant_without_a_zone_is_a_422(value: object) -> None:
    with pytest.raises(ValidationError):
        patch(completed={"a": value})


def test_an_instant_may_be_given_as_a_datetime() -> None:
    """In Python, as a test or a seed builds it."""
    assert TourProgressPatch(completed={"a": LATER}).completed == {"a": LATER}


@pytest.mark.parametrize("bad", ["", "-lead", ".dot", "has space", "ü", "x" * 65, "a/b"])
def test_a_bad_id_is_a_422(bad: str) -> None:
    with pytest.raises(ValidationError):
        patch(completed={bad: ""})
    with pytest.raises(ValidationError):
        patch(forget=[bad])


def test_the_id_pattern() -> None:
    """kastlan's camelCase ids, dots, dashes and underscores; 64 characters at most."""
    assert TOUR_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    ok = patch(completed={"addBuilding": "", "admin.users": "", "a_b-c": "", "9" * 64: ""})
    assert len(ok.completed) == 4


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        patch(completed={}, reset=True)
    with pytest.raises(ValidationError):
        TourProgressResponse.model_validate({"completed": {}, "extra": 1})


def test_an_id_in_both_is_a_conflict() -> None:
    with pytest.raises(TourProgressError) as caught:
        merge_completions({}, patch(completed={"a": ""}, forget=["a"]), now=NOW)
    assert caught.value.code == TOUR_PROGRESS_CONFLICT == "tour_progress_conflict"
    assert caught.value.status_code == 422 and isinstance(caught.value, ValueError)


def test_more_than_500_per_body_is_full() -> None:
    assert TOUR_PROGRESS_LIMIT == 500
    ids = [f"t{index}" for index in range(TOUR_PROGRESS_LIMIT)]
    merge_completions({}, patch(completed=dict.fromkeys(ids, "")), now=NOW)
    with pytest.raises(TourProgressError) as caught:
        merge_completions(
            {}, patch(completed=dict.fromkeys(ids[:300], ""), forget=["x" + i for i in ids[:201]]), now=NOW
        )
    assert caught.value.code == TOUR_PROGRESS_FULL == "tour_progress_full"
    assert str(caught.value) == "Too many tours: at most 500 per request and per account"


def test_more_than_500_per_account_is_full() -> None:
    stored: dict[str, datetime | None] = {f"t{index}": None for index in range(TOUR_PROGRESS_LIMIT)}
    with pytest.raises(TourProgressError) as caught:
        merge_completions(stored, patch(completed={"one-more": ""}), now=NOW)
    assert caught.value.code == TOUR_PROGRESS_FULL
    # Updating a stored tour, or making room first, is fine at the limit.
    assert merge_completions(stored, patch(completed={"t0": iso(EARLIER)}), now=NOW).upserts == {"t0": EARLIER}
    room = merge_completions(stored, patch(completed={"one-more": ""}, forget=["t1"]), now=NOW)
    assert len(room.answer.completed) == TOUR_PROGRESS_LIMIT
    # An account already past it (written by hand) can still forget.
    over = {**stored, "extra": None}
    assert merge_completions(over, patch(forget=["extra"]), now=NOW).deletes == ("extra",)


def test_a_naive_store_and_clock_are_read_as_utc() -> None:
    """kastlan's naive ``timestamp`` columns and naive clock (S1): no TypeError, naive is UTC."""
    naive_now = NOW.replace(tzinfo=None)
    merged = merge_completions(
        {"kept": LATER.replace(tzinfo=None), "older": EARLIER.replace(tzinfo=None)},
        patch(completed={"kept": iso(EARLIER), "older": iso(LATER), "future": iso(NOW + timedelta(days=1))}),
        now=naive_now,
    )
    assert merged.upserts == {"older": LATER, "future": NOW}
    assert merged.answer.completed["kept"] == LATER
