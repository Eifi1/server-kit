"""Rework rounds and file lines in the body (contract §3.4) — the kit's TS helpers' twins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from eifi1_server_kit.feedback import (
    append_rework,
    appended_text,
    attachment_line,
    body_attachment_urls,
    is_rework_append,
    opens_with_rework_rule,
    rework_count,
    rework_stamp,
    split_body_attachments,
)

NOW = datetime(2026, 10, 4, 9, 12, 45, tzinfo=UTC)
URL = "/api/v1/feedback/attachments/1ed84e1bc3c7.webp"


def test_the_appended_shape_is_fixed() -> None:
    """``<old body>\\n\\n--- REWORK 2026-10-04 09:12 ---\\n<note>\\n[screenshot] <url>`` (§3.4)."""
    assert append_rework("original", "  still broken \n", URL, now=NOW) == (
        f"original\n\n--- REWORK 2026-10-04 09:12 ---\nstill broken\n[screenshot] {URL}"
    )
    # No file: no file line.
    assert append_rework("original", "again", now=NOW) == "original\n\n--- REWORK 2026-10-04 09:12 ---\nagain"
    # `\n\n` is left out when the old body is "".
    assert append_rework("", "first words", now=NOW) == "--- REWORK 2026-10-04 09:12 ---\nfirst words"


def test_the_note_is_required_and_the_file_must_be_ours() -> None:
    """A file alone cannot be sent (kit ``appendRework`` → null; keksdose ``feedback-page.tsx:577``)."""
    assert append_rework("b", "   ", URL, now=NOW) is None
    with pytest.raises(ValueError, match="not a feedback attachment URL"):
        append_rework("b", "note", "https://evil.example/x.png", now=NOW)
    # Another prefix is another app's route.
    other = "/api/v2/files/aaaaaaaaaaaa.png"
    assert append_rework("b", "note", other, now=NOW, prefix="/api/v2/files/") is not None
    with pytest.raises(ValueError):
        append_rework("b", "note", other, now=NOW)


def test_the_stamp_is_utc_to_the_minute() -> None:
    """``toISOString().slice(0, 16).replace("T", " ")`` — UTC, whatever zone ``now`` is in."""
    zurich = timezone(timedelta(hours=2))
    assert rework_stamp(datetime(2026, 10, 4, 11, 12, 59, tzinfo=zurich)) == "2026-10-04 09:12"
    # A naive datetime is read as UTC.
    assert rework_stamp(datetime(2026, 10, 4, 9, 12)) == "2026-10-04 09:12"


def test_the_rework_count_anchors_on_whole_rule_lines() -> None:
    """``^---\\s*REWORK\\b`` on a whole line, so kastlan's folded ``--- COMMENT … ---`` blocks
    (§7.7) and a sentence that happens to say REWORK never count."""
    body = "\n".join(
        [
            "original, and I did a REWORK --- honestly",
            "",
            "--- REWORK 2026-10-01 08:00 ---",
            "first",
            "",
            "--- COMMENT 2026-10-02 10:00 · Erika ---",
            "a folded comment",
            "",
            "  ---REWORK 2026-10-03 07:00---  ",
            "second",
            "--- REWORKED 2026-10-03 ---",
        ]
    )
    assert rework_count(body) == 2
    assert rework_count("") == 0 and rework_count(None) == 0
    # A body the kit's own helper wrote counts one per round.
    twice = append_rework(append_rework("o", "a", now=NOW) or "", "b", URL, now=NOW) or ""
    assert rework_count(twice) == 2


def test_file_lines_are_split_out_in_body_order() -> None:
    body = (
        "what happened\n"
        f"[screenshot] {URL}\n\n"
        "--- REWORK 2026-10-04 09:12 ---\nstill\n"
        "[screenshot]   /api/v1/feedback/attachments/aaaaaaaaaaaa.pdf  \n"
        "[screenshot] https://evil.example/x.png\n"
        "see [screenshot] /api/v1/feedback/attachments/bbbbbbbbbbbb.png inline\n\n"
    )
    text, urls = split_body_attachments(body)
    assert urls == [URL, "/api/v1/feedback/attachments/aaaaaaaaaaaa.pdf"]
    # A foreign URL and a sentence mentioning a marker are prose, not files; trailing blank lines go.
    assert "[screenshot] https://evil.example/x.png" in text
    assert "see [screenshot]" in text
    assert not text.endswith("\n")
    assert body_attachment_urls(body) == urls
    assert split_body_attachments(None) == ("", [])
    assert attachment_line(URL) == f"[screenshot] {URL}"


def test_an_append_is_body_only_and_strictly_extends() -> None:
    """keksdose ``test_feedback.py:165`` and ``:203``: an append reworks; a rewrite, a truncation,
    or an append bundled with any other field does not."""
    old = "original"
    appended = "original\n\n--- REWORK 2026-08-19 12:00 ---\nactually, still broken"
    assert is_rework_append(old, {"body": appended})
    assert not is_rework_append(old, {"body": "something else"})
    assert not is_rework_append(old, {"body": "orig"})
    assert not is_rework_append(old, {"body": old})
    assert not is_rework_append(old, {"body": "original + more", "status": "OPEN"})
    assert not is_rework_append(old, {"title": "x"})
    assert not is_rework_append(old, {"body": None})
    # An empty or missing stored body: anything non-empty extends it.
    assert is_rework_append("", {"body": "x"})
    assert is_rework_append(None, {"body": "x"})


def test_the_stricter_reading_of_an_append() -> None:
    old = "original"
    reworked = append_rework(old, "note", now=NOW) or ""
    assert appended_text(old, reworked) == "\n\n--- REWORK 2026-10-04 09:12 ---\nnote"
    assert opens_with_rework_rule(old, reworked)
    assert not opens_with_rework_rule(old, old + " and a sentence")
    assert appended_text(old, "unrelated") == ""
    assert not opens_with_rework_rule(old, "unrelated")
