"""The PATCH rules (contract §3.4), ported from keksdose's service and API tests.

keksdose's tests drive ``update_feedback`` through a session; here the same cases run on
the pure plan: stored status + body, the sent fields, and two booleans.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eifi1_server_kit.feedback import (
    AUTHOR_EDITABLE_STATUSES,
    AWAITING_STATUSES,
    REWORKABLE_STATUSES,
    TERMINAL_STATUSES,
    CrashCategoryNotAssignableError,
    FeedbackCategory,
    FeedbackForbiddenError,
    FeedbackStatus,
    FeedbackUpdate,
    UpdatePlan,
    check_author_edit,
    initial_status,
    plan_update,
    reject_manual_crash,
    reopens,
    resolved_at_change,
    rework_status,
)

NOW = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
S = FeedbackStatus


def _admin(status: FeedbackStatus, changes: dict[str, Any], body: str = "b") -> UpdatePlan:
    return plan_update(status=status, body=body, changes=changes, is_admin=True, is_author=False, now=NOW)


def _author(status: FeedbackStatus, changes: dict[str, Any], body: str = "b") -> UpdatePlan:
    return plan_update(status=status, body=body, changes=changes, is_admin=False, is_author=True, now=NOW)


# ── resolved_at (keksdose feedback #96) ─────────────────────────────────────


def test_entering_a_terminal_state_stamps_resolved_at() -> None:
    """keksdose ``test_feedback_service.py:46`` and ``:61``: DONE and WONT_DO both stamp."""
    assert _admin(S.IN_PROGRESS, {"status": S.DONE, "outcome": "fixed"}).changes == {
        "status": S.DONE,
        "outcome": "fixed",
        "resolved_at": NOW,
    }
    assert _admin(S.OPEN, {"status": S.WONT_DO}).changes["resolved_at"] == NOW
    assert "resolved_at" not in _admin(S.OPEN, {"status": S.IN_PROGRESS}).changes


def test_leaving_a_terminal_state_clears_resolved_at() -> None:
    """keksdose ``test_feedback_service.py:69``: DONE → OPEN and WONT_DO → IN_EVALUATION."""
    assert _admin(S.DONE, {"status": S.OPEN}).changes["resolved_at"] is None
    assert _admin(S.WONT_DO, {"status": S.IN_EVALUATION}).changes["resolved_at"] is None


def test_a_redundant_terminal_patch_keeps_the_original_date() -> None:
    """keksdose ``test_feedback_service.py:87`` and ``:104``: DONE → DONE, DONE → WONT_DO and an
    outcome-only PATCH leave ``resolved_at`` alone."""
    assert "resolved_at" not in _admin(S.DONE, {"status": S.DONE, "outcome": "noted"}).changes
    assert "resolved_at" not in _admin(S.DONE, {"status": S.WONT_DO}).changes
    assert "resolved_at" not in _admin(S.DONE, {"outcome": "outcome only"}).changes


def test_resolved_at_change_for_every_pair() -> None:
    for old in S:
        assert resolved_at_change(old, None) is None
        for new in S:
            expected = (
                "stamp"
                if new in TERMINAL_STATUSES and old not in TERMINAL_STATUSES
                else "clear"
                if new not in TERMINAL_STATUSES and old in TERMINAL_STATUSES
                else None
            )
            assert resolved_at_change(old.value, new.value) == expected, (old, new)


def test_the_stamp_defaults_to_now_in_utc() -> None:
    plan = plan_update(status=S.OPEN, body="", changes={"status": S.DONE}, is_admin=True, is_author=False)
    stamped = plan.changes["resolved_at"]
    assert isinstance(stamped, datetime) and stamped.tzinfo is UTC


# ── the author's lane ───────────────────────────────────────────────────────


def test_the_author_edits_title_body_and_category_while_open_ready_or_in_progress() -> None:
    """keksdose ``test_feedback_service.py:123`` and ``:141``; READY joins the lane (§8.2)."""
    assert {S.OPEN, S.READY, S.IN_PROGRESS} == AUTHOR_EDITABLE_STATUSES
    changes = {"title": "reworded", "body": "clarified", "category": FeedbackCategory.BUG}
    for status in AUTHOR_EDITABLE_STATUSES:
        plan = _author(status, changes)
        assert plan.changes == changes and not plan.reopened


def test_the_author_cannot_edit_after_in_evaluation() -> None:
    """keksdose ``test_feedback_service.py:154``: from IN_EVALUATION on the body is frozen."""
    with pytest.raises(FeedbackForbiddenError, match=r"Entry is in IN_EVALUATION; only OPEN / READY / IN_PROGRESS"):
        _author(S.IN_EVALUATION, {"body": "too late"}, body="b - longer")
    with pytest.raises(FeedbackForbiddenError):
        _author(S.DONE, {"title": "renamed"})


def test_the_author_cannot_change_status_or_outcome() -> None:
    """keksdose ``test_feedback_service.py:167`` / ``test_feedback.py:121``: no self-resolving."""
    with pytest.raises(FeedbackForbiddenError, match="Author cannot change: status"):
        _author(S.OPEN, {"status": S.DONE})
    with pytest.raises(FeedbackForbiddenError, match="Author cannot change: outcome"):
        _author(S.OPEN, {"outcome": "I self-resolved"})
    with pytest.raises(FeedbackForbiddenError, match="Author cannot change: outcome, status"):
        _author(S.OPEN, {"status": S.DONE, "outcome": "x", "title": "t"})


def test_anyone_else_is_refused_before_the_append_is_looked_at() -> None:
    """keksdose ``test_feedback_service.py:176``: not the author, not an admin → 403, even for
    a perfectly shaped rework append."""
    for status in S:
        with pytest.raises(FeedbackForbiddenError, match="Not the author"):
            plan_update(status=status, body="b", changes={"body": "b + more"}, is_admin=False, is_author=False)


def test_check_author_edit_alone() -> None:
    check_author_edit("OPEN", {"title": "t"}, is_author=True, reworking=False)
    check_author_edit("DONE", {"body": "b + more"}, is_author=True, reworking=True)
    with pytest.raises(FeedbackForbiddenError):
        check_author_edit("DONE", {"body": "x"}, is_author=True, reworking=False)


# ── rework (keksdose P-F9-2, live #331) ─────────────────────────────────────


def test_the_author_reworks_a_settled_entry_by_appending_to_it() -> None:
    """keksdose ``test_feedback.py:165``: back in the queue, and the terminal stamp cleared."""
    appended = "original\n\n--- REWORK 2026-08-19 12:00 ---\nactually, still broken"
    plan = _author(S.DONE, {"body": appended}, body="original")
    assert plan.reworking and plan.reopened
    assert plan.changes == {"body": appended, "status": S.OPEN, "resolved_at": None}


def test_every_answered_status_reopens_on_a_rework() -> None:
    assert {S.IN_EVALUATION, S.NEEDS_LIVE_TEST, S.POSTPONED, S.DONE, S.WONT_DO} == REWORKABLE_STATUSES, (
        "the same five as before READY (§8.2)"
    )
    for status in REWORKABLE_STATUSES:
        plan = _author(status, {"body": "b + more"})
        assert plan.changes["status"] is S.OPEN, status
        assert ("resolved_at" in plan.changes) == (status in TERMINAL_STATUSES)
    # In the author's lane an append is an ordinary edit: nothing to reopen.
    for status in AUTHOR_EDITABLE_STATUSES:
        plan = _author(status, {"body": "b + more"})
        assert plan.reworking and not plan.reopened and "status" not in plan.changes


def test_a_settled_entry_still_cannot_be_rewritten_by_its_author() -> None:
    """keksdose ``test_feedback.py:203``: a different text, a truncation, and an append bundled
    with a status are all refused."""
    for changes in ({"body": "something else"}, {"body": "orig"}, {"body": "original + more", "status": S.OPEN}):
        with pytest.raises(FeedbackForbiddenError):
            _author(S.DONE, changes, body="original")


def test_an_admins_rework_goes_back_to_ready_without_being_told_to() -> None:
    """keksdose ``test_feedback.py:310`` (live #331): body alone, from an admin, on a parked
    entry. An admin's rework is already triaged, so it is READY, not OPEN (§8.2)."""
    plan = _admin(S.NEEDS_LIVE_TEST, {"body": "original\n\n--- REWORK 2026-09-16 03:57 ---\nstill"}, body="original")
    assert plan.changes["status"] is S.READY and plan.reopened
    done = _admin(S.DONE, {"body": "original + more"}, body="original")
    assert done.changes == {"body": "original + more", "status": S.READY, "resolved_at": None}


def test_an_admin_can_still_annotate_without_re_queueing() -> None:
    """keksdose ``test_feedback.py:351``: a status sent with the body is no append — it wins,
    and the terminal stamp is not disturbed."""
    plan = _admin(S.DONE, {"body": "original\n\n--- REWORK x ---\nfor the record", "status": S.DONE}, body="original")
    assert plan.changes["status"] is S.DONE and "resolved_at" not in plan.changes
    assert not plan.reworking and not plan.reopened


def test_reopens_alone() -> None:
    assert reopens("DONE", reworking=True)
    assert not reopens("DONE", reworking=False)
    assert not reopens("IN_PROGRESS", reworking=True)
    assert not reopens("READY", reworking=True), "READY is not answered yet: nothing to send back"


# ── READY: the triage step (contract §8.2, keksdose live #396) ──────────────


def test_ready_sits_between_open_and_in_progress() -> None:
    order = list(S)
    assert order[:3] == [S.OPEN, S.READY, S.IN_PROGRESS] and len(order) == 8
    assert S("READY") is S.READY and S.READY.value == "READY"
    # OPEN waits on the triager now, READY on the implementer (kit FEEDBACK_AWAITING_STATUSES).
    assert {S.OPEN, S.IN_EVALUATION, S.NEEDS_LIVE_TEST} == AWAITING_STATUSES


def test_an_admins_own_report_is_filed_ready_and_everyone_elses_open() -> None:
    assert initial_status(author_is_admin=True) is S.READY
    assert initial_status(author_is_admin=False) is S.OPEN
    # A crash: nobody has looked at it yet, whoever's session it came from.
    assert initial_status(author_is_admin=True, crash=True) is S.OPEN
    assert initial_status(author_is_admin=False, crash=True) is S.OPEN


def test_a_rework_goes_back_to_ready_from_an_admin_and_to_open_otherwise() -> None:
    assert rework_status(actor_is_admin=True) is S.READY
    assert rework_status(actor_is_admin=False) is S.OPEN


def test_the_author_may_edit_a_ready_row() -> None:
    plan = _author(S.READY, {"title": "reworded", "body": "b + more"})
    assert plan.changes == {"title": "reworded", "body": "b + more"} and not plan.reopened


def test_the_sent_changes_are_not_mutated() -> None:
    sent: dict[str, Any] = {"body": "b + more"}
    plan = _author(S.DONE, sent)
    assert sent == {"body": "b + more"} and plan.changes is not sent


def test_an_admin_may_do_anything_else() -> None:
    plan = _admin(S.WONT_DO, {"title": "t", "body": "rewritten", "category": FeedbackCategory.IDEA, "outcome": None})
    assert plan.changes == {"title": "t", "body": "rewritten", "category": FeedbackCategory.IDEA, "outcome": None}


def test_a_dumped_update_payload_plugs_straight_in() -> None:
    payload = FeedbackUpdate.model_validate({"status": "DONE", "outcome": "shipped"})
    plan = _admin(S.IN_EVALUATION, payload.model_dump(exclude_unset=True))
    assert plan.changes == {"status": S.DONE, "outcome": "shipped", "resolved_at": NOW}


# ── CRASH (keksdose feedback #160) ──────────────────────────────────────────


def test_crash_cannot_be_set_by_hand_by_anyone() -> None:
    """keksdose ``test_feedback_service.py:309`` / ``test_feedback_crash.py:247``: refused for the
    admin too — the crash reporter is the only way in; refused even for a missing row."""
    for category in (FeedbackCategory.CRASH, "CRASH"):
        with pytest.raises(CrashCategoryNotAssignableError, match="cannot be set by hand"):
            reject_manual_crash(category)
    for admin in (True, False):
        with pytest.raises(CrashCategoryNotAssignableError):
            plan_update(status=S.OPEN, body="b", changes={"category": "CRASH"}, is_admin=admin, is_author=True)
    reject_manual_crash(None)
    reject_manual_crash(FeedbackCategory.BUG)


def test_a_genuine_crash_row_can_still_be_recategorised() -> None:
    """keksdose ``test_feedback_service.py:334``: CRASH → BUG stays an ordinary admin update."""
    assert _admin(S.OPEN, {"category": FeedbackCategory.BUG}).changes == {"category": FeedbackCategory.BUG}


def test_the_refusals_carry_their_status_and_are_value_errors() -> None:
    """Apps map them (contract §3.7); a ValueError inside a Pydantic validator is a 422 there."""
    assert CrashCategoryNotAssignableError.status_code == 422
    assert FeedbackForbiddenError.status_code == 403
    assert issubclass(FeedbackForbiddenError, ValueError)


def test_an_apps_own_enum_classes_work_unchanged() -> None:
    """An app that keeps its own ``(str, Enum)`` classes (or a VARCHAR column's plain string)
    can hand its values straight in — the kit compares by value."""
    import enum

    class AppStatus(str, enum.Enum):
        OPEN = "OPEN"
        DONE = "DONE"

    class AppCategory(str, enum.Enum):
        CRASH = "CRASH"
        BUG = "BUG"

    plan = plan_update(status=AppStatus.DONE, body="a", changes={"body": "a b"}, is_admin=False, is_author=True)
    assert plan.changes == {"body": "a b", "status": S.OPEN, "resolved_at": None}
    with pytest.raises(CrashCategoryNotAssignableError):
        reject_manual_crash(AppCategory.CRASH)
    reject_manual_crash(AppCategory.BUG)
