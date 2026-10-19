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
    LEGACY_REWORK_APPEND,
    LEGACY_REWORK_APPEND_DETAIL,
    REWORKABLE_STATUSES,
    TERMINAL_STATUSES,
    CrashCategoryNotAssignableError,
    FeedbackCategory,
    FeedbackForbiddenError,
    FeedbackStatus,
    FeedbackUpdate,
    FeedbackValidationError,
    LegacyReworkAppendError,
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
        assert plan.changes == changes and not plan.reworking


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


def test_an_old_clients_rework_append_is_a_coded_422() -> None:
    """Since 0.16 (§10.3, Marcel 2026-10-19): the PATCH append a client from before ui-kit
    0.40 sent as its rework is refused — no entry, no status, nothing written. The coded 422
    tells a tab left open across the deploy to reload; ``POST …/rework`` is the way now."""
    appended = "original\n\n--- REWORK 2026-08-19 12:00 ---\nactually, still broken"
    with pytest.raises(LegacyReworkAppendError, match="reload") as refused:
        _author(S.DONE, {"body": appended}, body="original")
    assert isinstance(refused.value, FeedbackValidationError)
    assert (
        (refused.value.status_code, refused.value.code) == (422, LEGACY_REWORK_APPEND) == (422, "legacy_rework_append")
    )
    assert str(refused.value) == LEGACY_REWORK_APPEND_DETAIL


def test_every_answered_status_refuses_the_append() -> None:
    assert {S.IN_EVALUATION, S.NEEDS_LIVE_TEST, S.POSTPONED, S.DONE, S.WONT_DO, S.REWORK} == REWORKABLE_STATUSES, (
        "the five answered ones and REWORK itself (§10.2 decision 4)"
    )
    for status in REWORKABLE_STATUSES:
        with pytest.raises(LegacyReworkAppendError):
            _author(status, {"body": "b + more"})
    # In the author's lane an append is an ordinary edit: nothing to refuse.
    for status in AUTHOR_EDITABLE_STATUSES:
        plan = _author(status, {"body": "b + more"})
        assert plan.reworking and plan.changes == {"body": "b + more"}


def test_a_settled_entry_still_cannot_be_rewritten_by_its_author() -> None:
    """keksdose ``test_feedback.py:203``: a different text, a truncation, and an append bundled
    with a status are all refused."""
    for changes in ({"body": "something else"}, {"body": "orig"}, {"body": "original + more", "status": S.OPEN}):
        with pytest.raises(FeedbackForbiddenError):
            _author(S.DONE, changes, body="original")


def test_an_admins_append_is_refused_too() -> None:
    """keksdose ``test_feedback.py:310`` (live #331) was an admin's rework by body alone; since
    0.16 an admin's append outside the author's lane is the same coded 422 (an admin sends a
    REWORK reply)."""
    with pytest.raises(LegacyReworkAppendError):
        _admin(S.NEEDS_LIVE_TEST, {"body": "original\n\n--- REWORK 2026-09-16 03:57 ---\nstill"}, body="original")
    with pytest.raises(LegacyReworkAppendError):
        _admin(S.DONE, {"body": "original + more"}, body="original")


def test_an_admin_can_still_annotate_without_re_queueing() -> None:
    """keksdose ``test_feedback.py:351``: a status sent with the body is no append — it wins,
    and the terminal stamp is not disturbed."""
    plan = _admin(S.DONE, {"body": "original\n\n--- REWORK x ---\nfor the record", "status": S.DONE}, body="original")
    assert plan.changes["status"] is S.DONE and "resolved_at" not in plan.changes
    assert not plan.reworking


def test_reopens_alone() -> None:
    assert reopens("DONE", reworking=True)
    assert not reopens("DONE", reworking=False)
    assert not reopens("IN_PROGRESS", reworking=True)
    assert not reopens("READY", reworking=True), "READY is not answered yet: nothing to send back"


# ── READY: the triage step (contract §8.2, keksdose live #396) ──────────────


def test_ready_sits_between_open_and_in_progress() -> None:
    order = list(S)
    assert order[:4] == [S.OPEN, S.READY, S.REWORK, S.IN_PROGRESS] and len(order) == 9
    assert S("READY") is S.READY and S.READY.value == "READY"
    # OPEN waits on the triager now, READY on the implementer (kit FEEDBACK_AWAITING_STATUSES).
    assert {S.OPEN, S.IN_EVALUATION, S.NEEDS_LIVE_TEST} == AWAITING_STATUSES


def test_an_admins_own_report_is_filed_ready_and_everyone_elses_open() -> None:
    assert initial_status(author_is_admin=True) is S.READY
    assert initial_status(author_is_admin=False) is S.OPEN
    # A crash: nobody has looked at it yet, whoever's session it came from.
    assert initial_status(author_is_admin=True, crash=True) is S.OPEN
    assert initial_status(author_is_admin=False, crash=True) is S.OPEN


def test_a_rework_goes_back_to_rework_from_an_admin_and_to_open_otherwise() -> None:
    assert rework_status(actor_is_admin=True) is S.REWORK
    assert rework_status(actor_is_admin=False) is S.OPEN


def test_the_author_may_edit_a_ready_row() -> None:
    plan = _author(S.READY, {"title": "reworded", "body": "b + more"})
    assert plan.changes == {"title": "reworded", "body": "b + more"} and not plan.reworking


def test_the_sent_changes_are_not_mutated() -> None:
    sent: dict[str, Any] = {"status": S.DONE}
    plan = _admin(S.OPEN, sent)
    assert sent == {"status": S.DONE} and plan.changes is not sent and plan.changes["resolved_at"] == NOW


def test_an_admin_may_do_anything_else() -> None:
    plan = _admin(S.WONT_DO, {"title": "t", "body": "rewritten", "category": FeedbackCategory.IDEA, "outcome": None})
    assert plan.changes == {"title": "t", "body": "rewritten", "category": FeedbackCategory.IDEA, "outcome": None}


def test_a_dumped_update_payload_plugs_straight_in() -> None:
    payload = FeedbackUpdate.model_validate({"status": "DONE", "title": "shipped"})
    plan = _admin(S.IN_EVALUATION, payload.model_dump(exclude_unset=True))
    assert plan.changes == {"status": S.DONE, "title": "shipped", "resolved_at": NOW}


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

    plan = plan_update(status=AppStatus.OPEN, body="a", changes={"body": "a b"}, is_admin=False, is_author=True)
    assert plan.changes == {"body": "a b"} and plan.reworking
    with pytest.raises(LegacyReworkAppendError):
        plan_update(status=AppStatus.DONE, body="a", changes={"body": "a b"}, is_admin=False, is_author=True)
    with pytest.raises(CrashCategoryNotAssignableError):
        reject_manual_crash(AppCategory.CRASH)
    reject_manual_crash(AppCategory.BUG)


# ── public feedback (0.11, §9.3, §9.7) ──────────────────────────────────────


def test_the_author_switches_public_while_the_report_is_open() -> None:
    assert _author(S.OPEN, {"public": False}).changes == {"public": False}
    assert _author(S.IN_PROGRESS, {"public": True, "title": "t"}).changes == {"public": True, "title": "t"}
    with pytest.raises(FeedbackForbiddenError, match="Entry is in DONE"):
        _author(S.DONE, {"public": False})
    with pytest.raises(FeedbackForbiddenError, match="Not the author"):
        plan_update(status=S.OPEN, body="b", changes={"public": False}, is_admin=False, is_author=False)
    assert _admin(S.DONE, {"public": False}).changes == {"public": False}


def test_a_filed_private_report_may_be_switched_at_any_status_and_nothing_else() -> None:
    """The opt-out (§9.3): ``public`` alone, at any status, on a report the migration made public."""
    for status in FeedbackStatus:
        plan = plan_update(
            status=status, body="b", changes={"public": False}, is_admin=False, is_author=True, filed_private=True
        )
        assert plan.changes == {"public": False}
    with pytest.raises(FeedbackForbiddenError, match="Entry is in DONE"):
        plan_update(
            status=S.DONE,
            body="b",
            changes={"public": False, "title": "t"},
            is_admin=False,
            is_author=True,
            filed_private=True,
        )
    check_author_edit(S.WONT_DO, {"public": True}, is_author=True, reworking=False, filed_private=True)


def test_a_reply_without_a_status_changes_nothing() -> None:
    from eifi1_server_kit.feedback import ReplyPlan, plan_reply

    assert plan_reply(status=S.OPEN, new_status=None) == ReplyPlan(changes={}, status_from=None, status_to=None)


def test_a_reply_with_a_status_moves_the_report_by_the_resolved_at_rule() -> None:
    from eifi1_server_kit.feedback import plan_reply

    done = plan_reply(status=S.IN_EVALUATION, new_status="DONE", now=NOW)
    assert done.changes == {"status": S.DONE, "resolved_at": NOW}
    assert (done.status_from, done.status_to) == (S.IN_EVALUATION, S.DONE)
    reopened = plan_reply(status=S.WONT_DO, new_status=S.OPEN)
    assert reopened.changes == {"status": S.OPEN, "resolved_at": None}
    # The nudge's answer: a reply onto the status the row already has still records it.
    same = plan_reply(status=S.DONE, new_status=S.DONE)
    assert same.changes == {"status": S.DONE} and same.status_to is S.DONE
    stamped = plan_reply(status=S.OPEN, new_status=S.WONT_DO)
    assert isinstance(stamped.changes["resolved_at"], datetime)


# ── rework as an entry (round 0.40, §10.2, §10.3) ───────────────────────────


def test_rework_is_in_the_queue_and_reworkable_never_awaiting_nor_the_authors() -> None:
    from eifi1_server_kit.feedback import QUEUE_STATUSES

    assert {S.READY, S.REWORK, S.IN_PROGRESS} == QUEUE_STATUSES
    assert S.REWORK in REWORKABLE_STATUSES
    assert S.REWORK not in AWAITING_STATUSES and S.REWORK not in AUTHOR_EDITABLE_STATUSES
    assert S("REWORK") is S.REWORK


def test_the_entry_kinds() -> None:
    from eifi1_server_kit.feedback import FeedbackEntryKind

    assert [kind.value for kind in FeedbackEntryKind] == ["reply", "rework"]


def test_plan_rework_is_the_authors_on_an_answered_report() -> None:
    from eifi1_server_kit.feedback import REWORK_AUTHOR_ONLY, ReplyPlan, plan_rework

    member = plan_rework(status=S.DONE, is_author=True, actor_is_admin=False, now=NOW)
    assert member == ReplyPlan(changes={"status": S.OPEN, "resolved_at": None}, status_from=S.DONE, status_to=S.OPEN)
    admin = plan_rework(status="NEEDS_LIVE_TEST", is_author=True, actor_is_admin=True)
    assert admin == ReplyPlan(changes={"status": S.REWORK}, status_from=S.NEEDS_LIVE_TEST, status_to=S.REWORK)
    again = plan_rework(status=S.REWORK, is_author=True, actor_is_admin=True)
    assert again.status_to is S.REWORK, "a second rework adds an entry"
    for is_admin in (True, False):
        with pytest.raises(FeedbackForbiddenError, match=REWORK_AUTHOR_ONLY):
            plan_rework(status=S.DONE, is_author=False, actor_is_admin=is_admin)
    for status in AUTHOR_EDITABLE_STATUSES:
        with pytest.raises(FeedbackForbiddenError, match=f"Entry is in {status.value}; only answered entries"):
            plan_rework(status=status, is_author=True, actor_is_admin=False)
