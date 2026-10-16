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
    """The nine statuses every app speaks (contract §2.1, §8.2, §10.2), in the chain's order.

    ``OPEN`` — filed, not yet triaged: nobody works on it, agents included; ``READY`` —
    released for implementation by an admin, the triage step between OPEN and IN_PROGRESS
    (keksdose live #396, contract §8.2: an admin sets OPEN → READY, and an admin's own
    report starts there, :func:`~eifi1_server_kit.feedback.initial_status`);
    ``NEEDS_LIVE_TEST`` — resolved, but only checkable on a deployed build (keksdose
    feedback #196); ``POSTPONED`` — deliberately not now, neither a refusal nor a queue
    (keksdose feedback #195); ``REWORK`` — "Rework requested" (round 0.40, §10.2 decision
    4): sent back by its author or an admin, released for work again, so in the loops'
    queue with READY and IN_PROGRESS (:data:`QUEUE_STATUSES`); its next step is
    IN_PROGRESS.

    An app whose status column is a database enum adds the label before it can store
    READY (Postgres: ``ALTER TYPE feedbackstatus ADD VALUE 'READY' AFTER 'OPEN'``);
    existing OPEN rows stay OPEN — there is no back-fill. REWORK the same way (§10.4:
    ``ADD VALUE IF NOT EXISTS 'REWORK' AFTER 'READY'``, committed before any write of it).
    """

    OPEN = "OPEN"
    READY = "READY"
    REWORK = "REWORK"
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
#: (keksdose ``feedback_service.py:89`` ``_AUTHOR_EDITABLE_STATUSES``). READY joins it
#: (contract §8.2): released for implementation is not yet answered, so there is nothing
#: the author's edit could contradict.
AUTHOR_EDITABLE_STATUSES: frozenset[FeedbackStatus] = frozenset(
    {FeedbackStatus.OPEN, FeedbackStatus.READY, FeedbackStatus.IN_PROGRESS}
)

#: The two settled states: ``resolved_at`` is stamped on entering them, cleared on leaving,
#: and a crash matching a row in one of them files a NEW row (keksdose
#: ``feedback_service.py:90`` ``_TERMINAL_STATUSES``).
TERMINAL_STATUSES: frozenset[FeedbackStatus] = frozenset({FeedbackStatus.DONE, FeedbackStatus.WONT_DO})

#: Everything the team has answered — a report can be sent back for rework from these
#: (contract §3.4, §10.3; kit ``FEEDBACK_REWORKABLE_STATUSES``): the five answered
#: statuses, and REWORK itself (a second rework adds another entry, §10.2 decision 4).
#: READY is not among them (§8.2).
REWORKABLE_STATUSES: frozenset[FeedbackStatus] = frozenset(FeedbackStatus) - AUTHOR_EDITABLE_STATUSES

#: The statuses that wait on the person triaging (kit ``FEEDBACK_AWAITING_STATUSES``): OPEN,
#: since READY took over "released for implementation" (contract §8.2), and the two
#: answered statuses handed back for a verdict.
AWAITING_STATUSES: frozenset[FeedbackStatus] = frozenset(
    {FeedbackStatus.OPEN, FeedbackStatus.IN_EVALUATION, FeedbackStatus.NEEDS_LIVE_TEST}
)

#: The loops' queue (§10.2 decision 4): released for work — READY, REWORK — and the one
#: being worked on. Kurvenschmiede's loop adds OPEN (decision 9), a documented difference.
QUEUE_STATUSES: frozenset[FeedbackStatus] = frozenset(
    {FeedbackStatus.READY, FeedbackStatus.REWORK, FeedbackStatus.IN_PROGRESS}
)

#: The decided statuses (contract §9.7, B2): a status-only move into one of them tells the
#: author nothing, so the admin's detail nudges toward a reply with that ``status_to``
#: (kit ``FEEDBACK_DECIDED_STATUSES``). A reply notifies; a status alone does not.
DECIDED_STATUSES: frozenset[FeedbackStatus] = frozenset(
    {FeedbackStatus.DONE, FeedbackStatus.WONT_DO, FeedbackStatus.POSTPONED}
)


class FeedbackEntryKind(str, enum.Enum):
    """What one row of ``feedback_replies`` is (§10.2 decision 7): an admin's or a loop's
    ``reply``, or the author's ``rework`` — "this is not done yet", its own dated entry in
    the report's timeline since round 0.40, where it used to be a block appended to the
    body. Stored as the lower-case value (``VARCHAR(12) NOT NULL DEFAULT 'reply'``), so a
    loop's INSERT without the column writes a reply and can never write a rework."""

    REPLY = "reply"
    REWORK = "rework"
