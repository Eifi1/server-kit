"""Crash filing (contract §3.6): the fingerprint, the row's text, fold-or-file.

The GOLDEN VECTORS below were computed by running keksdose's own ``feedback_service._normalise``,
``_fingerprint``, ``_crash_title`` and ``_crash_body`` (keksdose ``main`` working tree,
2026-10-04) on these exact inputs. If one of them fails, the kit's fingerprint has drifted
and every crash row open in keksdose would stop folding when it switches — fix the code,
never the vector.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eifi1_server_kit.feedback import (
    MAX_TITLE_LENGTH,
    CrashDecision,
    CrashReportCreate,
    FeedbackStatus,
    crash_body,
    crash_context,
    crash_fingerprint,
    crash_reference,
    crash_seen_at,
    crash_title,
    decide_crash,
    fold_crash_context,
    normalise_crash_text,
    truncate,
)

STACK = (
    "TypeError: fx.getRate is not a function\n"
    "  at AccountsPage (index-A1b2C3.js:120:9)\n"
    "  at renderWithHooks\n"
    "  at deeper (x.js:1:1)"
)

GOLDEN_NORMALISE = {
    "Loading chunk 1234 failed": "Loading chunk # failed",
    "index-A1b2C3.js:120:9": "index-#.js:#:#",
    "  a \n\t b  ": "a b",
    "uuid 3fae6774-1234-4abc-9def-0123456789ab": "uuid #-#-#abc-#def-#",
    "abcdef": "#",
    "abcde 12345": "abcde #",
    "x_abcdef12_y": "x_abcdef#_y",
    "café 0xDEADBEEF": "café #xDEADBEEF",
    "": "",
    " nbsp em": "nbsp em",
}

#: (label, user id, payload, keksdose's fingerprint, keksdose's title)
GOLDEN_CRASHES: list[tuple[str, int | str, dict[str, Any], str, str]] = [
    (
        "basic",
        7,
        {
            "name": "TypeError",
            "message": "fx.getRate is not a function",
            "stack": STACK,
            "route": "/accounts",
            "version": "0.4.26",
        },
        "712e959886e7718b4bbc3833bd157a2e",
        "[crash] TypeError: fx.getRate is not a function",
    ),
    (
        "chunk_1234",
        7,
        {"message": "Loading chunk 1234 failed", "route": "/accounts", "version": "0.4.26", "name": "ChunkLoadError"},
        "2d7bafb88f0a1554ef24071e705b7573",
        "[crash] ChunkLoadError: Loading chunk 1234 failed",
    ),
    (
        "chunk_5678",
        7,
        {"message": "Loading chunk 5678 failed", "route": "/accounts", "version": "0.4.26", "name": "ChunkLoadError"},
        "2d7bafb88f0a1554ef24071e705b7573",
        "[crash] ChunkLoadError: Loading chunk 5678 failed",
    ),
    ("minimal", 1, {"message": "boom"}, "eb1ab2c9ec2c3fcaa009e03ebea25116", "[crash] Error: boom"),
    (
        "unicode_ws",
        42,
        {
            "name": "RangeError",
            "message": "  Invalid   time\tvalue 2026-10-04 deadBEEF99 ü ",
            "stack": "RangeError: x\r\n at a (b-0123abcdef.js:9:9)\n\n at c",
            "route": "/r",
            "version": "1.2.3",
        },
        "de1e854c8c25a858964b7d91f21b07ff",
        "[crash] RangeError: Invalid   time\tvalue 2026-10-04 deadBEEF99 ü",
    ),
    (
        "surrogate",
        3,
        {"name": "TypeError\udfff", "message": "boom \ud800 tail", "route": "/a\x00b"},
        "3b731836d754f17c8b3e7144b1b79bec",
        "[crash] TypeError?: boom ? tail",
    ),
    ("user_str", "abc", {"message": "x", "route": "/"}, "2189a8b77639119f12e0fa8aeef6a081", "[crash] Error: x"),
]

GOLDEN_BODY = (
    "TypeError: fx.getRate is not a function\n\n-- Diagnostics --\n"
    "Boundary:    page (a single page went down)\nRoute:       /accounts\n"
    "URL:         https://keksdose.app/accounts\nOccurred at: 2026-10-04T09:12:00+00:00\n"
    "App version: 0.4.26\nViewport:    412x915\nOnline:      yes\n"
    "User:        erika@example.test (#7)\nUser agent:  UA\n"
    "Fingerprint: 712e959886e7718b4bbc3833bd157a2e\n\n-- Stack --\n"
    + STACK
    + "\n\n-- Component stack --\n  at AccountsPage"
)


def _crash(**overrides: Any) -> CrashReportCreate:
    data: dict[str, Any] = {
        "name": "TypeError",
        "message": "fx.getRate is not a function",
        "stack": "TypeError: fx.getRate is not a function\n  at AccountsPage (index-A1b2C3.js:120:9)",
        "boundary": "page",
        "route": "/accounts",
        "version": "0.4.26",
    }
    data.update(overrides)
    return CrashReportCreate.model_validate(data)


# ── golden vectors ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected"), GOLDEN_NORMALISE.items())
def test_normalise_is_keksdoses(text: str, expected: str) -> None:
    assert normalise_crash_text(text) == expected


@pytest.mark.parametrize(("label", "user_id", "payload", "fingerprint", "title"), GOLDEN_CRASHES)
def test_fingerprint_and_title_are_byte_identical_to_keksdose(
    label: str, user_id: int | str, payload: dict[str, Any], fingerprint: str, title: str
) -> None:
    crash = CrashReportCreate.model_validate(payload)
    assert crash_fingerprint(crash, user_id) == fingerprint, label
    assert crash_title(crash) == title, label


def test_the_body_is_byte_identical_to_keksdose() -> None:
    crash = CrashReportCreate.model_validate(
        {
            "name": "TypeError",
            "message": "fx.getRate is not a function",
            "stack": STACK,
            "route": "/accounts",
            "version": "0.4.26",
            "url": "https://keksdose.app/accounts",
            "viewport": "412x915",
            "ua": "UA",
            "online": True,
            "component_stack": "  at AccountsPage",
            "occurred_at": "2026-10-04T09:12:00Z",
        }
    )
    fingerprint = crash_fingerprint(crash, 7)
    assert fingerprint == "712e959886e7718b4bbc3833bd157a2e"
    body = crash_body(crash, user_id=7, user_email="erika@example.test", fingerprint=fingerprint)
    assert body == GOLDEN_BODY


# ── the rules behind the vectors (keksdose test_feedback_service.py) ────────


def test_messages_differing_only_in_numbers_fold_together() -> None:
    """keksdose ``:210``: a bad deploy makes every visitor report "Loading chunk <n> failed"."""
    a = _crash(message="Loading chunk 1234 failed", stack=None)
    b = _crash(message="Loading chunk 5678 failed", stack=None)
    assert crash_fingerprint(a, 1) == crash_fingerprint(b, 1)


def test_the_fingerprint_separates_distinct_defects() -> None:
    """keksdose ``:227``: the route and the error type are part of the identity."""
    base = crash_fingerprint(_crash(), 1)
    assert crash_fingerprint(_crash(route="/reports"), 1) != base
    assert crash_fingerprint(_crash(name="RangeError", message="Invalid time value"), 1) != base
    assert crash_fingerprint(_crash(version="0.4.27"), 1) != base


def test_the_fingerprint_is_per_user() -> None:
    """keksdose ``:240``: two people on the same broken page are two reports."""
    assert crash_fingerprint(_crash(), 1) != crash_fingerprint(_crash(), 2)
    # An int id and its string are the same user.
    assert crash_fingerprint(_crash(), 7) == crash_fingerprint(_crash(), "7")


def test_only_the_top_three_frames_take_part() -> None:
    top = "Error: x\n at a (a.js:1:1)\n at b (b.js:2:2)"
    assert crash_fingerprint(_crash(stack=top + "\n at c"), 1) == crash_fingerprint(_crash(stack=top + "\n at z"), 1)
    # …normalised, so a rebuild's new line numbers mint nothing new.
    assert crash_fingerprint(_crash(stack="E\n at a (a.js:10:1)"), 1) == crash_fingerprint(
        _crash(stack="E\n at a (a.js:99:7)"), 1
    )


def test_origin_and_environment_take_no_part() -> None:
    """Adding them to the payload (§3.6) must not move any existing fingerprint."""
    assert crash_fingerprint(_crash(origin="https://dev.x", environment="dev"), 1) == crash_fingerprint(_crash(), 1)


def test_the_title_never_exceeds_the_column_and_is_never_blank() -> None:
    """keksdose ``:252`` / ``test_feedback_crash.py:97``."""
    long = crash_title(_crash(message="x" * 2000))
    assert len(long) == MAX_TITLE_LENGTH and long.endswith("…")
    assert crash_title(_crash(name=None, message="   ")) == "[crash] Error: (no message)"
    assert crash_title(_crash(name="  ")).startswith("[crash] Error:")


def test_the_body_carries_everything_to_reproduce() -> None:
    """keksdose ``test_feedback_crash.py:59``/``:97``: the full message reaches the body even
    when the title is cut; the unknowns say so."""
    crash = _crash(message="x" * 2000, boundary="app", online=False, stack=None)
    body = crash_body(crash, user_id=7, user_email="a@x.x", fingerprint="f" * 32, seen_at="2026-10-04T09:12:00+00:00")
    assert "x" * 2000 in body
    assert "Boundary:    app (the whole app shell went down)" in body
    assert "Online:      no" in body
    assert "(no stack captured)" in body and "(no component stack captured)" in body
    assert "URL:         (not captured)" in body and "User agent:  (not captured)" in body
    assert "User:        a@x.x (#7)" in body
    unknown = crash_body(_crash(online=None, route=None, version=None), user_id=1, user_email="a@x.x", fingerprint="f")
    assert "Online:      (unknown)" in unknown and "Route:       (unknown)" in unknown


def test_seen_at_is_the_clients_time_or_now() -> None:
    at = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
    assert crash_seen_at(_crash(occurred_at=at.isoformat())) == "2026-10-04T09:12:00+00:00"
    assert crash_seen_at(_crash(), now=at) == "2026-10-04T09:12:00+00:00"
    assert crash_seen_at(_crash()).endswith("+00:00")


def test_a_new_row_context_has_the_dialogs_keys_and_the_crash_keys() -> None:
    """keksdose ``test_feedback_crash.py:59``: the same key set the manual dialog attaches,
    origin and environment included, so a dev crash wears the DEV chip."""
    crash = _crash(
        viewport="412x915",
        ua="UA",
        url="https://dev.keksdose.app/accounts",
        origin="https://dev.keksdose.app",
        environment="dev",
        online=True,
    )
    fingerprint = crash_fingerprint(crash, 7)
    context = crash_context(
        crash,
        user_id=7,
        user_email="admin@x.x",
        user_display_name="Admin",
        fingerprint=fingerprint,
        seen_at="2026-10-04T09:12:00+00:00",
    )
    assert context == {
        "url": "https://dev.keksdose.app/accounts",
        "route": "/accounts",
        "origin": "https://dev.keksdose.app",
        "environment": "dev",
        "user_id": 7,
        "user_email": "admin@x.x",
        "user_display_name": "Admin",
        "viewport": "412x915",
        "ua": "UA",
        "version": "0.4.26",
        "fingerprint": fingerprint,
        "boundary": "page",
        "online": True,
        "occurrences": 1,
        "first_seen_at": "2026-10-04T09:12:00+00:00",
        "last_seen_at": "2026-10-04T09:12:00+00:00",
        "auto_reported": True,
    }
    assert (
        crash_context(_crash(), user_id=1, user_email=None, user_display_name=None, fingerprint="f", seen_at="t")[
            "environment"
        ]
        == ""
    )


def test_the_reference_is_the_first_eight_characters() -> None:
    assert crash_reference("712e959886e7718b4bbc3833bd157a2e") == "712e9598"
    assert crash_reference(None) is None and crash_reference("") is None


# ── fold or file (keksdose record_crash) ────────────────────────────────────


def test_a_repeat_folds_into_a_row_still_in_the_lane() -> None:
    """keksdose ``test_feedback_service.py:348``: anything short of terminal folds, IN_EVALUATION included."""
    for status in FeedbackStatus:
        expected = CrashDecision.FILE if status in (FeedbackStatus.DONE, FeedbackStatus.WONT_DO) else CrashDecision.FOLD
        assert decide_crash(status) is expected, status
        assert decide_crash(status.value) is expected


def test_no_candidate_or_a_settled_one_files_a_new_row() -> None:
    """keksdose ``test_feedback_service.py:261`` / ``test_feedback_crash.py:139``: a fixed crash
    coming back is news."""
    assert decide_crash(None) is CrashDecision.FILE
    assert decide_crash(FeedbackStatus.DONE) is CrashDecision.FILE
    assert decide_crash(FeedbackStatus.WONT_DO) is CrashDecision.FILE


def test_folding_counts_up_in_a_new_dict() -> None:
    """keksdose ``test_feedback_crash.py:114``: the counter only survives because ``context`` is
    REASSIGNED, never mutated — and it keeps counting rather than resetting."""
    first = {"occurrences": 1, "first_seen_at": "t0", "last_seen_at": "t0", "route": "/a"}
    second = fold_crash_context(first, "t1")
    third = fold_crash_context(second, "t2")
    assert first == {"occurrences": 1, "first_seen_at": "t0", "last_seen_at": "t0", "route": "/a"}
    assert second is not first and second["occurrences"] == 2 and second["last_seen_at"] == "t1"
    assert third["occurrences"] == 3 and third["first_seen_at"] == "t0" and third["route"] == "/a"
    # A row without a counter (an older one) was the first occurrence.
    assert fold_crash_context(None, "t")["occurrences"] == 2
    assert fold_crash_context({"occurrences": None}, "t")["occurrences"] == 2


def test_truncate() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("abcd", 3) == "ab…"
