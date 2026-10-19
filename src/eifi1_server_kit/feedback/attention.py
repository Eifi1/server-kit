"""The feedback counts an admin's attention page shows, from one count per status.

``docs/admin-harmonization.md`` §9.5 in ``Eifi1/ui-kit`` (0.15). Every app counted its
feedback tiles in its own SQL, by the same rule; now the app's repository answers
:meth:`~eifi1_server_kit.feedback.FeedbackRepository.count_by_status` (one ``GROUP BY
status`` in the request's scope) and :func:`feedback_attention` folds it into the tiles
from the kit's own sets, so a status the kit adds to a set joins every app's tile::

    counts = await repository.count_by_status()
    tiles = feedback_attention(counts)
    AdminAttention.of(feedback_open=tiles.to_triage, awaiting_verdict=tiles.awaiting_verdict, ...)

Pure: no ports, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from eifi1_server_kit.feedback.enums import AWAITING_STATUSES, QUEUE_STATUSES, FeedbackStatus

__all__ = ["FeedbackAttention", "feedback_attention"]

#: What waits on the person triaging, never seen yet: OPEN.
_TO_TRIAGE: frozenset[FeedbackStatus] = frozenset({FeedbackStatus.OPEN})

#: What was answered and handed back for a verdict: ``AWAITING_STATUSES`` without
#: OPEN (keksdose's ``VERDICT_STATUSES`` exactly; IN_EVALUATION and NEEDS_LIVE_TEST today).
_AWAITING_VERDICT: frozenset[FeedbackStatus] = AWAITING_STATUSES - _TO_TRIAGE


@dataclass(frozen=True, slots=True)
class FeedbackAttention:
    """The three feedback tiles (§9.5.2). An app shows the ones it has a tile for."""

    #: OPEN: new reports nobody has looked at.
    to_triage: int
    #: :data:`~eifi1_server_kit.feedback.AWAITING_STATUSES` without OPEN: answered, waiting
    #: on the admin's verdict.
    awaiting_verdict: int
    #: :data:`~eifi1_server_kit.feedback.QUEUE_STATUSES`: released for work, or being
    #: worked on (Kurvenschmiede's queue tile; its loop's queue adds OPEN on its own).
    released_for_work: int


def _sum(counts: Mapping[FeedbackStatus, int], statuses: frozenset[FeedbackStatus]) -> int:
    return sum(counts.get(status, 0) for status in statuses)


def feedback_attention(counts: Mapping[FeedbackStatus, int]) -> FeedbackAttention:
    """The tiles from :meth:`~eifi1_server_kit.feedback.FeedbackRepository.count_by_status`'s
    mapping. A status missing from ``counts`` counts 0."""
    return FeedbackAttention(
        to_triage=_sum(counts, _TO_TRIAGE),
        awaiting_verdict=_sum(counts, _AWAITING_VERDICT),
        released_for_work=_sum(counts, QUEUE_STATUSES),
    )
