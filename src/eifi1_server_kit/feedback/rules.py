"""Who may change what on a report, and what the server changes with it (contract §3.4).

keksdose ``feedback_service.py:117-196`` ``update_feedback``, without the session and
without the ``User``: the row's stored status and body, the fields the client sent, and
two booleans the app computes its own way (keksdose ``role == ADMIN``, kastlan "any of
its maintainer roles", Kurvenschmiede ``role is ADMIN``)::

    reject_manual_crash(payload.category)            # 422 even for a missing row
    row = await load(feedback_id)                    # the app's query, RLS, 404
    plan = plan_update(
        status=row.status, body=row.body,
        changes=payload.model_dump(exclude_unset=True),
        is_admin=..., is_author=row.user_id == actor.id,
    )                                                # FeedbackForbiddenError → 403
    for name, value in plan.changes.items():
        setattr(row, name, value)

The lanes:

* **ADMIN** — any field, any status jump, any time.
* **Author** — ``title`` / ``body`` / ``category`` only, and only while OPEN / READY /
  IN_PROGRESS; anything else is a :class:`FeedbackForbiddenError` naming the reason.
* **Rework** — a body-only PATCH that strictly extends the body (admin or author; anyone
  else is refused before the append is looked at). On a row outside OPEN / READY /
  IN_PROGRESS the SERVER sets the status — the client never sends one (keksdose live
  #331): READY when an admin sends the rework, OPEN otherwise (:func:`rework_status`,
  contract §8.2).
* **The first status** — the server decides it at submit, from the author's role:
  :func:`initial_status` (§8.2). The client never sends a status with a new row.
* **resolved_at** — stamped on a real transition INTO DONE / WONT_DO, cleared on leaving
  them (keksdose feedback #96).
* **CRASH** — never set by hand, by anyone; CRASH → BUG stays an ordinary update.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from eifi1_server_kit.feedback.body import is_rework_append
from eifi1_server_kit.feedback.enums import (
    AUTHOR_EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    FeedbackCategory,
    FeedbackStatus,
)
from eifi1_server_kit.feedback.errors import CrashCategoryNotAssignableError, FeedbackForbiddenError

#: The fields an author may edit while the row is in their lane (keksdose
#: ``feedback_service.py:88``). Status, outcome and resolved_at record what the team decided.
AUTHOR_EDITABLE_FIELDS: frozenset[str] = frozenset({"title", "body", "category"})


def reject_manual_crash(category: FeedbackCategory | str | None) -> None:
    """Refuse CRASH set by hand — on a create or an update, admins included.

    keksdose ``feedback_service.py:23`` ``_reject_manual_crash``: a row wearing the badge
    means the error boundary filed it, so it carries a fingerprint and an ``auto_reported``
    context; the dedupe and the occurrence counter hang off those. Call it BEFORE loading
    the row, so the payload is refused whether or not the row exists.
    """
    if category is not None and FeedbackCategory(category) is FeedbackCategory.CRASH:
        raise CrashCategoryNotAssignableError(
            "CRASH is filed automatically by the crash reporter and cannot be set by hand"
        )


def check_author_edit(
    status: FeedbackStatus | str,
    changes: Mapping[str, Any],
    *,
    is_author: bool,
    reworking: bool,
) -> None:
    """The non-admin half of the lanes (keksdose ``feedback_service.py:152-161``), in its order.

    Raises :class:`FeedbackForbiddenError` with keksdose's wording: ``Not the author``;
    ``Author cannot change: <fields>``; ``Entry is in <STATUS>; only OPEN / READY /
    IN_PROGRESS entries can be edited by the author`` (unless ``reworking``).
    """
    current = FeedbackStatus(status)
    if not is_author:
        raise FeedbackForbiddenError("Not the author")
    forbidden = set(changes) - AUTHOR_EDITABLE_FIELDS
    if forbidden:
        raise FeedbackForbiddenError(f"Author cannot change: {', '.join(sorted(forbidden))}")
    if current not in AUTHOR_EDITABLE_STATUSES and not reworking:
        raise FeedbackForbiddenError(
            f"Entry is in {current.value}; only OPEN / READY / IN_PROGRESS entries can be edited by the author"
        )


def reopens(status: FeedbackStatus | str, *, reworking: bool) -> bool:
    """Does a rework append send this row back to the queue? Yes outside OPEN / READY /
    IN_PROGRESS (keksdose ``:179``); :func:`rework_status` says to which status."""
    return reworking and FeedbackStatus(status) not in AUTHOR_EDITABLE_STATUSES


def initial_status(*, author_is_admin: bool, crash: bool = False) -> FeedbackStatus:
    """The status a new row is filed in, decided by the server at submit (contract §8.2).

    READY for an admin's own report: an admin filing it has triaged it by writing it, so it
    is released for implementation at once (keksdose live #396, Marcel: "my (or admin
    feedback) will be right away persisted with ready to implement"). OPEN for everyone
    else's — filed, not yet triaged. A crash report is OPEN whoever's session it came
    from: nobody has looked at it yet.
    """
    if crash:
        return FeedbackStatus.OPEN
    return FeedbackStatus.READY if author_is_admin else FeedbackStatus.OPEN


def rework_status(*, actor_is_admin: bool) -> FeedbackStatus:
    """Where a rework append sends a row back to (contract §3.4, §8.2): READY when an admin
    sends it — an admin's rework is already triaged — and OPEN otherwise, because a user's
    rework is new input for the triager to look at."""
    return FeedbackStatus.READY if actor_is_admin else FeedbackStatus.OPEN


def resolved_at_change(
    old_status: FeedbackStatus | str,
    new_status: FeedbackStatus | str | None,
) -> Literal["stamp", "clear"] | None:
    """What a status write does to ``resolved_at`` (keksdose ``feedback_service.py:185-194``).

    ``"stamp"`` on a genuine transition INTO DONE / WONT_DO (DONE → DONE and DONE → WONT_DO
    keep the original date), ``"clear"`` on leaving them (a rework back to OPEN, the
    DONE → IN_EVALUATION back-step), ``None`` otherwise — including when no status was sent.
    """
    if new_status is None:
        return None
    old, new = FeedbackStatus(old_status), FeedbackStatus(new_status)
    if new in TERMINAL_STATUSES and old not in TERMINAL_STATUSES:
        return "stamp"
    if new not in TERMINAL_STATUSES and old in TERMINAL_STATUSES:
        return "clear"
    return None


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """What to write onto the row: ``changes`` is the sent fields, plus the server's own
    ``status`` (a rework sent back to the queue — :func:`rework_status`; ``reopened``
    says it happened) and ``resolved_at`` (a stamp or a clear) when they apply."""

    changes: dict[str, Any]
    reworking: bool
    reopened: bool


def plan_update(
    *,
    status: FeedbackStatus | str,
    body: str | None,
    changes: Mapping[str, Any],
    is_admin: bool,
    is_author: bool,
    now: datetime | None = None,
) -> UpdatePlan:
    """The whole PATCH rule for one row, as data (keksdose ``update_feedback``).

    ``status`` / ``body`` are the row's STORED values; ``changes`` is what the client sent
    (``payload.model_dump(exclude_unset=True)``) and is not mutated. Raises
    :class:`CrashCategoryNotAssignableError` (→ 422) or :class:`FeedbackForbiddenError`
    (→ 403); otherwise every key of :attr:`UpdatePlan.changes` is a column to assign.
    ``now`` stamps ``resolved_at`` (default: the current UTC time).
    """
    reject_manual_crash(changes.get("category"))
    current = FeedbackStatus(status)
    # Read before anything is written: a question about the edit, not about the actor.
    reworking = is_rework_append(body, changes)
    if not is_admin:
        check_author_edit(current, changes, is_author=is_author, reworking=reworking)

    planned = dict(changes)
    # The server re-queues, whoever sent the rework: to READY for an admin's (already
    # triaged), to OPEN for anyone else's (§8.2). An admin who means to annotate without
    # re-queueing sends a status with the body — then this is not an append at all and the
    # status they sent wins by construction (keksdose test_feedback.py:351).
    reopened = reopens(current, reworking=reworking)
    if reopened:
        planned["status"] = rework_status(actor_is_admin=is_admin)

    change = resolved_at_change(current, planned.get("status"))
    if change == "stamp":
        planned["resolved_at"] = now or datetime.now(UTC)
    elif change == "clear":
        planned["resolved_at"] = None
    return UpdatePlan(changes=planned, reworking=reworking, reopened=reopened)
