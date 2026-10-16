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


# ── split_rework_blocks (round 0.40, §10.4) ─────────────────────────────────


def test_a_body_without_a_block_comes_back_unchanged() -> None:
    from eifi1_server_kit.feedback import split_rework_blocks

    assert split_rework_blocks("Sync is slow\n\n") == ("Sync is slow\n\n", [])
    assert split_rework_blocks(None) == ("", [])
    # A sentence that says REWORK is no rule: nothing is split.
    sentence = "I did a REWORK --- honestly\nand --- REWORK in the middle --- of a line, too"
    assert split_rework_blocks(sentence) == (sentence, [])


def test_one_and_two_blocks_with_a_file() -> None:
    from eifi1_server_kit.feedback import REWORK_BLOCK_RE, ReworkBlock, split_rework_blocks

    one = append_rework("Sync is slow", "  still slow  ", now=NOW)
    assert one is not None
    assert split_rework_blocks(one) == (
        "Sync is slow",
        [ReworkBlock(datetime(2026, 10, 4, 9, 12, tzinfo=UTC), "still slow")],
    )
    later = NOW + timedelta(days=1, minutes=3)
    two = append_rework(one, "Still cut off\non the iPad", URL, now=later)
    assert two is not None
    description, blocks = split_rework_blocks(two)
    assert description == "Sync is slow"
    assert [block.note for block in blocks] == ["still slow", "Still cut off\non the iPad"]
    assert (blocks[0].url, blocks[1].url) == (None, URL)
    assert blocks[1].stamp == datetime(2026, 10, 5, 9, 15, tzinfo=UTC)
    assert REWORK_BLOCK_RE.match("--- REWORK 2026-10-04 09:12 ---") is not None


def test_only_the_first_file_line_is_the_url_and_a_foreign_one_is_prose() -> None:
    from eifi1_server_kit.feedback import split_rework_blocks

    other = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"
    body = (
        f"--- REWORK 2026-10-04 09:12 ---\n[screenshot] {URL}\nlook\n[screenshot] {other}\n[screenshot] https://evil/x"
    )
    description, [block] = split_rework_blocks(body)
    assert description == "" and block.url == URL
    assert block.note == f"look\n[screenshot] {other}\n[screenshot] https://evil/x"
    # Under another prefix the line is not this app's file.
    _, [mine] = split_rework_blocks(body, prefix="/files/")
    assert mine.url is None and mine.note.startswith(f"[screenshot] {URL}")


def test_kastlans_comment_blocks_stay_in_the_description() -> None:
    from eifi1_server_kit.feedback import split_rework_blocks

    body = (
        "Original\n\n--- REWORK 2026-10-04 09:12 ---\nagain\n\n"
        "--- COMMENT 2026-10-05 10:00 · Ann ---\nA comment\n\n--- REWORK 2026-10-06 11:00 ---\nthird"
    )
    description, blocks = split_rework_blocks(body)
    assert description == "Original\n\n--- COMMENT 2026-10-05 10:00 · Ann ---\nA comment"
    assert [block.note for block in blocks] == ["again", "third"]


@pytest.mark.parametrize(
    "rule",
    ["--- REWORK ANSWERED 2026-09-16 ---", "--- REWORK 2026-09-04 ---", "--- REWORK (2) ---", "---REWORK---"],
)
def test_a_rework_rule_that_is_no_block_stops_the_split(rule: str) -> None:
    """keksdose's ``outcome`` holds such lines from old loop answers: split ``body`` only."""
    from eifi1_server_kit.feedback import split_rework_blocks

    with pytest.raises(ValueError, match="a REWORK rule that is no block"):
        split_rework_blocks(f"Original\n\n  {rule}  \nanswered")


def test_a_stamp_that_is_no_date_stops_the_split() -> None:
    from eifi1_server_kit.feedback import split_rework_blocks

    with pytest.raises(ValueError):
        split_rework_blocks("x\n--- REWORK 2026-13-45 09:12 ---\nnote")
