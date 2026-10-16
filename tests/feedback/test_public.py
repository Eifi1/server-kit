"""Public feedback's layer-1 rules (feedback-harmonization §9.4, §9.3, §9.11)."""

from __future__ import annotations

import pytest

from eifi1_server_kit.feedback import (
    COMMENT_MAX,
    DECIDED_STATUSES,
    FEEDBACK_ANSWERED,
    FEEDBACK_COMMENT,
    PUBLIC_COMMENT_FIELDS,
    PUBLIC_REPLY_FIELDS,
    PUBLIC_REPORT_FIELDS,
    REMOVED_URL,
    REPLY_MAX,
    SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    FeedbackCategory,
    FeedbackStatus,
    feedback_recipients,
    is_public,
    public_body,
    public_text,
)

POLICY = AttachmentUrlPolicy(SHA12_KEY_PATTERN)
SHOT = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"
LOG = "/api/v1/feedback/attachments/bbbbbbbbbbbb.txt"


def test_the_screenshot_lines_go() -> None:
    body = f"Sync is slow.\n[screenshot] {SHOT}\nAfter that, nothing.\n  [screenshot] {LOG}  "
    assert public_body(body, policy=POLICY) == "Sync is slow.\nAfter that, nothing."


def test_a_pasted_url_mid_line_goes_wherever_it_stands() -> None:
    body = f"See {SHOT} and https://keksdose.example{LOG}, twice."
    assert public_body(body, policy=POLICY) == f"See {REMOVED_URL} and {REMOVED_URL}, twice."


def test_a_url_in_a_rework_note_goes_and_the_rework_lines_stay() -> None:
    body = f"Slow.\n\n--- REWORK 2026-10-04 09:12 ---\nStill slow, see {SHOT}\n[screenshot] {LOG}"
    assert (
        public_body(body, policy=POLICY) == f"Slow.\n\n--- REWORK 2026-10-04 09:12 ---\nStill slow, see {REMOVED_URL}"
    )


def test_a_foreign_prefix_and_a_key_outside_the_policy_are_kept() -> None:
    body = "See /api/v1/other/attachments/aaaaaaaaaaaa.png and /api/v1/feedback/attachments/NOT-A-KEY"
    assert public_body(body, policy=POLICY) == body


def test_kastlans_folded_header_loses_the_name() -> None:
    body = (
        "Leak in the cellar.\n\n--- COMMENT 2026-08-11 10:02 · anna@example.com ---\nStill dripping.\n"
        "--- COMMENT · Anna Muster ---\nAnd now?\n--- COMMENT 2026-08-12 ---\nno name"
    )
    assert public_body(body, policy=POLICY) == (
        "Leak in the cellar.\n\n--- COMMENT 2026-08-11 10:02 ---\nStill dripping.\n"
        "--- COMMENT ---\nAnd now?\n--- COMMENT 2026-08-12 ---\nno name"
    )


def test_public_text_removes_urls_only_and_takes_none() -> None:
    assert public_text(f"[screenshot] {SHOT}", policy=POLICY) == f"[screenshot] {REMOVED_URL}"
    assert public_text(None) == "" and public_body(None) == ""
    # The default policy's opaque key.
    assert public_text("x /api/v1/feedback/attachments/a_b-c.pdf y") == f"x {REMOVED_URL} y"


@pytest.mark.parametrize("category", list(FeedbackCategory))
def test_a_crash_is_never_public(category: FeedbackCategory) -> None:
    assert is_public(category, True) is (category is not FeedbackCategory.CRASH)
    assert is_public(category.value, False) is False
    assert is_public(category, None) is False


def test_the_recipients_are_the_author_then_the_commenters_never_the_writer() -> None:
    assert feedback_recipients(author_id=1, commenter_ids=[3, 2, 3, None, 1], writer_id=2) == [1, 3]
    assert feedback_recipients(author_id=None, commenter_ids=[3, 4], writer_id=3) == [4]
    assert feedback_recipients(author_id=5, commenter_ids=[], writer_id=5) == []


def test_the_constants_are_the_contracts() -> None:
    assert (FEEDBACK_ANSWERED, FEEDBACK_COMMENT) == ("feedback_answered", "feedback_comment")
    assert (COMMENT_MAX, REPLY_MAX) == (4000, 8000)
    assert {FeedbackStatus.DONE, FeedbackStatus.WONT_DO, FeedbackStatus.POSTPONED} == DECIDED_STATUSES
    never = {"user_id", "user_email", "context", "screenshot_url", "attachment_urls", "crash_fingerprint"}
    never |= {"public", "filed_private", "hidden_by", "status_from", "outcome"}
    for fields in (PUBLIC_REPORT_FIELDS, PUBLIC_COMMENT_FIELDS, PUBLIC_REPLY_FIELDS):
        assert not fields & never
    assert {"id", "author_name", "body", "status_to", "created_at", "kind"} == PUBLIC_REPLY_FIELDS
    assert "attachment_url" not in PUBLIC_REPLY_FIELDS, "a rework's file never leaves for others (§10.3)"
