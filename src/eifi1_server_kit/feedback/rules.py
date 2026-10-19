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
* **Author** — ``title`` / ``body`` / ``category`` / ``public`` only, and only while OPEN /
  READY / IN_PROGRESS; anything else is a :class:`FeedbackForbiddenError` naming the
  reason. ``public`` alone at any status on a ``filed_private`` report (§9.3: the
  opt-out of a report the round-0.38 migration made public).
* **Rework** (round 0.40, §10.3) — an entry of its own, ``POST /feedback/{id}/rework``,
  the report's author's only: :func:`plan_rework`. The server sets the status — REWORK
  when an admin sends it, OPEN otherwise (:func:`rework_status`, §10.2 decision 5).
* **The legacy append** — a body-only PATCH that strictly extends the body (admin or
  author; anyone else is refused before the append is looked at), the rework of a client
  from before ui-kit 0.40. On a row outside OPEN / READY / IN_PROGRESS it is a
  :class:`LegacyReworkAppendError` (coded 422, since 0.16; 0.13–0.15 converted it into an
  entry). Inside that lane it is an ordinary body edit.
* **The first status** — the server decides it at submit, from the author's role:
  :func:`initial_status` (§8.2). The client never sends a status with a new row.
* **resolved_at** — stamped on a real transition INTO DONE / WONT_DO, cleared on leaving
  them (keksdose feedback #96).
* **CRASH** — never set by hand, by anyone; CRASH → BUG stays an ordinary update.
* **A reply** (§9.7) — :func:`plan_reply`: the status it sets, with the same
  ``resolved_at`` rule, and the ``status_from`` / ``status_to`` the reply records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from eifi1_server_kit.feedback.body import is_rework_append
from eifi1_server_kit.feedback.enums import (
    AUTHOR_EDITABLE_STATUSES,
    REWORKABLE_STATUSES,
    TERMINAL_STATUSES,
    FeedbackCategory,
    FeedbackStatus,
)
from eifi1_server_kit.feedback.errors import (
    CrashCategoryNotAssignableError,
    FeedbackForbiddenError,
    LegacyReworkAppendError,
)

#: The fields an author may edit while the row is in their lane (keksdose
#: ``feedback_service.py:88``). Status, outcome and resolved_at record what the team decided.
#: ``public`` joined in 0.11 (§9.3): the author may keep their own report private.
AUTHOR_EDITABLE_FIELDS: frozenset[str] = frozenset({"title", "body", "category", "public"})

#: Why anyone but the report's author is refused a rework entry (§10.3): an admin answers
#: with a reply, which offers REWORK as a status.
REWORK_AUTHOR_ONLY = "Only the author sends a report back; an admin replies"


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
    filed_private: bool = False,
) -> None:
    """The non-admin half of the lanes (keksdose ``feedback_service.py:152-161``), in its order.

    Raises :class:`FeedbackForbiddenError` with keksdose's wording: ``Not the author``;
    ``Author cannot change: <fields>``; ``Entry is in <STATUS>; only OPEN / READY /
    IN_PROGRESS entries can be edited by the author`` (unless ``reworking`` — then
    :func:`plan_update` answers the coded 422 — or a ``public``-only change on a
    ``filed_private`` report, §9.3).
    """
    current = FeedbackStatus(status)
    if not is_author:
        raise FeedbackForbiddenError("Not the author")
    forbidden = set(changes) - AUTHOR_EDITABLE_FIELDS
    if forbidden:
        raise FeedbackForbiddenError(f"Author cannot change: {', '.join(sorted(forbidden))}")
    opting_out = filed_private and set(changes) == {"public"}
    if current not in AUTHOR_EDITABLE_STATUSES and not reworking and not opting_out:
        raise FeedbackForbiddenError(
            f"Entry is in {current.value}; only OPEN / READY / IN_PROGRESS entries can be edited by the author"
        )


def reopens(status: FeedbackStatus | str, *, reworking: bool) -> bool:
    """Is this append the legacy rework (keksdose ``:179``)? Yes outside OPEN / READY /
    IN_PROGRESS: :func:`plan_update` refuses it with :class:`LegacyReworkAppendError`."""
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
    """Where a rework sends a row back to (§10.2 decision 5): REWORK when an admin sends
    it — an admin's rework is already triaged, released for work again — and OPEN
    otherwise, because a user's rework is new input for the triager, who releases it to
    REWORK. (Before 0.13 an admin's went to READY.)"""
    return FeedbackStatus.REWORK if actor_is_admin else FeedbackStatus.OPEN


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
    """What to write onto the row: ``changes`` is the sent fields, plus the server's
    ``resolved_at`` (a stamp or a clear) when it applies. ``reworking`` says the PATCH was
    an append — inside the author's lane, the only place one is still accepted."""

    changes: dict[str, Any]
    reworking: bool


def plan_update(
    *,
    status: FeedbackStatus | str,
    body: str | None,
    changes: Mapping[str, Any],
    is_admin: bool,
    is_author: bool,
    now: datetime | None = None,
    filed_private: bool = False,
) -> UpdatePlan:
    """The whole PATCH rule for one row, as data (keksdose ``update_feedback``).

    ``status`` / ``body`` are the row's STORED values; ``changes`` is what the client sent
    (``payload.model_dump(exclude_unset=True)``) and is not mutated. Raises
    :class:`CrashCategoryNotAssignableError` (→ 422), :class:`FeedbackForbiddenError`
    (→ 403) or :class:`LegacyReworkAppendError` (→ coded 422, below); otherwise every key
    of :attr:`UpdatePlan.changes` is a column to assign. ``now`` stamps ``resolved_at``
    (default: the current UTC time). ``filed_private`` is the row's flag (§9.3): its
    author may then switch ``public`` at any status.

    **The legacy append** (§10.3): a rework append on a row outside the author's lane, by
    the author or an admin, is refused with :class:`LegacyReworkAppendError` (since 0.16;
    0.13–0.15 converted it into an entry). An admin who means to annotate sends a status
    with the body — then it is not an append at all (keksdose ``test_feedback.py:351``).
    """
    reject_manual_crash(changes.get("category"))
    current = FeedbackStatus(status)
    # Read before anything is written: a question about the edit, not about the actor.
    reworking = is_rework_append(body, changes)
    if not is_admin:
        check_author_edit(current, changes, is_author=is_author, reworking=reworking, filed_private=filed_private)
    if reopens(current, reworking=reworking):
        raise LegacyReworkAppendError()

    planned = dict(changes)
    change = resolved_at_change(current, planned.get("status"))
    if change == "stamp":
        planned["resolved_at"] = now or datetime.now(UTC)
    elif change == "clear":
        planned["resolved_at"] = None
    return UpdatePlan(changes=planned, reworking=reworking)


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    """What a reply does to its report (§9.7): ``changes`` to write onto the row (empty
    when the reply moves no status), and the ``status_from`` / ``status_to`` the reply
    row records (both ``None`` then)."""

    changes: dict[str, Any]
    status_from: FeedbackStatus | None
    status_to: FeedbackStatus | None


def plan_reply(
    *,
    status: FeedbackStatus | str,
    new_status: FeedbackStatus | str | None,
    now: datetime | None = None,
) -> ReplyPlan:
    """A reply's status move, as data (§9.7). ``status`` is the row's stored one,
    ``new_status`` what the reply chose; ``None`` changes nothing.

    With a status, ``changes`` holds it and ``resolved_at`` by :func:`resolved_at_change`
    (as :func:`plan_update`), and the reply records the move — also onto the status the
    row already has, which is how a reply answers the nudge after a status-only move into
    a decided status. CRASH is a category, not a status, so nothing is refused here.
    """
    if new_status is None:
        return ReplyPlan(changes={}, status_from=None, status_to=None)
    current, target = FeedbackStatus(status), FeedbackStatus(new_status)
    changes: dict[str, Any] = {"status": target}
    change = resolved_at_change(current, target)
    if change == "stamp":
        changes["resolved_at"] = now or datetime.now(UTC)
    elif change == "clear":
        changes["resolved_at"] = None
    return ReplyPlan(changes=changes, status_from=current, status_to=target)


def plan_rework(
    *,
    status: FeedbackStatus | str,
    is_author: bool,
    actor_is_admin: bool,
    now: datetime | None = None,
) -> ReplyPlan:
    """A rework entry's move, as data (§10.3): the report's author only — anyone else,
    an admin included, is a :class:`FeedbackForbiddenError` :data:`REWORK_AUTHOR_ONLY` —
    on a row in :data:`~eifi1_server_kit.feedback.REWORKABLE_STATUSES` (else 403 "Entry is
    in <STATUS>; …"). The row goes to :func:`rework_status` (REWORK for an admin author,
    OPEN otherwise), ``resolved_at`` cleared on leaving DONE / WONT_DO, and the entry
    records ``status_from`` / ``status_to``."""
    if not is_author:
        raise FeedbackForbiddenError(REWORK_AUTHOR_ONLY)
    current = FeedbackStatus(status)
    if current not in REWORKABLE_STATUSES:
        raise FeedbackForbiddenError(f"Entry is in {current.value}; only answered entries can be sent back for rework")
    return plan_reply(status=current, new_status=rework_status(actor_is_admin=actor_is_admin), now=now)
