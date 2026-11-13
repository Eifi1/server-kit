"""The account mails' words (auth §12.3 "Mail texts", §12.5 item 10)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from eifi1_server_kit.auth import EMAIL_CHANGE_TTL, INVITE_TTL, RESET_TTL, VERIFY_TTL
from eifi1_server_kit.auth.mails import (
    ACCOUNT_MAIL_KINDS,
    ACCOUNT_MAIL_TEXTS,
    ACCOUNT_MAIL_VALUES,
    AccountMailText,
    account_mail_texts,
    mail_date,
)
from eifi1_server_kit.mail import MailText, render_mail

LANGUAGES = {"de-CH", "en", "es", "fr", "it", "hu", "zh"}


@pytest.mark.parametrize("kind", list(AccountMailText))
def test_every_mail_is_in_the_seven_languages_and_renders(kind: AccountMailText) -> None:
    table = ACCOUNT_MAIL_TEXTS[kind]
    assert set(table) == LANGUAGES
    values: dict[str, Any] = {name: f"<{name}>" for name in ACCOUNT_MAIL_VALUES[kind]}
    for locale in table:
        message = render_mail(locale, table, link="https://app.example/x?token=t", **values)
        assert "{" not in message.text and "<app>" in message.subject + message.text
        assert message.html is not None and "&lt;app&gt;" in message.html, "names are escaped"
    assert kind in ACCOUNT_MAIL_KINDS


def test_swiss_german_writes_ss_and_the_lifetimes_are_the_kit_s() -> None:
    for table in ACCOUNT_MAIL_TEXTS.values():
        german = table["de-CH"]
        assert "ß" not in german.subject + german.intro + german.body + german.cta + german.outro
    assert (
        timedelta(hours=1) == RESET_TTL
        and "einer Stunde" in ACCOUNT_MAIL_TEXTS[AccountMailText.PASSWORD_RESET]["de-CH"].body
    )
    assert VERIFY_TTL == EMAIL_CHANGE_TTL == timedelta(hours=48)
    assert "48 hours" in ACCOUNT_MAIL_TEXTS[AccountMailText.VERIFICATION]["en"].body
    assert "48 hours" in ACCOUNT_MAIL_TEXTS[AccountMailText.EMAIL_CHANGE]["en"].body
    assert timedelta(days=14) == INVITE_TTL and "14 days" in ACCOUNT_MAIL_TEXTS[AccountMailText.INVITATION]["en"].body


def test_an_app_replaces_a_kind_with_its_own_table() -> None:
    own = {"en": MailText(subject="Welcome to {app}", intro="Hi,", body="Come in.", cta="Join", outro="Bye")}
    merged = account_mail_texts({"invitation": own})
    assert merged[AccountMailText.INVITATION] is own
    assert merged[AccountMailText.PASSWORD_RESET] is ACCOUNT_MAIL_TEXTS[AccountMailText.PASSWORD_RESET]
    assert account_mail_texts() == ACCOUNT_MAIL_TEXTS


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"welcome": {}}, "no account mail is called 'welcome'"),
        ({"invitation": {"fr": MailText("s", "i", "b", "c", "o")}}, "no fallback language"),
        ({"invitation": {"en": MailText("{app} for {name}", "i", "b", "c", "o")}}, "placeholder the kit does not fill"),
        ({"invitation": {"en": MailText("{0}", "i", "b", "c", "o")}}, "placeholder the kit does not fill"),
    ],
)
def test_an_app_table_the_kit_cannot_fill_is_refused(overrides: dict[str, dict[str, MailText]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        account_mail_texts(overrides)


@pytest.mark.parametrize(
    ("locale", "written"),
    [
        ("de-CH", "13.12.2026"),
        ("fr", "13/12/2026"),
        ("it", "13/12/2026"),
        ("es", "13/12/2026"),
        ("hu", "2026. 12. 13."),
        ("zh", "2026年12月13日"),
        ("en", "2026-12-13"),
        (None, "2026-12-13"),
    ],
)
def test_a_day_is_written_as_the_language_writes_it(locale: str | None, written: str) -> None:
    assert mail_date(date(2026, 12, 13), locale) == written


def test_an_aware_moment_is_read_on_its_utc_day() -> None:
    late = datetime(2026, 12, 13, 23, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert mail_date(late, "en") == "2026-12-14"
    assert mail_date(datetime(2026, 12, 13, 23, 30), "en") == "2026-12-13"
    assert mail_date(datetime(2026, 12, 13, 1, 0, tzinfo=UTC), "de") == "13.12.2026"
