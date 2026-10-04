"""The feedback contract's two enums and the status sets the rules are written over.

Lifted from keksdose ``backend/keksdose/domain/models/feedback.py:12-43`` (contract §2.1,
§3.1). Each app may point its SQLAlchemy ``Enum(...)`` column at these classes: the
member NAMES are the database labels, so an app that already stores keksdose's names
needs no migration to switch.
"""

from __future__ import annotations

import enum


class FeedbackCategory(str, enum.Enum):
    """What a report is about.

    CRASH is listed first on purpose (keksdose ``models/feedback.py:13-18``): it is the
    one category nobody types — the client's error boundary files it — so the admin's
    category sort puts it on top, and keksdose's Postgres label sits ``BEFORE 'BUG'``
    (its migration 0050) so the database orders it the same way. Never client-settable:
    see :func:`eifi1_server_kit.feedback.reject_manual_crash`.
    """

    CRASH = "CRASH"
    BUG = "BUG"
    IDEA = "IDEA"
    QUESTION = "QUESTION"
    OTHER = "OTHER"


class FeedbackStatus(str, enum.Enum):
    """The seven statuses every app speaks (contract §2.1).

    ``NEEDS_LIVE_TEST`` — resolved, but only checkable on a deployed build (keksdose
    feedback #196); ``POSTPONED`` — deliberately not now, neither a refusal nor a queue
    (keksdose feedback #195).
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    IN_EVALUATION = "IN_EVALUATION"
    NEEDS_LIVE_TEST = "NEEDS_LIVE_TEST"
    POSTPONED = "POSTPONED"
    DONE = "DONE"
    WONT_DO = "WONT_DO"


#: What the category picker offers: everything but CRASH (kit ``FEEDBACK_PICKABLE_CATEGORIES``).
PICKABLE_CATEGORIES: tuple[FeedbackCategory, ...] = (
    FeedbackCategory.BUG,
    FeedbackCategory.IDEA,
    FeedbackCategory.QUESTION,
    FeedbackCategory.OTHER,
)

#: The author's lane: while a row is here its author may edit title / body / category
#: (keksdose ``feedback_service.py:89`` ``_AUTHOR_EDITABLE_STATUSES``).
AUTHOR_EDITABLE_STATUSES: frozenset[FeedbackStatus] = frozenset({FeedbackStatus.OPEN, FeedbackStatus.IN_PROGRESS})

#: The two settled states: ``resolved_at`` is stamped on entering them, cleared on leaving,
#: and a crash matching a row in one of them files a NEW row (keksdose
#: ``feedback_service.py:90`` ``_TERMINAL_STATUSES``).
TERMINAL_STATUSES: frozenset[FeedbackStatus] = frozenset({FeedbackStatus.DONE, FeedbackStatus.WONT_DO})

#: Everything the team has answered — a report can be sent back for rework from these, and
#: a rework append re-opens it (contract §3.4; kit ``FEEDBACK_REWORKABLE_STATUSES``).
REWORKABLE_STATUSES: frozenset[FeedbackStatus] = frozenset(FeedbackStatus) - AUTHOR_EDITABLE_STATUSES

#: The two statuses that wait on the person triaging (kit ``FEEDBACK_AWAITING_STATUSES``).
AWAITING_STATUSES: frozenset[FeedbackStatus] = frozenset({FeedbackStatus.IN_EVALUATION, FeedbackStatus.NEEDS_LIVE_TEST})
