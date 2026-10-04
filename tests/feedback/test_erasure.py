"""Account erasure keeps the report and loses the person (contract §3.2).

Ported from the feedback half of keksdose ``tests/unit/test_account_erasure.py``, which
drives ``erase_user_account`` through a database; here the same rows go through the pure
functions. Plus the body-line fix keksdose's own guidance asked for.
"""

from __future__ import annotations

from typing import Any

from eifi1_server_kit.feedback import (
    ERASED_MARK,
    FEEDBACK_CONTEXT_KEEP,
    SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    CrashReportCreate,
    anonymise_feedback,
    attachment_keys_to_purge,
    crash_body,
    crash_fingerprint,
    feedback_attachment_urls,
    scrub_context,
    scrub_text,
)

EMAIL = "erika@example.test"
NAME = "Erika Musterfrau"
PREFIX = "/api/v1/feedback/attachments/"
KEKSDOSE = AttachmentUrlPolicy(SHA12_KEY_PATTERN)


def _erase(**row: Any) -> Any:
    values: dict[str, Any] = {
        "title": "t",
        "body": "b",
        "context": None,
        "screenshot_url": None,
        "attachment_urls": None,
        "identifiers": [EMAIL, NAME],
        "policy": KEKSDOSE,
    }
    values.update(row)
    return anonymise_feedback(**values)


def test_the_report_itself_is_untouched() -> None:
    """keksdose ``test_account_erasure.py:160``: the report is the thing worth keeping;
    NULL ``user_id`` is the ``<deleted user>`` marker."""
    erased = _erase(title="Sync is slow", body="Opening the register takes 4s")
    assert erased.changes == {
        "title": "Sync is slow",
        "body": "Opening the register takes 4s",
        "context": None,
        "crash_fingerprint": None,
        "screenshot_url": None,
        "attachment_urls": None,
        "user_id": None,
    }
    assert erased.urls == frozenset()


def test_an_anonymised_report_keeps_nothing_that_names_the_person() -> None:
    """keksdose ``test_account_erasure.py:184``, with today's allow-list (environment kept,
    origin dropped)."""
    context = {
        "url": "https://keksdose.app/reports?q=Psychotherapie",
        "route": "/reports",
        "origin": "https://dev.keksdose.app",
        "environment": "dev",
        "user_id": 7,
        "user_email": EMAIL,
        "user_display_name": NAME,
        "viewport": "390x844",
        "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X)",
        "version": "0.4.39",
    }
    shot = PREFIX + "abc123abc123.png"
    erased = _erase(title="Crash on /reports", body="It blew up", context=context, screenshot_url=shot)
    blob = repr(erased.changes["context"]) + erased.changes["title"] + erased.changes["body"]
    assert EMAIL not in blob and "Erika" not in blob
    assert "Mozilla" not in blob, "the user agent is a device fingerprint, not a diagnostic"
    assert "Psychotherapie" not in blob, "the full URL can carry what the person typed"
    assert erased.changes["context"] == {
        "route": "/reports",
        "environment": "dev",
        "viewport": "390x844",
        "version": "0.4.39",
    }
    assert erased.changes["screenshot_url"] is None, "a screenshot is a picture of their own data"
    assert erased.urls == {shot}


def test_the_allow_list_is_the_contracts() -> None:
    """§3.2 as revised 2026-10-04: these ten keys and nothing else."""
    assert {
        "route",
        "environment",
        "viewport",
        "version",
        "boundary",
        "online",
        "occurrences",
        "first_seen_at",
        "last_seen_at",
        "auto_reported",
    } == FEEDBACK_CONTEXT_KEEP
    for gone in ("url", "ua", "origin", "fingerprint", "user_id", "user_email", "user_display_name", "anything_new"):
        assert gone not in FEEDBACK_CONTEXT_KEEP
    assert scrub_context(None) is None and scrub_context({}) is None
    assert scrub_context({"ua": "x"}) is None, "nothing left is NULL, not {}"


def test_a_crash_report_loses_the_email_out_of_its_body() -> None:
    """keksdose ``test_account_erasure.py:229``: the crash body writes ``User: <email> (#<id>)``
    in plain text, so scrubbing the JSON alone would leave the address one column over."""
    crash = CrashReportCreate(name="TypeError", message="x is not a function", route="/budget")
    fingerprint = crash_fingerprint(crash, 7)
    body = crash_body(crash, user_id=7, user_email=EMAIL, fingerprint=fingerprint)
    assert EMAIL in body, "precondition: the crash body embeds the address"
    erased = _erase(title="[crash] TypeError: x is not a function", body=body)
    kept = erased.changes["body"]
    assert EMAIL not in kept and ERASED_MARK in kept
    assert "TypeError: x is not a function" in kept and "/budget" in kept
    assert erased.changes["crash_fingerprint"] is None


def test_scrub_text_is_exact_and_case_insensitive() -> None:
    """keksdose ``_scrub_text``: exact known strings, any case; needles under 4 characters are
    skipped ("Bob" appears inside ordinary words); free prose stays."""
    assert scrub_text("Mail ERIKA@Example.test now", [EMAIL]) == f"Mail {ERASED_MARK} now"
    assert scrub_text("Bob wrote about bobsleighs", ["Bob"]) == "Bob wrote about bobsleighs"
    assert scrub_text("a.b+c@x.y (regex chars)", ["a.b+c@x.y"]) == f"{ERASED_MARK} (regex chars)"
    assert scrub_text(None, [EMAIL]) == ""
    assert scrub_text("x", [None, ""]) == "x"


def test_the_rework_files_in_the_body_go_too() -> None:
    """The fix: keksdose cleared both picture columns and left the body's ``[screenshot]``
    lines — and never purged their objects. Stripped here, and named for the purge."""
    rework = PREFIX + "deadbeef0001.png"
    body = f"original\n\n--- REWORK 2026-10-04 09:12 ---\nstill broken, {EMAIL}\n[screenshot] {rework}"
    erased = _erase(body=body, attachment_urls=[PREFIX + "0123456789ab.pdf"])
    assert erased.changes["body"] == f"original\n\n--- REWORK 2026-10-04 09:12 ---\nstill broken, {ERASED_MARK}"
    assert erased.urls == {rework, PREFIX + "0123456789ab.pdf"}
    assert erased.changes["attachment_urls"] is None


def test_a_shared_object_is_not_purged() -> None:
    """keksdose ``test_account_erasure.py:360`` and ``:382``: content-addressed keys carry no user,
    so a file another report names — in a column OR, now, in its body — is somebody else's too."""
    shared, lonely, rework = PREFIX + "deadbeef1234.png", PREFIX + "0123456789ab.png", PREFIX + "aaaaaaaaaaaa.png"
    mine = _erase(screenshot_url=shared, attachment_urls=[lonely], body=f"b\n[screenshot] {rework}")
    theirs = feedback_attachment_urls(
        screenshot_url=None, attachment_urls=[shared], body=f"x\n[screenshot] {rework}", policy=KEKSDOSE
    )
    assert attachment_keys_to_purge(mine.urls, theirs, policy=KEKSDOSE) == ["0123456789ab.png"]
    # Nobody else names any of them: all three go, sorted.
    assert attachment_keys_to_purge(mine.urls, [], policy=KEKSDOSE) == [
        "0123456789ab.png",
        "aaaaaaaaaaaa.png",
        "deadbeef1234.png",
    ]


def test_a_url_that_is_not_ours_is_never_a_purge_candidate() -> None:
    assert attachment_keys_to_purge(["https://evil.example/x.png", PREFIX + "../x"], [], policy=KEKSDOSE) == []
    # The opaque default policy reads kastlan-shaped keys too.
    kastlan = f"{PREFIX}{'0' * 32}_{'a' * 12}.png"
    assert attachment_keys_to_purge([kastlan], []) == [f"{'0' * 32}_{'a' * 12}.png"]
