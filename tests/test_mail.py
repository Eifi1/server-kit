"""Outbound mail: the language chain, the escaped layout, and the two transports."""

from __future__ import annotations

import json
import logging
import sys

import httpx
import pytest

from eifi1_server_kit.mail import (
    FALLBACK_LOCALES,
    RESEND_ENDPOINT,
    ConsoleMailer,
    Mailer,
    MailMessage,
    MailText,
    ResendClient,
    pick,
    pick_entry,
    render_mail,
    render_message,
    support_address,
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
