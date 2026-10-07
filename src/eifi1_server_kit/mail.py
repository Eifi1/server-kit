"""Outbound account mail: the words per language, one layout, two transports.

``docs/auth-harmonization.md`` §8 in ``Eifi1/ui-kit``. Lifted from keksdose
``backend/keksdose/infrastructure/email.py`` and ``domain/server_text.py`` and from
Kurvenschmiede ``infrastructure/email.py`` (``MailText``), which agree on everything
here; kastlan built its own on the same shape.

* **The words are the app's.** A mail's texts are a table per language of
  :class:`MailText` rows — the subject and the five parts :func:`render_message` lays
  out — and :func:`pick` chooses the row for the recipient's locale. Which mails exist
  and what they say stays in each app (§8 "App-side: the texts of the mails").
* **One layout, escaped at the boundary** (:func:`render_message`): a greeting is built
  from a person's name, and a name is free text, so an account called
  ``<a href="…">Reset here</a>`` must not put that anchor into the app's own mail.
* **A whole HTML document**, with the language and a title: Outlook junked a fully
  authenticated Kurvenschmiede reset mail (SCL 5 — a new domain on shared sending IPs),
  and a bare fragment with no ``<html lang>`` and no ``<title>`` is one more mark against
  a mail. :func:`render_mail` picks the row AND its language in one call.
* **A ``Reply-To``** (:class:`ResendClient`'s ``reply_to``): the mails come from a
  ``noreply@`` address, and a reply must reach a person. Each app's ``*_EMAIL_REPLY_TO``
  setting defaults to ``support@<its domain>`` (:func:`support_address`), which
  Cloudflare Email Routing forwards on all three domains.
* **Two transports behind one protocol** (:class:`Mailer`): :class:`ResendClient` for
  production — the ``mail`` extra (``eifi1-server-kit[mail]``, httpx) — and
  :class:`ConsoleMailer` for a fresh checkout, which logs the message, link and all.
  **Sending never raises**: a sign-up must not fail because a provider is down, and every
  link can be asked for again. ``send`` answers whether the mail went; the caller decides
  who is told.

Not here yet: ``List-Unsubscribe`` and anything else a NOTIFICATION mail needs — that is
the notifications round. These are transactional mails the recipient asked for.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import httpx

__all__ = [
    "FALLBACK_LOCALES",
    "RESEND_ENDPOINT",
    "SUPPORT_LOCAL_PART",
    "TIMEOUT_SECONDS",
    "ConsoleMailer",
    "MailMessage",
    "MailText",
    "Mailer",
    "ResendClient",
    "pick",
    "pick_entry",
    "render_mail",
    "render_message",
    "support_address",
]

logger = logging.getLogger("eifi1_server_kit.mail")

#: Resend's send endpoint (keksdose, Kurvenschmiede, kastlan).
RESEND_ENDPOINT = "https://api.resend.com/emails"
#: Per attempt (keksdose ``TIMEOUT_SECONDS``).
TIMEOUT_SECONDS = 10.0
#: Where :func:`pick` goes when the recipient's language has no row: the UI's German —
#: the apps' home language and the kit's fallback (i18n H7) — then English.
FALLBACK_LOCALES: tuple[str, ...] = ("de-CH", "de", "en")
#: The local part :func:`support_address` puts on the sender's domain.
SUPPORT_LOCAL_PART = "support"

#: A language tag as ``<html lang>`` takes it (BCP 47's shape: ``de-CH``, ``en``,
#: ``zh-Hans``) — checked, because it is written into an attribute.
_LANG_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def _language(tag: str) -> str:
    return tag.split("-", 1)[0]


def pick_entry[T](
    locale: str | None, texts: Mapping[str, T], *, fallback: Sequence[str] = FALLBACK_LOCALES
) -> tuple[str, T]:
    """``(key, row)``: the row :func:`pick` chooses and the key it is under in ``texts``,
    as written there — the language the mail is actually written in, which is not the
    recipient's when the chain fell back. :func:`render_mail` puts it in ``<html lang>``."""
    by_tag = {key.strip().lower().replace("_", "-"): (key, value) for key, value in texts.items()}
    by_language: dict[str, tuple[str, T]] = {}
    for tag, entry in by_tag.items():
        by_language.setdefault(_language(tag), entry)
    requested = (locale or "").strip().lower().replace("_", "-")
    chain = [requested, _language(requested)] if requested else []
    chain += [tag.lower() for tag in fallback]
    for tag in chain:
        if tag in by_tag:
            return by_tag[tag]
        if "-" not in tag and tag in by_language:
            return by_language[tag]
    raise KeyError(f"no text for {locale!r} nor any of {', '.join(fallback)}; have {', '.join(texts)}")


def pick[T](locale: str | None, texts: Mapping[str, T], *, fallback: Sequence[str] = FALLBACK_LOCALES) -> T:
    """The row of ``texts`` for ``locale``, through a fallback chain (keksdose
    ``server_text.pick``, Kurvenschmiede ``languages.language_of``).

    ``texts`` may be keyed by full tags (Kurvenschmiede: ``de-CH``, ``en``, …) or by bare
    languages (keksdose: ``de``, ``en``, …); case does not matter. The chain: the exact
    tag, then its language (``fr-CH`` → ``fr``, and ``de`` finds a ``de-CH`` row), then
    ``fallback`` — ``de-CH`` → ``de`` → ``en`` — so an unknown or missing locale reads
    German, as it does on every other surface. A table with none of them is a
    :class:`KeyError`: a programming error a unit test that renders every row catches.
    :func:`pick_entry` also answers the key it chose.
    """
    return pick_entry(locale, texts, fallback=fallback)[1]


def _lang_attribute(lang: str) -> str:
    tag = lang.strip().replace("_", "-")
    if not _LANG_TAG.match(tag):
        raise ValueError(f"a mail's language is a tag like 'de-CH' or 'en': {lang[:20]!r}")
    return tag


def render_message(
    *, subject: str, lang: str, intro: str, body: str, cta: str, link: str, outro: str
) -> tuple[str, str]:
    """``(html, text)`` for a transactional mail: greeting, one paragraph, one link, footer
    (keksdose ``infrastructure/email.py:50``).

    The one place a mail's layout is decided; the callers own the words. ``html`` is a
    WHOLE document — ``<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
    <title>{subject}</title></head><body>…</body></html>`` — because a bare fragment is one
    more mark against a mail with a spam filter (Kurvenschmiede's Outlook finding).
    ``lang`` is the language the words are in (``de-CH``, ``en``; :func:`pick_entry`'s
    key), ``subject`` the mail's subject.

    **Escaped here**, not by each caller: ``intro`` is built from a name, and a name may
    hold markup; the subject may hold one too. The plain-text half is not markup and stays
    exactly as written.

    ``link`` must be ``http(s)``; it is server-built from the app's base URL and a token
    the server minted. Unlike keksdose it is escaped inside the ``href`` too: ``&amp;`` in
    an attribute IS ``&`` to every HTML parser, so a query string survives, and a link can
    no longer close the attribute.
    """
    if not link.startswith(("https://", "http://")):
        raise ValueError(f"a mail link is http(s): {link[:40]!r}")
    content = (
        f"<p>{escape(intro)}</p><p>{escape(body)}</p>"
        f'<p><a href="{escape(link, quote=True)}">{escape(cta)}</a></p>'
        f'<p style="color:#64748b;font-size:12px">{escape(outro)}</p>'
    )
    html = (
        f'<!doctype html><html lang="{escape(_lang_attribute(lang), quote=True)}">'
        f'<head><meta charset="utf-8"><title>{escape(subject)}</title></head>'
        f"<body>{content}</body></html>"
    )
    text = f"{intro}\n\n{body}\n\n{cta}: {link}\n\n{outro}"
    return html, text


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One mail, ready to send: ``html`` may be ``None`` for a plain-text mail (kastlan)."""

    subject: str
    text: str
    html: str | None = None


@dataclass(frozen=True, slots=True)
class MailText:
    """One mail's words in one language (Kurvenschmiede ``MailText``).

    Keep a table per mail — ``{"de-CH": MailText(…), "en": MailText(…), …}`` — and render
    the recipient's row with :func:`render_mail`, which picks it and knows its language;
    or choose it with :func:`pick_entry` and call :meth:`render`. Any part may hold
    ``{placeholders}`` (``{name}``, ``{hours}``), filled by :meth:`render`.
    """

    subject: str
    intro: str
    body: str
    cta: str
    outro: str

    def render(self, *, link: str, lang: str, **values: object) -> MailMessage:
        """The mail with ``values`` filled in and the layout of :func:`render_message`;
        ``lang`` is the language this row is written in (its key in the table), so no
        template may use a ``{lang}`` placeholder.

        ``str.format`` runs on the TEMPLATE only, so braces in a person's name are text,
        never a format field; the name is HTML-escaped by the layout. A literal brace in a
        template is doubled (``{{``).
        """
        subject, intro, body, cta, outro = (
            part.format_map(values) for part in (self.subject, self.intro, self.body, self.cta, self.outro)
        )
        html, text = render_message(subject=subject, lang=lang, intro=intro, body=body, cta=cta, link=link, outro=outro)
        return MailMessage(subject=subject, text=text, html=html)


def render_mail(
    locale: str | None,
    texts: Mapping[str, MailText],
    *,
    link: str,
    fallback: Sequence[str] = FALLBACK_LOCALES,
    **values: object,
) -> MailMessage:
    """The recipient's mail from a table: :func:`pick_entry` for ``locale``, then
    :meth:`MailText.render` with the row's own language in ``<html lang>``::

        message = render_mail(user.locale, RESET_TEXTS, link=link, name=greeting, hours=1)
    """
    lang, text = pick_entry(locale, texts, fallback=fallback)
    return text.render(link=link, lang=lang, **values)


class Mailer(Protocol):
    """What sends a mail: :class:`ResendClient`, :class:`ConsoleMailer`, or a test's fake."""

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        """Send one mail; whether it went out. Never raises.

        ``kind`` (``verification``, ``password_reset``, ``invitation``) is what the mail
        is FOR — a small enum fit for a log line or a metric label, which the recipient's
        address never is.
        """
        ...


class ConsoleMailer:
    """The development transport: log the mail, link included, instead of sending it.

    A fresh checkout needs no credentials — you verify an account by reading the backend
    log (keksdose's default). kastlan logs at ``WARNING`` so the line shows under its
    default level; pass ``level``. Never in production: the link in the log is a
    credential.
    """

    def __init__(self, *, level: int = logging.INFO, log: logging.Logger = logger) -> None:
        self._level = level
        self._log = log

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        self._log.log(self._level, "[email:console] %s to=%s subject=%s\n%s", kind, to, message.subject, message.text)
        return True


def _require_httpx() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "ResendClient needs httpx: install the kit with its mail extra, eifi1-server-kit[mail]."
        ) from exc


def support_address(from_address: str, *, local: str = SUPPORT_LOCAL_PART) -> str:
    """``support@<the sender's domain>``: the default of each app's ``*_EMAIL_REPLY_TO``
    setting. ``"Kurvenschmiede <noreply@kurvenschmiede.app>"`` →
    ``"support@kurvenschmiede.app"``. Cloudflare Email Routing forwards ``support@`` on all
    three apps' domains (2026-10-07); the mails come from ``noreply@``, which nobody
    reads."""
    address = from_address.strip()
    if address.endswith(">") and "<" in address:
        address = address[address.rindex("<") + 1 : -1]
    _, at, domain = address.rpartition("@")
    domain = domain.strip().lower()
    if not at or "." not in domain or any(char.isspace() for char in domain):
        raise ValueError(f"no domain in the sender address {from_address!r}")
    return f"{local}@{domain}"


class ResendClient:
    """One POST to Resend's HTTP API per mail (keksdose, Kurvenschmiede ``send_email``).

    ``api_key`` and ``from_address`` are injected — the app's settings, never read here;
    ``from_address`` must be on a domain verified with Resend. ``reply_to`` is where a
    reply goes — the app's ``*_EMAIL_REPLY_TO`` setting, by default
    ``support_address(from_address)``; ``None`` or empty sends none. ``client`` is an
    ``httpx.AsyncClient`` the app owns and closes (tests pass one with a
    ``MockTransport``); without it each send opens and closes its own.

    **At most one retry**, and only where a second attempt can help: a transport error (no
    connection, a timeout) or an answer of ``429`` / ``5xx``. A ``4xx`` — a bad key, an
    unverified sender — fails at once. Both attempts carry the same ``Idempotency-Key``, so
    a first attempt that did reach Resend before the timeout is not sent twice. No queue,
    no delivery tracking: a handful of mails, each awaited by somebody at the app.

    The log lines name the ``kind`` and Resend's answer, never the recipient.
    """

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        timeout: float = TIMEOUT_SECONDS,
        retry_delay: float = 0.5,
        endpoint: str = RESEND_ENDPOINT,
        client: httpx.AsyncClient | None = None,
        reply_to: str | None = None,
    ) -> None:
        _require_httpx()
        if not api_key:
            raise ValueError("ResendClient needs an API key")
        if not from_address:
            raise ValueError("ResendClient needs a from address")
        self._api_key = api_key
        self._from = from_address
        self._timeout = timeout
        self._retry_delay = retry_delay
        self._endpoint = endpoint
        self._client = client
        self._reply_to = (reply_to or "").strip() or None

    def _payload(self, to: str, message: MailMessage) -> dict[str, object]:
        payload: dict[str, object] = {
            "from": self._from,
            "to": [to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html is not None:
            payload["html"] = message.html
        if self._reply_to is not None:
            payload["reply_to"] = [self._reply_to]
        return payload

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        """Send ``message`` to ``to``; whether Resend accepted it. Never raises."""
        headers = {"Authorization": f"Bearer {self._api_key}", "Idempotency-Key": f"{kind}-{uuid.uuid4()}"}
        payload = self._payload(to, message)
        sent = await self._attempt(headers, payload, kind=kind, attempt=1)
        if sent is None:
            await asyncio.sleep(self._retry_delay)
            sent = await self._attempt(headers, payload, kind=kind, attempt=2)
        return sent is True

    async def _attempt(
        self, headers: dict[str, str], payload: dict[str, object], *, kind: str, attempt: int
    ) -> bool | None:
        """One POST: ``True`` sent, ``False`` refused for good, ``None`` worth one more try."""
        import httpx

        try:
            if self._client is not None:
                response = await self._client.post(self._endpoint, headers=headers, json=payload, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._endpoint, headers=headers, json=payload)
        except httpx.TransportError as exc:
            logger.warning("Could not reach Resend for the %s mail (attempt %d): %r", kind, attempt, exc)
            return None
        except httpx.HTTPError:
            logger.exception("The Resend call for the %s mail failed", kind)
            return False
        if response.status_code < 400:
            return True
        logger.error(
            "Resend refused the %s mail (attempt %d): %s %s",
            kind,
            attempt,
            response.status_code,
            response.text[:500],
        )
        return None if response.status_code == 429 or response.status_code >= 500 else False
