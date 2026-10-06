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
    render_message,
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


def test_render_message_escapes_everything_but_the_text_half() -> None:
    """keksdose ``email.py:50``: a name like ``<a href=…>`` must not become a link in the app's own mail."""
    html, text = render_message(
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
        render_message(intro="i", body="b", cta="c", link=link, outro="o")


def test_mail_text_fills_the_template_only() -> None:
    """Kurvenschmiede: ``str.format`` on the template, so braces in a name stay text."""
    message = pick("en", RESET).render(link=LINK, name="Ada {hours} <Example>", hours=1)
    assert message.subject == "Reset your password"
    assert message.text.startswith("Hi Ada {hours} <Example>,\n\nThe link works for 1 hour.")
    assert message.html is not None and "Hi Ada {hours} &lt;Example&gt;," in message.html
    with pytest.raises(KeyError):
        pick("en", RESET).render(link=LINK, name="Ada")


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
    assert pick("en", RESET).render(link=LINK, name="Ada", hours=1).subject == "Reset your password"
