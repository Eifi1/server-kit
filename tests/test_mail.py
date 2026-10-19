"""Outbound mail: the language chain, the escaped layout, and the two transports."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import assert_type

import httpx
import pytest

from eifi1_server_kit.auth import Budget, OneTimeTokenKind
from eifi1_server_kit.mail import (
    FALLBACK_LOCALES,
    MAIL_TEST_KIND,
    MAIL_TEST_LIMIT,
    RESEND_ENDPOINT,
    AccountNoticeKind,
    ConsoleMailer,
    HtmlMailMessage,
    Mailer,
    MailMessage,
    MailTestResult,
    MailText,
    MailUnsubscribe,
    ResendClient,
    list_unsubscribe_headers,
    pick,
    pick_entry,
    render_mail,
    render_message,
    support_address,
    test_mail_message,
)

LINK = "https://app.example/reset-password?token=abc&lang=en"
RESET = {
    "de-CH": MailText(
        "Passwort zurücksetzen", "Guten Tag {name},", "Der Link gilt {hours} Stunde.", "Neu", "Ignorieren."
    ),
    "en": MailText("Reset your password", "Hi {name},", "The link works for {hours} hour.", "Choose", "Ignore it."),
    "fr": MailText("Réinitialiser", "Bonjour {name},", "Le lien vaut {hours} heure.", "Choisir", "Ignorez."),
}


@pytest.mark.parametrize(
    ("locale", "subject"),
    [
        ("en", "Reset your password"),
        ("EN-gb", "Reset your password"),
        ("fr-CH", "Réinitialiser"),
        ("de-CH", "Passwort zurücksetzen"),
        ("de", "Passwort zurücksetzen"),
        ("de_ch", "Passwort zurücksetzen"),
        ("it", "Passwort zurücksetzen"),
        ("", "Passwort zurücksetzen"),
        (None, "Passwort zurücksetzen"),
    ],
)
def test_pick_full_tags(locale: str | None, subject: str) -> None:
    """Kurvenschmiede keys its tables by the UI's codes; an unknown locale reads German."""
    assert pick(locale, RESET).subject == subject


def test_pick_bare_languages_and_the_english_floor() -> None:
    """keksdose keys by bare language; a table without German falls to English."""
    keksdose = {"de": "Hallo", "en": "Hi", "fr": "Bonjour"}
    assert pick("de-CH", keksdose) == "Hallo" and pick("fr-CH", keksdose) == "Bonjour"
    assert pick("hu", keksdose) == "Hallo"
    assert pick("hu", {"en": "Hi", "fr": "Bonjour"}) == "Hi"
    assert FALLBACK_LOCALES == ("de-CH", "de", "en")
    assert pick("zh", {"fr": "Bonjour", "it": "Ciao"}, fallback=("it",)) == "Ciao"
    with pytest.raises(KeyError, match="no text for 'zh'"):
        pick("zh", {"fr": "Bonjour"})


def test_pick_entry_answers_the_key_it_chose() -> None:
    """The mail's language is the ROW's, not the recipient's, when the chain fell back."""
    assert pick_entry("fr-CH", RESET)[0] == "fr"
    assert pick_entry("it", RESET) == ("de-CH", RESET["de-CH"])
    assert pick_entry("de", {"DE_ch": "Hallo"}) == ("DE_ch", "Hallo"), "as written in the table"
    with pytest.raises(KeyError):
        pick_entry("zh", {"fr": "Bonjour"})


def test_render_message_is_a_whole_document() -> None:
    """Kurvenschmiede's Outlook finding: a bare fragment, no ``<html lang>`` and no
    ``<title>``, is one more mark against a mail."""
    html, _ = render_message(
        subject="Reset <your> password", lang="de-CH", intro="i", body="b", cta="c", link=LINK, outro="o"
    )
    assert html.startswith(
        '<!doctype html><html lang="de-CH"><head><meta charset="utf-8">'
        "<title>Reset &lt;your&gt; password</title></head><body><p>i</p>"
    )
    assert html.endswith("</p></body></html>")
    zh, _ = render_message(subject="s", lang="zh_Hans", intro="i", body="b", cta="c", link=LINK, outro="o")
    assert '<html lang="zh-Hans">' in zh


@pytest.mark.parametrize("lang", ["", "d", 'de" onload="x', "de CH", "de-", "deutsch"])
def test_render_message_refuses_a_language_that_is_not_a_tag(lang: str) -> None:
    with pytest.raises(ValueError, match="language"):
        render_message(subject="s", lang=lang, intro="i", body="b", cta="c", link=LINK, outro="o")


def test_render_message_escapes_everything_but_the_text_half() -> None:
    """keksdose ``email.py:50``: a name like ``<a href=…>`` must not become a link in the app's own mail."""
    html, text = render_message(
        subject="Reset",
        lang="en",
        intro='Hi <a href="https://evil.example">Reset here</a>,',
        body="Body & more",
        cta="Choose <b>",
        link=LINK,
        outro="Bye",
    )
    assert '<a href="https://evil.example">' not in html
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in html
    assert 'href="https://app.example/reset-password?token=abc&amp;lang=en"' in html, "& in an attribute"
    assert "Choose &lt;b&gt;" in html and "Body &amp; more" in html
    assert text == f'Hi <a href="https://evil.example">Reset here</a>,\n\nBody & more\n\nChoose <b>: {LINK}\n\nBye'


@pytest.mark.parametrize("lang", ["fr", "fr-CH", "FR_ca"])
def test_render_message_sets_the_french_space_before_the_colon(lang: str) -> None:
    """keksdose's 0.33 report: French takes a no-break space (U+00A0) before the colon, as
    ui-kit's French catalogue writes it; the HTML half has no colon to set."""
    html, text = render_message(subject="s", lang=lang, intro="i", body="b", cta="Choisir", link=LINK, outro="o")
    assert f"\n\nChoisir\u00a0: {LINK}\n\n" in text
    assert "\u00a0" not in html
    for other in ("en", "de-CH", "it", "es", "hu", "zh-Hans"):
        _, plain = render_message(subject="s", lang=other, intro="i", body="b", cta="Choose", link=LINK, outro="o")
        assert f"\n\nChoose: {LINK}\n\n" in plain and "\u00a0" not in plain
    french = render_mail("fr-CH", RESET, link=LINK, name="Ada", hours=1)
    assert f"Choisir\u00a0: {LINK}" in french.text


@pytest.mark.parametrize("link", ["javascript:alert(1)", "/relative", "", 'ftp://x"'])
def test_render_message_refuses_a_link_that_is_not_http(link: str) -> None:
    with pytest.raises(ValueError, match="http"):
        render_message(subject="s", lang="en", intro="i", body="b", cta="c", link=link, outro="o")


def test_mail_text_fills_the_template_only() -> None:
    """Kurvenschmiede: ``str.format`` on the template, so braces in a name stay text."""
    message = pick("en", RESET).render(link=LINK, lang="en", name="Ada {hours} <Example>", hours=1)
    assert message.subject == "Reset your password"
    assert message.text.startswith("Hi Ada {hours} <Example>,\n\nThe link works for 1 hour.")
    assert message.html is not None and "Hi Ada {hours} &lt;Example&gt;," in message.html
    assert "<title>Reset your password</title>" in message.html
    with pytest.raises(KeyError):
        pick("en", RESET).render(link=LINK, lang="en", name="Ada")


def test_render_mail_writes_the_rows_own_language() -> None:
    """Italian falls back to the German row, and the document says ``de-CH``, not ``it``."""
    message = render_mail("it-CH", RESET, link=LINK, name="Ada", hours=1)
    assert message.subject == "Passwort zurücksetzen"
    assert message.html is not None and message.html.startswith('<!doctype html><html lang="de-CH">')
    french = render_mail("fr-CH", RESET, link=LINK, name="Ada", hours=1)
    assert french.html is not None and '<html lang="fr">' in french.html
    english = render_mail("hu", {"en": RESET["en"]}, link=LINK, fallback=("en",), name="Ada", hours=1)
    assert english.subject == "Reset your password"


async def test_the_console_mailer_logs_the_link(caplog: pytest.LogCaptureFixture) -> None:
    mailer: Mailer = ConsoleMailer()
    message = MailMessage(subject="Confirm", text=f"Confirm: {LINK}")
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.mail"):
        assert await mailer.send("ada@example.com", message, kind="verification")
    assert LINK in caplog.text and "verification" in caplog.text and "ada@example.com" in caplog.text
    caplog.clear()
    loud = ConsoleMailer(level=logging.WARNING)
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.mail"):
        assert await loud.send("ada@example.com", message)
    assert caplog.records[0].levelno == logging.WARNING


def test_the_account_notices_have_their_words() -> None:
    """KS S5: the account mails with no token. ``email_change_notice`` is the three apps'
    word already; the deletion mail is keksdose's ``account_deletion`` (kastlan sent
    ``deletion``, Kurvenschmiede ``deletion_request``); ``work_transferred_notice`` tells an
    active former owner that an admin handed their work on (notifications §9.3). No word
    repeats a token's."""
    assert [str(kind) for kind in AccountNoticeKind] == [
        "email_change_notice",
        "account_deletion",
        "work_transferred_notice",
        "feedback_published",
    ]
    assert not {str(kind) for kind in AccountNoticeKind} & {str(kind) for kind in OneTimeTokenKind}


async def test_a_notice_is_sent_and_logged_under_its_word(caplog: pytest.LogCaptureFixture) -> None:
    """The kind is what an operator greps the logs and Resend's ``Idempotency-Key`` for."""
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.mail"):
        assert await ConsoleMailer().send("ada@example.com", MESSAGE, kind=AccountNoticeKind.ACCOUNT_DELETION)
    assert "[email:console] account_deletion to=" in caplog.text
    resend = _Resend(200)
    assert await resend.client().send("ada@example.com", MESSAGE, kind=AccountNoticeKind.EMAIL_CHANGE_NOTICE)
    assert resend.requests[0].headers["Idempotency-Key"].startswith("email_change_notice-")


class _Resend:
    """A fake Resend: answers from a script, records every request."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"id": "m-1"} if answer < 400 else {"message": "no"})

    def client(self) -> ResendClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return ResendClient(api_key="re_test", from_address="App <noreply@app.example>", client=http, retry_delay=0)


MESSAGE = MailMessage(subject="Confirm", text="Confirm: https://app.example/v", html="<p>Confirm</p>")


async def test_resend_posts_one_mail() -> None:
    resend = _Resend(200)
    assert await resend.client().send("ada@example.com", MESSAGE, kind="verification")
    (request,) = resend.requests
    assert str(request.url) == RESEND_ENDPOINT and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer re_test"
    assert request.headers["Idempotency-Key"].startswith("verification-")
    assert json.loads(request.content) == {
        "from": "App <noreply@app.example>",
        "to": ["ada@example.com"],
        "subject": "Confirm",
        "text": "Confirm: https://app.example/v",
        "html": "<p>Confirm</p>",
    }


async def test_resend_sets_reply_to() -> None:
    """Kurvenschmiede: the mail comes from noreply@, a reply must reach a person."""
    resend = _Resend(200, 200)
    http = httpx.AsyncClient(transport=httpx.MockTransport(resend))
    client = ResendClient(
        api_key="re_test",
        from_address="App <noreply@app.example>",
        client=http,
        reply_to=support_address("App <noreply@app.example>"),
    )
    assert await client.send("ada@example.com", MESSAGE)
    assert json.loads(resend.requests[0].content)["reply_to"] == ["support@app.example"]
    blank = ResendClient(api_key="re_test", from_address="noreply@app.example", client=http, reply_to="  ")
    assert await blank.send("ada@example.com", MESSAGE)
    assert "reply_to" not in json.loads(resend.requests[1].content)


@pytest.mark.parametrize(
    ("sender", "support"),
    [
        ("noreply@kurvenschmiede.app", "support@kurvenschmiede.app"),
        ("Kurvenschmiede <noreply@Kurvenschmiede.APP>", "support@kurvenschmiede.app"),
        ("  keksdose <noreply@mail.keksdose.app>  ", "support@mail.keksdose.app"),
    ],
)
def test_support_address_is_support_on_the_senders_domain(sender: str, support: str) -> None:
    assert support_address(sender) == support
    assert support_address(sender, local="help").startswith("help@")


@pytest.mark.parametrize("sender", ["", "noreply", "noreply@localhost", "App <noreply@>", "a@b c.example"])
def test_support_address_needs_a_domain(sender: str) -> None:
    with pytest.raises(ValueError, match="no domain"):
        support_address(sender)


async def test_resend_sends_plain_text_without_html() -> None:
    resend = _Resend(200)
    assert await resend.client().send("ada@example.com", MailMessage(subject="s", text="t"))
    assert "html" not in json.loads(resend.requests[0].content)


@pytest.mark.parametrize(
    ("answers", "sent", "attempts"),
    [
        ((503, 200), True, 2),
        ((429, 200), True, 2),
        ((httpx.ConnectError("down"), 200), True, 2),
        ((httpx.ReadTimeout("slow"), 502), False, 2),
        ((500, 500), False, 2),
        ((403,), False, 1),
        ((422,), False, 1),
        ((httpx.DecodingError("garbled"),), False, 1),
    ],
)
async def test_resend_retries_once_and_only_where_it_can_help(
    answers: tuple[int | Exception, ...], sent: bool, attempts: int, caplog: pytest.LogCaptureFixture
) -> None:
    resend = _Resend(*answers)
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.mail"):
        assert await resend.client().send("ada@example.com", MESSAGE, kind="password_reset") is sent
    assert len(resend.requests) == attempts
    keys = {request.headers["Idempotency-Key"] for request in resend.requests}
    assert len(keys) == 1, "a retry is the same mail to Resend"
    assert "ada@example.com" not in caplog.text, "the log never names the recipient"


async def test_resend_without_an_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each send opens and closes its own client."""
    resend = _Resend(200)
    real = httpx.AsyncClient

    def with_fake_transport(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs == {"timeout": 3.0}
        return real(transport=httpx.MockTransport(resend))

    monkeypatch.setattr(httpx, "AsyncClient", with_fake_transport)
    client = ResendClient(api_key="re_test", from_address="noreply@app.example", timeout=3.0)
    assert await client.send("ada@example.com", MESSAGE)
    assert len(resend.requests) == 1


def test_resend_needs_its_key_and_sender() -> None:
    with pytest.raises(ValueError, match="API key"):
        ResendClient(api_key="", from_address="noreply@app.example")
    with pytest.raises(ValueError, match="from address"):
        ResendClient(api_key="re_test", from_address="")


def test_without_the_extra_resend_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import httpx`` fail as if it were not installed;
    everything else in the module works without it."""
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ImportError, match=r"eifi1-server-kit\[mail\]"):
        ResendClient(api_key="re_test", from_address="noreply@app.example")
    assert render_mail("en", RESET, link=LINK, name="Ada", hours=1).subject == "Reset your password"


# ── a notification mail: one-click unsubscribe (notifications §3.6) ─────────

ONE_CLICK = "https://app.example/api/v1/notifications/unsubscribe/v1.NDI6c2hhcmVfZ3JhbnRlZA.mac"
PAGE = "https://app.example/unsubscribe?token=v1.NDI6c2hhcmVfZ3JhbnRlZA.mac&from=mail"
WAY_OUT = MailUnsubscribe(
    label="You get this because shared projects are on. Stop <these>", page=PAGE, one_click=ONE_CLICK
)


def test_the_list_unsubscribe_headers_are_rfc_8058s() -> None:
    assert list_unsubscribe_headers(ONE_CLICK) == {
        "List-Unsubscribe": f"<{ONE_CLICK}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example/api/v1/notifications/unsubscribe/t",
        "mailto:unsubscribe@app.example",
        "https://app.example/u/t>, <https://evil.example",
        "https://app.example/u/a b",
        "https://app.example/u/t\r\nBcc: someone@example.com",
        "https://app.example/ü",
        "",
    ],
)
def test_the_list_unsubscribe_url_is_https_and_one_header_value(url: str) -> None:
    with pytest.raises(ValueError, match="List-Unsubscribe URL"):
        list_unsubscribe_headers(url)


def test_an_unsubscribe_needs_its_links() -> None:
    with pytest.raises(ValueError, match="unsubscribe page is http"):
        MailUnsubscribe(label="Stop", page="/unsubscribe?token=t", one_click=ONE_CLICK)
    with pytest.raises(ValueError, match="List-Unsubscribe URL"):
        MailUnsubscribe(label="Stop", page=PAGE, one_click="http://localhost:8000/api/v1/notifications/unsubscribe/t")
    development = MailUnsubscribe(label="Stop", page="http://localhost:5173/unsubscribe?token=t", one_click=None)
    assert development.headers is None, "RFC 8058 takes https only: the footer line alone"
    assert WAY_OUT.headers == list_unsubscribe_headers(ONE_CLICK)


def test_the_footer_line_is_last_escaped_and_typeset_like_the_action_line() -> None:
    html, text = render_message(
        subject="s", lang="en", intro="i", body="b", cta="Open", link=LINK, outro="o", unsubscribe=WAY_OUT
    )
    assert html.endswith(
        '<p style="color:#64748b;font-size:12px">o</p>'
        '<p style="color:#64748b;font-size:12px"><a href="https://app.example/unsubscribe?token='
        'v1.NDI6c2hhcmVfZ3JhbnRlZA.mac&amp;from=mail">You get this because shared projects are on. Stop '
        "&lt;these&gt;</a></p></body></html>"
    )
    assert text.endswith(f"\n\no\n\nYou get this because shared projects are on. Stop <these>: {PAGE}")
    _, french = render_message(
        subject="s", lang="fr-CH", intro="i", body="b", cta="Ouvrir", link=LINK, outro="o", unsubscribe=WAY_OUT
    )
    assert french.endswith(f"Stop <these> : {PAGE}")
    plain_html, plain_text = render_message(subject="s", lang="en", intro="i", body="b", cta="c", link=LINK, outro="o")
    assert PAGE not in plain_text and "unsubscribe" not in plain_html, "account mail has none"


def test_render_mail_makes_a_notification_mail() -> None:
    message = render_mail("de-CH", RESET, link=LINK, name="Ada", hours=1, unsubscribe=WAY_OUT)
    assert message.headers == list_unsubscribe_headers(ONE_CLICK)
    assert message.text.endswith(f"Stop <these>: {PAGE}")
    assert message.html is not None and message.html.startswith('<!doctype html><html lang="de-CH">')
    assert render_mail("de-CH", RESET, link=LINK, name="Ada", hours=1).headers is None
    development = MailUnsubscribe(label="Stop", page="http://localhost:5173/unsubscribe?token=t", one_click=None)
    local = render_mail("en", RESET, link=LINK, name="Ada", hours=1, unsubscribe=development)
    assert local.headers is None and local.text.endswith("Stop: http://localhost:5173/unsubscribe?token=t")


@pytest.mark.parametrize(
    "headers",
    [{"List Unsubscribe": "<x>"}, {"": "x"}, {"X-A:B": "x"}, {"X-Note": "a\r\nBcc: b@example.com"}, {"X-N": "a\nb"}],
)
def test_a_header_is_a_name_and_one_line(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="mail header"):
        MailMessage(subject="s", text="t", headers=headers)


async def test_resend_sends_the_headers_in_its_payload() -> None:
    resend = _Resend(200)
    message = render_mail("en", RESET, link=LINK, name="Ada", hours=1, unsubscribe=WAY_OUT)
    assert await resend.client().send("ada@example.com", message, kind="share_granted")
    (request,) = resend.requests
    assert json.loads(request.content)["headers"] == {
        "List-Unsubscribe": f"<{ONE_CLICK}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert "List-Unsubscribe" not in request.headers, "in the mail, not on the request to Resend"
    assert request.headers["Idempotency-Key"].startswith("share_granted-"), "sent under its type id"


async def test_the_console_mailer_logs_the_headers(caplog: pytest.LogCaptureFixture) -> None:
    message = render_mail("en", RESET, link=LINK, name="Ada", hours=1, unsubscribe=WAY_OUT)
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.mail"):
        assert await ConsoleMailer().send("ada@example.com", message, kind="share_granted")
    assert "[email:console] share_granted to=ada@example.com" in caplog.text
    assert f"List-Unsubscribe: <{ONE_CLICK}>\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\n" in caplog.text


# ── a job's notification mail: its own Idempotency-Key (notifications §9.4) ─

KEY = "invoices_overdue:42:2026-10-13:17:2026-10-14"


def test_a_message_has_no_key_unless_given_one() -> None:
    assert MESSAGE.idempotency_key is None
    assert render_mail("en", RESET, link=LINK, name="Ada", hours=1, unsubscribe=WAY_OUT).idempotency_key is None
    assert replace(MESSAGE, idempotency_key=KEY).idempotency_key == KEY
    assert MailMessage(subject="s", text="t", idempotency_key="x" * 256).idempotency_key == "x" * 256


@pytest.mark.parametrize("key", ["", " ", " a", "a b", "a\r\nBcc: b@example.com", "a\tb", "Zürich", "x" * 257])
def test_an_idempotency_key_is_one_header_line_of_at_most_256(key: str) -> None:
    """Resend takes 1–256 characters (a 400 ``invalid_idempotency_key`` otherwise), and the
    key is a request header's value: refused here, not at the send, which never raises."""
    with pytest.raises(ValueError, match="idempotency key is 1–256 printable ASCII"):
        MailMessage(subject="s", text="t", idempotency_key=key)


async def test_resend_sends_the_messages_key_on_both_tries() -> None:
    """Two runs on one day send one mail: Resend answers the second from the first."""
    resend = _Resend(503, 200)
    assert await resend.client().send("ada@example.com", replace(MESSAGE, idempotency_key=KEY), kind="invoices_overdue")
    assert [request.headers["Idempotency-Key"] for request in resend.requests] == [KEY, KEY]
    payload = json.loads(resend.requests[0].content)
    assert "idempotency_key" not in payload and "headers" not in payload, "a request header, not a mail header"


async def test_resend_without_a_key_sends_a_new_one_per_mail() -> None:
    resend = _Resend(200, 200)
    client = resend.client()
    assert await client.send("ada@example.com", MESSAGE, kind="verification")
    assert await client.send("ada@example.com", MESSAGE, kind="verification")
    first, second = (request.headers["Idempotency-Key"] for request in resend.requests)
    assert first != second and first.startswith("verification-") and second.startswith("verification-")


async def test_a_409_on_the_key_is_not_sent_and_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    """Resend's 409: the key's first request is still running
    (``concurrent_idempotent_requests``) — it has the mail, and its run writes the marker —
    or the key went with another body today (``invalid_idempotent_request``); tomorrow's
    key is a new one."""
    resend = _Resend(409)
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.mail"):
        sent = await resend.client().send("ada@example.com", replace(MESSAGE, idempotency_key=KEY), kind="x_y")
    assert sent is False and len(resend.requests) == 1
    assert "Resend refused the x_y mail (attempt 1): 409" in caplog.text


async def test_the_console_mailer_ignores_the_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.mail"):
        assert await ConsoleMailer().send("ada@example.com", replace(MESSAGE, idempotency_key=KEY), kind="x_y")
    assert "[email:console] x_y to=ada@example.com" in caplog.text and KEY not in caplog.text


# --- the admin's test mail (admin-harmonization §9.2) --------------------------------------

SENT_AT = datetime(2026, 10, 19, 16, 5, tzinfo=timezone(timedelta(hours=2)))
SEVEN = ("de-CH", "en", "es", "fr", "hu", "it", "zh")


@pytest.mark.parametrize("locale", SEVEN)
def test_the_test_mail_renders_in_each_language(locale: str) -> None:
    """Subject, the transport and the time (in UTC) in every language; a whole document."""
    message = test_mail_message(locale, app_name="Kurvenschmiede", backend="resend", sent_at=SENT_AT)
    assert "Kurvenschmiede" in message.subject
    for part in (message.text, message.html or ""):
        assert "Kurvenschmiede" in part and "resend" in part and "2026-10-19 14:05 UTC" in part
    assert message.html is not None and message.html.startswith(f'<!doctype html><html lang="{locale}">')
    assert f"<title>{message.subject}</title>" in message.html
    assert "{" not in message.text and "{" not in message.html, "every placeholder filled"
    assert message.headers is None and message.idempotency_key is None


def test_the_test_mail_says_what_it_is_and_falls_back() -> None:
    english = test_mail_message("en", app_name="kastlan", backend="smtp", sent_at=SENT_AT)
    assert english.subject == "Test mail from kastlan"
    assert english.text == (
        "This is a test mail.\n\n"
        "kastlan sent it through the mail transport “smtp” on 2026-10-19 14:05 UTC.\n\n"
        "If it reached you, the app's mail works. There is no need to reply."
    )
    # The language chain of every mail: a region finds its language, an unknown one German.
    assert test_mail_message("fr-CH", app_name="a", backend="b", sent_at=SENT_AT).subject == "E-mail de test de a"
    for unknown in (None, "", "xx"):
        assert test_mail_message(unknown, app_name="a", backend="b", sent_at=SENT_AT).subject == "Testmail von a"


def test_the_test_mail_names_no_recipient_and_escapes_its_values() -> None:
    """The route's log and the mail never carry the address; an app's name is text."""
    message = test_mail_message("en", app_name="<b>A&B</b>", backend="console", sent_at=SENT_AT.astimezone(UTC))
    assert "@" not in message.text and "@" not in (message.html or "")
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in (message.html or "") and "<b>" not in (message.html or "")
    assert message.subject == "Test mail from <b>A&B</b>", "the subject header is plain text"
    braces = test_mail_message("en", app_name="{backend}", backend="smtp", sent_at=SENT_AT)
    assert braces.subject == "Test mail from {backend}", "a value is never a format field"


def test_the_test_mail_always_has_its_html_half() -> None:
    """keksdose N2: the return type says ``html`` is a ``str``, so an app whose own send
    takes ``html: str`` needs no ``message.html or ""``. Still a :class:`MailMessage`."""
    message = test_mail_message("en", app_name="a", backend="b", sent_at=SENT_AT)
    assert_type(message.html, str)
    assert isinstance(message, HtmlMailMessage) and isinstance(message, MailMessage)
    assert message.html.startswith("<!doctype html>")
    keyed = replace(message, idempotency_key="k1")
    assert_type(keyed, HtmlMailMessage)
    assert keyed.html == message.html and keyed.idempotency_key == "k1"
    with pytest.raises(TypeError):
        HtmlMailMessage(subject="s", text="t")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="mail header"):
        HtmlMailMessage(subject="s", text="t", html="<p>t</p>", headers={"Bad Name": "v"})


def test_the_test_mail_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="time zone"):
        test_mail_message("en", app_name="a", backend="b", sent_at=datetime(2026, 10, 19, 14, 5))


def test_the_test_mail_kind_limit_and_answer() -> None:
    assert MAIL_TEST_KIND == "mail_test"
    assert Budget(3, 600) == MAIL_TEST_LIMIT
    assert MailTestResult(sent=True, mail_backend="console").model_dump() == {"sent": True, "mail_backend": "console"}
    with pytest.raises(ValueError):
        MailTestResult.model_validate({"sent": True, "mail_backend": "resend", "to": "a@example.com"})
    assert test_mail_message.__test__ is False  # type: ignore[attr-defined]
