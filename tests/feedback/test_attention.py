"""The feedback tiles from one count per status (admin-harmonization §9.5)."""

from __future__ import annotations

from eifi1_server_kit.feedback import (
    AWAITING_STATUSES,
    QUEUE_STATUSES,
    FeedbackAttention,
    FeedbackStatus,
    feedback_attention,
)


def test_each_tile_is_its_set_of_the_kits_statuses() -> None:
    """To triage: OPEN. Awaiting a verdict: AWAITING_STATUSES without OPEN (kk's
    VERDICT_STATUSES). Released for work: QUEUE_STATUSES (KS's queue tile)."""
    counts = {status: 1 << index for index, status in enumerate(FeedbackStatus)}

    def total(statuses: frozenset[FeedbackStatus]) -> int:
        return sum(counts[status] for status in statuses)

    tiles = feedback_attention(counts)
    assert tiles == FeedbackAttention(
        to_triage=counts[FeedbackStatus.OPEN],
        awaiting_verdict=total(AWAITING_STATUSES - {FeedbackStatus.OPEN}),
        released_for_work=total(QUEUE_STATUSES),
    )
    # The sets as the contract names them today.
    assert tiles.awaiting_verdict == counts[FeedbackStatus.IN_EVALUATION] + counts[FeedbackStatus.NEEDS_LIVE_TEST]
    assert tiles.released_for_work == (
        counts[FeedbackStatus.READY] + counts[FeedbackStatus.REWORK] + counts[FeedbackStatus.IN_PROGRESS]
    )


def test_every_status_of_a_set_joins_its_tile() -> None:
    """A status the kit adds to a set counts on the tile with no app change: each member
    of each set, counted alone, reaches exactly its tile."""
    for status in FeedbackStatus:
        tiles = feedback_attention({status: 1})
        assert tiles.to_triage == (status is FeedbackStatus.OPEN), status
        assert tiles.awaiting_verdict == (status in AWAITING_STATUSES and status is not FeedbackStatus.OPEN), status
        assert tiles.released_for_work == (status in QUEUE_STATUSES), status


def test_a_missing_status_counts_zero() -> None:
    """A GROUP BY answers only the statuses with a row."""
    assert feedback_attention({}) == FeedbackAttention(0, 0, 0)
    assert feedback_attention({FeedbackStatus.DONE: 7}) == FeedbackAttention(0, 0, 0)
