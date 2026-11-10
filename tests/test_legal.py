"""A change to the terms or the privacy policy (legal decision 10): the record and its notice
period, the in-app list, the mail in seven languages, and every account mailed once."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from typing import assert_type

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from eifi1_server_kit.legal import (
    LEGAL_CHANGE_BATCH,
    LEGAL_CHANGE_PAUSE,
    LEGAL_CHANGE_STOP_AFTER,
    LEGAL_CHANGES_PATH,
    NOTICE_DAYS,
    LegalChange,
    LegalChangeAnnouncement,
    LegalChangeOutcome,
    LegalChangeRecipient,
    LegalChangeResult,
    LegalChangeRun,
    LegalNoticeError,
    current_announcements,
    legal_change_message,
    send_legal_change_mails,
)
from eifi1_server_kit.mail import AccountNoticeKind, HtmlMailMessage, MailMessage, ResendClient
from eifi1_server_kit.notifications import MAIL_EVENT_PATTERN

SEVEN = ("de-CH", "en", "es", "fr", "hu", "it", "zh")
BASE = "https://app.example"
TERMS = LegalChange("terms", announced=date(2026, 11, 10), effective=date(2026, 12, 15))
PRIVACY = LegalChange("privacy", announced=date(2026, 11, 10), effective=date(2026, 12, 15))
TODAY = date(2026, 11, 10)

SENT = LegalChangeOutcome.SENT
SKIPPED = LegalChangeOutcome.SKIPPED
FAILED = LegalChangeOutcome.FAILED


# --- the record and its notice period ----------------------------------------------------


def test_the_notice_is_thirty_days_and_the_route_is_legal_changes() -> None:
    assert NOTICE_DAYS == 30
    assert LEGAL_CHANGES_PATH == "/legal/changes"
    assert (LEGAL_CHANGE_BATCH, LEGAL_CHANGE_PAUSE, LEGAL_CHANGE_STOP_AFTER) == (200, 0.5, 5)


def test_a_change_with_thirty_days_notice_is_made() -> None:
    change = LegalChange("privacy", announced=date(2026, 11, 1), effective=date(2026, 12, 1))
    assert change.key == "privacy:2026-12-01"
    assert re.fullmatch(MAIL_EVENT_PATTERN, change.key), "a notifications marker table can hold it"
    assert change.mail_by == date(2026, 11, 1), "the last day a mail still gives 30 days"
    assert TERMS.mail_by == date(2026, 11, 15)


@pytest.mark.parametrize("effective", [date(2026, 11, 30), date(2026, 11, 2), date(2026, 11, 1), date(2026, 10, 1)])
def test_a_shorter_notice_cannot_be_made(effective: date) -> None:
    """The app's own record refuses it, so its tests fail before a release does."""
    with pytest.raises(LegalNoticeError, match="change to the terms takes effect at least 30 days after") as caught:
        LegalChange("terms", announced=date(2026, 11, 1), effective=effective)
    assert isinstance(caught.value, ValueError) and caught.value.status_code == 409
    with pytest.raises(LegalNoticeError, match=r"change to the privacy policy .* is -?\d+ days$"):
        LegalChange("privacy", announced=date(2026, 11, 1), effective=effective)


def test_a_change_is_to_the_terms_or_the_privacy_policy() -> None:
    with pytest.raises(ValueError, match="terms or the privacy policy"):
        LegalChange("impressum", announced=date(2026, 11, 1), effective=date(2026, 12, 15))  # type: ignore[arg-type]


def test_a_change_counts_whole_days() -> None:
    with pytest.raises(TypeError, match=r"pass \.date\(\)"):
        LegalChange("terms", announced=datetime(2026, 11, 1, 9, 0), effective=date(2026, 12, 15))


@pytest.mark.parametrize(
    "href",
    [
        "/terms",
        "/terms?version=2026-12-15#changes",
        "/",
        "https://legal.app.example/terms/next",
        "http://localhost:5173/terms",
    ],
)
def test_the_new_text_is_a_path_in_the_app_or_a_url(href: str) -> None:
    assert LegalChange("terms", announced=TODAY, effective=date(2026, 12, 15), href=href).href == href
    assert LegalChangeAnnouncement(page="terms", effective=date(2026, 12, 15), href=href).href == href


@pytest.mark.parametrize(
    "href",
    [
        "",
        "terms",
        "javascript:alert(1)",
        "//evil.example/terms",
        "/\\evil.example",
        "/terms next",
        "/terms\nLocation: x",
        "/terms x",
        "ftp://app.example/terms",
    ],
)
def test_the_new_text_is_never_another_scheme_or_host(href: str) -> None:
    with pytest.raises(ValueError, match="path in the app"):
        LegalChange("terms", announced=TODAY, effective=date(2026, 12, 15), href=href)
    with pytest.raises(ValidationError, match="path in the app"):
        LegalChangeAnnouncement(page="terms", effective=date(2026, 12, 15), href=href)


# --- the in-app list ---------------------------------------------------------------------


def test_the_announcement_is_page_effective_and_href_on_the_wire() -> None:
    """The shape ui-kit's notice reads, exactly: no announcement day, nothing else."""
    announcement = TERMS.announcement
    assert announcement == LegalChangeAnnouncement(page="terms", effective=date(2026, 12, 15))
    assert announcement.model_dump(mode="json") == {"page": "terms", "effective": "2026-12-15", "href": None}
    schema = LegalChangeAnnouncement.model_json_schema()
    assert list(schema["properties"]) == ["page", "effective", "href"]
    assert schema["required"] == ["page", "effective"]
    assert schema["properties"]["page"]["enum"] == ["terms", "privacy"]
    assert schema["properties"]["effective"]["format"] == "date"
    parsed = LegalChangeAnnouncement.model_validate(
        {"page": "privacy", "effective": "2026-12-15", "href": "/privacy?next=1"}
    )
    assert parsed.page == "privacy" and parsed.effective == date(2026, 12, 15)
    with pytest.raises(ValidationError):
        LegalChangeAnnouncement.model_validate({"page": "impressum", "effective": "2026-12-15"})
    with pytest.raises(ValidationError):
        LegalChangeAnnouncement.model_validate({"page": "terms", "effective": "2026-12-15", "announced": "2026-11-10"})
    with pytest.raises(ValidationError):
        announcement.href = "/terms"  # type: ignore[misc]


CHANGES = (
    PRIVACY,
    LegalChange("terms", announced=date(2026, 11, 10), effective=date(2026, 12, 15), href="/terms?version=2026-12-15"),
    LegalChange("terms", announced=date(2026, 10, 1), effective=date(2026, 11, 9)),
    LegalChange("terms", announced=date(2026, 11, 20), effective=date(2027, 1, 1)),
)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 30), []),
        (date(2026, 11, 9), [("terms", "2026-11-09")]),
        (date(2026, 11, 10), [("terms", "2026-12-15"), ("privacy", "2026-12-15")]),
        (date(2026, 11, 20), [("terms", "2026-12-15"), ("privacy", "2026-12-15"), ("terms", "2027-01-01")]),
        (date(2026, 12, 15), [("terms", "2026-12-15"), ("privacy", "2026-12-15"), ("terms", "2027-01-01")]),
        (date(2026, 12, 16), [("terms", "2027-01-01")]),
        (date(2027, 1, 2), []),
    ],
)
def test_the_list_holds_what_is_announced_and_not_yet_past(today: date, expected: list[tuple[str, str]]) -> None:
    """From the announcement day through the effective day, soonest first, the terms before
    the privacy policy on one day."""
    current = current_announcements(CHANGES, today=today)
    assert [(item.page, item.effective.isoformat()) for item in current] == expected


def test_the_list_counts_days() -> None:
    with pytest.raises(TypeError, match=r"pass \.date\(\)"):
        current_announcements(CHANGES, today=datetime(2026, 11, 10, 12, 0))


async def test_the_route_is_three_lines_over_the_apps_changes() -> None:
    """The README's route: public, the list as JSON."""
    app = FastAPI()

    @app.get(f"/api/v1{LEGAL_CHANGES_PATH}", response_model=list[LegalChangeAnnouncement])
    def legal_changes() -> list[LegalChangeAnnouncement]:
        return current_announcements(CHANGES, today=date(2026, 11, 10))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/legal/changes")
    assert response.status_code == 200
    assert response.json() == [
        {"page": "terms", "effective": "2026-12-15", "href": "/terms?version=2026-12-15"},
        {"page": "privacy", "effective": "2026-12-15", "href": None},
    ]


# --- the mail ----------------------------------------------------------------------------


@pytest.mark.parametrize("change", [TERMS, PRIVACY], ids=["terms", "privacy"])
@pytest.mark.parametrize("locale", SEVEN)
def test_the_mail_renders_in_each_language(locale: str, change: LegalChange) -> None:
    message = legal_change_message(locale, change, app_name="Kurvenschmiede", app_base_url=BASE)
    assert "Kurvenschmiede" in message.subject
    for part in (message.text, message.html):
        assert "Kurvenschmiede" in part and f"{BASE}/{change.page}" in part
        assert "{" not in part, "every placeholder filled"
    assert message.html.startswith(f'<!doctype html><html lang="{locale}">')
    assert f"<title>{message.subject}</title>" in message.html
    assert message.headers is None, "a notice the terms owe: no List-Unsubscribe"
    assert message.idempotency_key is None, "the send adds it"


@pytest.mark.parametrize(
    ("locale", "fifteenth", "first"),
    [
        ("de-CH", "15. Dezember 2026", "1. Dezember 2026"),
        ("en", "15 December 2026", "1 December 2026"),
        ("es", "15 de diciembre de 2026", "1 de diciembre de 2026"),
        ("fr", "15 décembre 2026", "1er décembre 2026"),
        ("hu", "2026. december 15.", "2026. december 1."),
        ("it", "15 dicembre 2026", "1º dicembre 2026"),
        ("zh", "2026年12月15日", "2026年12月1日"),
    ],
)
def test_the_date_is_written_out_in_the_mails_language(locale: str, fifteenth: str, first: str) -> None:
    for effective, written in ((date(2026, 12, 15), fifteenth), (date(2026, 12, 1), first)):
        change = LegalChange("terms", announced=date(2026, 10, 1), effective=effective)
        message = legal_change_message(locale, change, app_name="A", app_base_url=BASE)
        assert written in message.subject and written in message.text


def test_the_terms_mail_says_what_changes_and_how_to_object() -> None:
    message = legal_change_message("en", TERMS, app_name="Kurvenschmiede", app_base_url="https://kurvenschmiede.app")
    assert message.subject == "Kurvenschmiede: our terms of service change on 15 December 2026"
    assert message.text == (
        "We are updating the terms of service of Kurvenschmiede. The new terms apply from 15 December 2026.\n\n"
        "If you do not agree, you can object or delete your account before that date, and you can export your "
        "data until then. To object, simply reply to this email. If you object, we may end your account on that "
        "date. If you keep using Kurvenschmiede after that date, the new terms apply.\n\n"
        "Read the new terms: https://kurvenschmiede.app/terms\n\n"
        "You are receiving this notice because you have an account with Kurvenschmiede."
    )


def test_the_privacy_mail_has_no_sentence_the_policy_has_no_clause_for() -> None:
    message = legal_change_message("en", PRIVACY, app_name="kastlan", app_base_url=BASE)
    assert message.subject == "kastlan: our privacy policy changes on 15 December 2026"
    assert "object or delete your account before that date" in message.text
    assert "end your account" not in message.text and "Read the new privacy policy: " in message.text
    german = legal_change_message("de-CH", PRIVACY, app_name="kastlan", app_base_url=BASE)
    assert german.subject == "kastlan: Unsere Datenschutzerklärung ändert sich am 15. Dezember 2026"
    assert "ß" not in german.text + legal_change_message("de-CH", TERMS, app_name="k", app_base_url=BASE).text


def test_the_mail_falls_back_as_every_mail_does() -> None:
    """A region finds its language; an unknown or missing locale reads German."""
    french = legal_change_message("fr-CH", TERMS, app_name="a", app_base_url=BASE)
    assert french.subject == "Les conditions d’utilisation de a changent le 15 décembre 2026"
    assert "Lire les nouvelles conditions : https://app.example/terms" in french.text
    for unknown in (None, "", "xx"):
        message = legal_change_message(unknown, TERMS, app_name="a", app_base_url=BASE)
        assert message.subject == "a: Unsere Nutzungsbedingungen ändern sich am 15. Dezember 2026"
        assert message.html.startswith('<!doctype html><html lang="de-CH">')


@pytest.mark.parametrize(
    ("href", "base", "link"),
    [
        (None, "https://app.example/", "https://app.example/terms"),
        ("/terms?version=2026-12-15", "https://app.example", "https://app.example/terms?version=2026-12-15"),
        ("/legal/terms-next", "https://app.example/base/", "https://app.example/base/legal/terms-next"),
        ("https://legal.example/terms-next", "https://app.example", "https://legal.example/terms-next"),
    ],
)
def test_the_button_goes_to_the_new_text(href: str | None, base: str, link: str) -> None:
    change = LegalChange("terms", announced=TODAY, effective=date(2026, 12, 15), href=href)
    message = legal_change_message("en", change, app_name="a", app_base_url=base)
    assert f"Read the new terms: {link}" in message.text
    assert f'href="{link}"' in message.html


@pytest.mark.parametrize("base", ["", "app.example", "ftp://app.example", "https://app.example/ x", "/terms"])
def test_the_base_url_is_http(base: str) -> None:
    with pytest.raises(ValueError, match="base URL is http"):
        legal_change_message("en", TERMS, app_name="a", app_base_url=base)


def test_the_mail_escapes_the_apps_name_and_always_has_its_html_half() -> None:
    message = legal_change_message("en", TERMS, app_name="<b>A&B</b>", app_base_url=BASE)
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in message.html and "<b>" not in message.html
    assert message.subject.startswith("<b>A&B</b>: "), "the subject header is plain text"
    assert legal_change_message("en", TERMS, app_name="{date}", app_base_url=BASE).subject.startswith("{date}: ")
    assert_type(message.html, str)
    assert isinstance(message, HtmlMailMessage) and isinstance(message, MailMessage)


# --- every account, once -----------------------------------------------------------------


class _Mailer:
    """A transport that answers from a script (then ``True``) and keeps every send."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.sends: list[tuple[str, MailMessage, str]] = []

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        self.sends.append((to, message, kind))
        return self.answers.pop(0) if self.answers else True


class _Record:
    def __init__(self) -> None:
        self.ids: list[int] = []

    async def __call__(self, user_id: int) -> None:
        self.ids.append(user_id)


class _Sleep:
    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)


class _Read:
    """The recipients as a generator that counts how many were read."""

    def __init__(self, recipients: Sequence[LegalChangeRecipient[int]]) -> None:
        self.recipients = recipients
        self.read = 0

    def __iter__(self) -> Iterator[LegalChangeRecipient[int]]:
        for recipient in self.recipients:
            self.read += 1
            yield recipient


def _people(*ids: int, locale: str | None = "en") -> list[LegalChangeRecipient[int]]:
    return [LegalChangeRecipient(user_id, f"person{user_id}@example.com", locale) for user_id in ids]


def _outcomes(run: LegalChangeRun[int]) -> list[tuple[int, LegalChangeOutcome]]:
    return [(result.user_id, result.outcome) for result in run.results]


async def test_every_account_is_mailed_once_in_its_language() -> None:
    mailer, record, sleep = _Mailer(), _Record(), _Sleep()
    recipients = [
        LegalChangeRecipient(1, "ada@example.com", "de-CH"),
        LegalChangeRecipient(2, "bob@example.com", "en"),
        LegalChangeRecipient(3, "chloe@example.com", "fr-CH"),
        LegalChangeRecipient(4, "dora@example.com", None),
    ]
    run = await send_legal_change_mails(
        TERMS,
        recipients,
        mailer,
        app_name="Kurvenschmiede",
        app_base_url="https://kurvenschmiede.app",
        today=TODAY,
        already_sent=set(),
        record_sent=record,
        sleep=sleep,
    )
    assert_type(run, LegalChangeRun[int])
    assert _outcomes(run) == [(1, SENT), (2, SENT), (3, SENT), (4, SENT)]
    assert (run.sent, run.skipped, run.failed, run.complete) == (4, 0, 0, True)
    assert record.ids == [1, 2, 3, 4]
    assert [to for to, _, _ in mailer.sends] == [
        "ada@example.com",
        "bob@example.com",
        "chloe@example.com",
        "dora@example.com",
    ]
    assert [kind for _, _, kind in mailer.sends] == [AccountNoticeKind.LEGAL_CHANGE] * 4
    assert str(AccountNoticeKind.LEGAL_CHANGE) == "legal_change"
    assert [message.idempotency_key for _, message, _ in mailer.sends] == [
        f"legal_change:kurvenschmiede.app:terms:2026-12-15:{user_id}" for user_id in (1, 2, 3, 4)
    ]
    subjects = [message.subject for _, message, _ in mailer.sends]
    assert subjects[0] == subjects[3] == "Kurvenschmiede: Unsere Nutzungsbedingungen ändern sich am 15. Dezember 2026"
    assert subjects[1] == "Kurvenschmiede: our terms of service change on 15 December 2026"
    assert subjects[2].startswith("Les conditions d’utilisation")
    assert all(message.headers is None for _, message, _ in mailer.sends), "no List-Unsubscribe"
    assert sleep.pauses == [LEGAL_CHANGE_PAUSE] * 3, "between two mails, never before the first"


async def test_a_rerun_never_mails_anyone_twice() -> None:
    """The ids the app recorded are skipped, and so is an id listed twice."""
    mailer, record = _Mailer(), _Record()
    first = await send_legal_change_mails(
        TERMS,
        _people(1, 2, 3, 3, 4),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent={1, 2},
        record_sent=record,
        pause=0,
    )
    assert _outcomes(first) == [(1, SKIPPED), (2, SKIPPED), (3, SENT), (3, SKIPPED), (4, SENT)]
    assert (first.sent, first.skipped, first.failed, first.complete) == (2, 3, 0, True)
    assert record.ids == [3, 4] and [to for to, _, _ in mailer.sends] == ["person3@example.com", "person4@example.com"]

    again = await send_legal_change_mails(
        TERMS,
        _people(1, 2, 3, 4),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=date(2026, 11, 11),
        already_sent={1, 2, *record.ids},
        record_sent=record,
        pause=0,
    )
    assert _outcomes(again) == [(1, SKIPPED), (2, SKIPPED), (3, SKIPPED), (4, SKIPPED)]
    assert again.complete and len(mailer.sends) == 2 and record.ids == [3, 4]


async def test_a_failed_send_is_reported_not_recorded_and_tried_by_the_next_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mailer, record = _Mailer(True, False, True), _Record()
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.legal"):
        run = await send_legal_change_mails(
            TERMS,
            _people(1, 2, 3),
            mailer,
            app_name="a",
            app_base_url=BASE,
            today=TODAY,
            already_sent=set(),
            record_sent=record,
            pause=0,
        )
    assert _outcomes(run) == [(1, SENT), (2, FAILED), (3, SENT)]
    assert (run.sent, run.failed, run.complete) == (2, 1, True)
    assert record.ids == [1, 3]
    [line] = [r for r in caplog.records if r.name == "eifi1_server_kit.legal"]
    assert line.levelno == logging.WARNING
    assert line.getMessage() == "legal change terms:2026-12-15: 2 sent, 0 skipped, 1 failed"
    assert "@" not in caplog.text, "never an address"

    retry = await send_legal_change_mails(
        TERMS,
        _people(1, 2, 3),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=set(record.ids),
        record_sent=record,
        pause=0,
    )
    assert _outcomes(retry) == [(1, SKIPPED), (2, SENT), (3, SKIPPED)] and record.ids == [1, 3, 2]


async def test_a_run_stops_after_failures_in_a_row() -> None:
    """The provider is down or the quota spent: the rest wait for the next run, unread."""
    mailer, record = _Mailer(False, False, False), _Record()
    recipients = _Read(_people(1, 2, 3, 4, 5))
    run = await send_legal_change_mails(
        TERMS,
        recipients,
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=set(),
        record_sent=record,
        pause=0,
        stop_after=2,
    )
    assert _outcomes(run) == [(1, FAILED), (2, FAILED)]
    assert not run.complete and len(mailer.sends) == 2 and record.ids == []
    assert recipients.read == 3, "the third is read to know one is left, and none after it"


async def test_a_sent_mail_ends_a_run_of_failures() -> None:
    mailer, record = _Mailer(False, True, False, True), _Record()
    run = await send_legal_change_mails(
        TERMS,
        _people(1, 2, 3, 4),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=set(),
        record_sent=record,
        pause=0,
        stop_after=2,
    )
    assert _outcomes(run) == [(1, FAILED), (2, SENT), (3, FAILED), (4, SENT)] and run.complete


async def test_the_batch_stops_at_its_limit_and_the_next_call_goes_on(caplog: pytest.LogCaptureFixture) -> None:
    mailer, record = _Mailer(), _Record()
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.legal"):
        first = await send_legal_change_mails(
            TERMS,
            _people(1, 2, 3, 4),
            mailer,
            app_name="a",
            app_base_url=BASE,
            today=TODAY,
            already_sent=set(),
            record_sent=record,
            pause=0,
            limit=2,
        )
    assert _outcomes(first) == [(1, SENT), (2, SENT)] and not first.complete
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].getMessage() == (
        "legal change terms:2026-12-15: 2 sent, 0 skipped, 0 failed; stopped with accounts left, run it again"
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.legal"):
        second = await send_legal_change_mails(
            TERMS,
            _people(1, 2, 3, 4),
            mailer,
            app_name="a",
            app_base_url=BASE,
            today=TODAY,
            already_sent=set(record.ids),
            record_sent=record,
            pause=0,
            limit=2,
        )
    assert _outcomes(second) == [(1, SKIPPED), (2, SKIPPED), (3, SENT), (4, SENT)] and second.complete
    assert caplog.records[-1].levelno == logging.INFO
    assert caplog.records[-1].getMessage() == "legal change terms:2026-12-15: 2 sent, 2 skipped, 0 failed"


async def test_skipped_accounts_after_the_limit_leave_the_run_complete() -> None:
    mailer, record = _Mailer(), _Record()
    run = await send_legal_change_mails(
        TERMS,
        _people(1, 2, 3),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent={3},
        record_sent=record,
        pause=0,
        limit=2,
    )
    assert _outcomes(run) == [(1, SENT), (2, SENT), (3, SKIPPED)] and run.complete


async def test_no_limit_sends_to_everyone_and_no_pause_never_sleeps() -> None:
    mailer, record, sleep = _Mailer(), _Record(), _Sleep()
    run = await send_legal_change_mails(
        PRIVACY,
        _people(*range(1, 8)),
        mailer,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=set(),
        record_sent=record,
        limit=None,
        pause=0,
        sleep=sleep,
    )
    assert run.sent == 7 and run.complete and sleep.pauses == []
    assert mailer.sends[0][1].idempotency_key == "legal_change:app.example:privacy:2026-12-15:1"


async def test_string_ids_type_the_run_by_them() -> None:
    user_id = "6f1c2a9e-1b0d-4c55-9a51-3f4e2d1c0b7a"
    recorded: list[str] = []

    async def record(sent_id: str) -> None:
        recorded.append(sent_id)

    run = await send_legal_change_mails(
        TERMS,
        [LegalChangeRecipient(user_id, "ada@example.com")],
        _Mailer(),
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=frozenset[str](),
        record_sent=record,
        pause=0,
    )
    assert_type(run, LegalChangeRun[str])
    assert run.results == (LegalChangeResult(user_id, SENT),) and recorded == [user_id]


@pytest.mark.parametrize("today", [date(2026, 11, 10), date(2026, 11, 15)])
async def test_mail_goes_from_the_announcement_to_mail_by(today: date) -> None:
    run = await send_legal_change_mails(
        TERMS,
        _people(1),
        _Mailer(),
        app_name="a",
        app_base_url=BASE,
        today=today,
        already_sent=set(),
        record_sent=_Record(),
    )
    assert run.sent == 1


@pytest.mark.parametrize(
    ("today", "message"),
    [
        (date(2026, 11, 16), "needed its mail by 2026-11-15 for 30 days' notice"),
        (date(2026, 12, 15), "needed its mail by 2026-11-15"),
        (date(2026, 11, 9), "is announced on 2026-11-10: mail it from that day on"),
    ],
)
async def test_the_notice_holds_or_nothing_is_sent(today: date, message: str) -> None:
    mailer, recipients = _Mailer(), _Read(_people(1, 2))
    with pytest.raises(LegalNoticeError, match=message):
        await send_legal_change_mails(
            TERMS,
            recipients,
            mailer,
            app_name="a",
            app_base_url=BASE,
            today=today,
            already_sent=set(),
            record_sent=_Record(),
        )
    assert mailer.sends == [] and recipients.read == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"limit": 0}, "limit is at least 1"),
        ({"stop_after": 0}, "stop_after is at least 1"),
        ({"pause": -0.1}, "pause is 0 or more"),
        ({"app_base_url": "ftp://app.example"}, "base URL is http"),
        ({"app_base_url": "https://"}, "ASCII host"),
        ({"app_base_url": "https://bücher.example"}, "ASCII host"),
        ({"today": datetime(2026, 11, 10, 9, 0)}, "pass .date"),
    ],
)
async def test_a_bad_argument_sends_nothing(options: dict[str, object], message: str) -> None:
    mailer, recipients = _Mailer(), _Read(_people(1))
    arguments: dict[str, object] = {"app_base_url": BASE, "today": TODAY, **options}
    with pytest.raises((ValueError, TypeError), match=message):
        await send_legal_change_mails(
            TERMS,
            recipients,
            mailer,
            app_name="a",
            already_sent=set(),
            record_sent=_Record(),
            **arguments,  # type: ignore[arg-type]
        )
    assert mailer.sends == [] and recipients.read == 0


async def test_a_record_that_fails_stops_the_run() -> None:
    """The mail went and the app could not write it down: the job fails loudly; the
    send's idempotency key covers a rerun within Resend's 24 hours."""
    mailer = _Mailer()

    async def record(_user_id: int) -> None:
        raise RuntimeError("database gone")

    with pytest.raises(RuntimeError, match="database gone"):
        await send_legal_change_mails(
            TERMS,
            _people(1, 2),
            mailer,
            app_name="a",
            app_base_url=BASE,
            today=TODAY,
            already_sent=set(),
            record_sent=record,
            pause=0,
        )
    assert len(mailer.sends) == 1


async def test_resend_gets_the_key_the_reply_to_and_no_unsubscribe_headers() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "m-1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    resend = ResendClient(
        api_key="re_test",
        from_address="App <noreply@app.example>",
        reply_to="support@app.example",
        client=http,
        retry_delay=0,
    )
    run = await send_legal_change_mails(
        TERMS,
        _people(17),
        resend,
        app_name="a",
        app_base_url=BASE,
        today=TODAY,
        already_sent=set(),
        record_sent=_Record(),
    )
    assert run.sent == 1
    [request] = requests
    assert request.headers["Idempotency-Key"] == "legal_change:app.example:terms:2026-12-15:17"
    payload = json.loads(request.content)
    assert payload["to"] == ["person17@example.com"] and payload["reply_to"] == ["support@app.example"]
    assert "headers" not in payload, "no List-Unsubscribe: the terms owe this notice"


def test_a_recipient_never_shows_its_address() -> None:
    recipient = LegalChangeRecipient(17, "ada@example.com", "de-CH")
    assert "ada@example.com" not in repr(recipient) and "17" in repr(recipient)
    assert LegalChangeRecipient("6f1c2a9e-1b0d-4c55-9a51-3f4e2d1c0b7a", "a@example.com").locale is None


@pytest.mark.parametrize("user_id", ["", " ", "a:b", "a b", "-a", "x" * 65])
def test_a_string_user_id_is_one_key_part(user_id: str) -> None:
    with pytest.raises(ValueError, match="no colon or space"):
        LegalChangeRecipient(user_id, "a@example.com")


def test_the_outcomes_are_words() -> None:
    assert [str(outcome) for outcome in LegalChangeOutcome] == ["sent", "skipped", "failed"]
    run = LegalChangeRun(
        (LegalChangeResult(1, SENT), LegalChangeResult(2, SKIPPED), LegalChangeResult(3, FAILED)), complete=False
    )
    assert (run.sent, run.skipped, run.failed) == (1, 1, 1)
