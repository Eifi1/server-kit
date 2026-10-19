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
* **A word for every mail** — the ``kind`` it is sent and logged under, which an
  operator greps three apps' logs for: a mail with a one-time link by its token's
  :class:`~eifi1_server_kit.auth.OneTimeTokenKind`, billing's notices by
  :class:`~eifi1_server_kit.billing.BillingNoticeKind`, and the account notices with no
  token by :class:`AccountNoticeKind`.

* **A notification mail** (``docs/notifications-harmonization.md`` §3.6) is sent under its
  notification type's id and carries RFC 8058's one-click unsubscribe:
  :func:`list_unsubscribe_headers` on :attr:`MailMessage.headers`, and a footer line
  linking the app's unsubscribe page (:class:`MailUnsubscribe`, which
  :func:`render_mail` lays out and turns into the headers). Account mail and billing's
  notices carry none: the recipient asked for them, and has no switch to turn them off.
  One a job sends is owed until it went (§9.4): it carries
  :attr:`MailMessage.idempotency_key` from
  :meth:`~eifi1_server_kit.notifications.MailMarker.idempotency_key`, so two runs on one
  day send it once.
* **The admin's test mail** (``docs/admin-harmonization.md`` §9.2, 0.15):
  :func:`test_mail_message` in seven languages, sent under :data:`MAIL_TEST_KIND`
  through the app's own send, awaited inline (never an after-commit hook), and answered
  as :class:`MailTestResult`; :data:`MAIL_TEST_LIMIT` for the app's limiter. No router:
  each app's route is a few lines over its own transport.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from eifi1_server_kit.auth.limits import Budget

if TYPE_CHECKING:
    import httpx

__all__ = [
    "FALLBACK_LOCALES",
    "MAIL_TEST_KIND",
    "MAIL_TEST_LIMIT",
    "RESEND_ENDPOINT",
    "SUPPORT_LOCAL_PART",
    "TIMEOUT_SECONDS",
    "AccountNoticeKind",
    "ConsoleMailer",
    "HtmlMailMessage",
    "MailMessage",
    "MailTestResult",
    "MailText",
    "MailUnsubscribe",
    "Mailer",
    "ResendClient",
    "list_unsubscribe_headers",
    "pick",
    "pick_entry",
    "render_mail",
    "render_message",
    "support_address",
    "test_mail_message",
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
#: What goes before the colon of the plain text's link line, per language: French sets a
#: no-break space (U+00A0) before it, as ui-kit's French catalogue does (``Mixte\u00a0:``);
#: every other language none.
_BEFORE_COLON: Mapping[str, str] = {"fr": "\u00a0"}
#: A header's name: printable ASCII but the colon (RFC 5322 ``ftext``).
_HEADER_NAME = re.compile(r"[!-9;-~]+")
#: RFC 8058's second header: the body a mail client POSTs to the first one's URL.
_ONE_CLICK = "List-Unsubscribe=One-Click"
#: An ``Idempotency-Key``: 1–256 characters (Resend's range; outside it is a 400
#: ``invalid_idempotency_key``), printable ASCII with no space — one HTTP header value.
_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,256}")


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


def _check_link(link: str, what: str) -> None:
    if not link.startswith(("https://", "http://")):
        raise ValueError(f"{what} is http(s): {link[:40]!r}")


def list_unsubscribe_headers(url: str) -> dict[str, str]:
    """RFC 8058's two headers for a notification mail (notifications §3.6)::

        List-Unsubscribe: <https://app.example/api/v1/notifications/unsubscribe/v1.…>
        List-Unsubscribe-Post: List-Unsubscribe=One-Click

    ``url`` is the app's ``POST /notifications/unsubscribe/{token}``, which a mail client
    calls with no cookie and no page in between. A :class:`ValueError` unless it is
    ``https://`` (RFC 8058 takes nothing else) in printable ASCII with no space, ``<``,
    ``>`` or ``,`` — anything that would end the header's ``<…>``.
    """
    if (
        not url.startswith("https://")
        or not url.isascii()
        or not url.isprintable()
        or any(char.isspace() or char in "<>," for char in url)
    ):
        raise ValueError(f"a List-Unsubscribe URL is https:// with no space, <, > or ,: {url[:60]!r}")
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": _ONE_CLICK}


@dataclass(frozen=True, slots=True)
class MailUnsubscribe:
    """A notification mail's way out (notifications §3.6), for :func:`render_mail`.

    ``label`` is the footer line, in the recipient's language: why the mail came and what
    the link does ("You get this because e-mails about shared projects are on. Stop
    them"). ``page`` is the app's signed-out ``/unsubscribe?token=…`` page, which offers
    "Stop these e-mails" and "Stop all"; ``one_click`` the ``POST
    /notifications/unsubscribe/{token}`` URL for :func:`list_unsubscribe_headers`, or
    ``None`` where the app's base URL isn't https (a development server): RFC 8058 takes
    https only, so such a mail has the footer line and no headers. A :class:`ValueError`
    for a ``page`` that isn't http(s) or a ``one_click`` that isn't https.
    """

    label: str
    page: str
    one_click: str | None

    def __post_init__(self) -> None:
        _check_link(self.page, "an unsubscribe page")
        if self.one_click is not None:
            list_unsubscribe_headers(self.one_click)  # refused here, not at the send

    @property
    def headers(self) -> dict[str, str] | None:
        """:func:`list_unsubscribe_headers` for :attr:`one_click`, or ``None``."""
        return None if self.one_click is None else list_unsubscribe_headers(self.one_click)


def render_message(
    *,
    subject: str,
    lang: str,
    intro: str,
    body: str,
    cta: str,
    link: str,
    outro: str,
    unsubscribe: MailUnsubscribe | None = None,
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

    The plain text's link line is ``{cta}: {link}``, typeset for ``lang``: French takes a
    no-break space before the colon (``Choisir\u00a0: https://…``), every other language
    none.

    ``unsubscribe`` (a notification mail, :class:`MailUnsubscribe`) adds a last line after
    the footer, linking its ``page``: escaped and typeset like the action line —
    ``{label}: {page}`` in the text half.
    """
    _check_link(link, "a mail link")
    tag = _lang_attribute(lang)
    content = (
        f"<p>{escape(intro)}</p><p>{escape(body)}</p>"
        f'<p><a href="{escape(link, quote=True)}">{escape(cta)}</a></p>'
        f'<p style="color:#64748b;font-size:12px">{escape(outro)}</p>'
    )
    if unsubscribe is not None:
        content += (
            f'<p style="color:#64748b;font-size:12px">'
            f'<a href="{escape(unsubscribe.page, quote=True)}">{escape(unsubscribe.label)}</a></p>'
        )
    html = (
        f'<!doctype html><html lang="{escape(tag, quote=True)}">'
        f'<head><meta charset="utf-8"><title>{escape(subject)}</title></head>'
        f"<body>{content}</body></html>"
    )
    colon = _BEFORE_COLON.get(_language(tag).lower(), "") + ":"
    text = f"{intro}\n\n{body}\n\n{cta}{colon} {link}\n\n{outro}"
    if unsubscribe is not None:
        text += f"\n\n{unsubscribe.label}{colon} {unsubscribe.page}"
    return html, text


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One mail, ready to send: ``html`` may be ``None`` for a plain-text mail (kastlan).

    ``headers`` are extra mail headers — a notification mail's
    :func:`list_unsubscribe_headers` — which :class:`ResendClient` sends as Resend's
    ``headers`` object and an SMTP transport sets on its message. A :class:`ValueError` for
    a name that is no header name, or a line break in a value.

    ``idempotency_key`` is the send's own ``Idempotency-Key`` — a request header to the
    provider, not a mail header — for a mail a job may send again (notifications §9.4,
    :meth:`~eifi1_server_kit.notifications.MailMarker.idempotency_key`)::

        message = replace(message, idempotency_key=marker.idempotency_key(today))

    :class:`ResendClient` sends it on both tries; ``None`` (account mail) is a random key
    per send. :class:`ConsoleMailer` ignores it. A :class:`ValueError` unless it is 1–256
    characters of printable ASCII with no space: Resend refuses a longer one, and it is
    one header line.
    """

    subject: str
    text: str
    html: str | None = None
    headers: Mapping[str, str] | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name, value in (self.headers or {}).items():
            if not _HEADER_NAME.fullmatch(name) or any(char in value for char in "\r\n\x00"):
                raise ValueError(f"a mail header is a name and a one-line value: {name[:40]!r}")
        if self.idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(self.idempotency_key):
            raise ValueError(
                f"an idempotency key is 1–256 printable ASCII characters, no space: {self.idempotency_key[:60]!r}"
            )


@dataclass(frozen=True, slots=True)
class HtmlMailMessage(MailMessage):
    """A :class:`MailMessage` whose ``html`` is always there, as :func:`test_mail_message`
    returns it: an app whose own send takes ``html: str`` passes ``message.html`` as it is
    (keksdose N2, 0.15.1). Every :class:`MailMessage` rule holds; ``html`` is required."""

    html: str


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

    def render(
        self, *, link: str, lang: str, unsubscribe: MailUnsubscribe | None = None, **values: object
    ) -> MailMessage:
        """The mail with ``values`` filled in and the layout of :func:`render_message`;
        ``lang`` is the language this row is written in (its key in the table), so no
        template may use a ``{lang}`` or ``{unsubscribe}`` placeholder.

        ``str.format`` runs on the TEMPLATE only, so braces in a person's name are text,
        never a format field; the name is HTML-escaped by the layout. A literal brace in a
        template is doubled (``{{``).

        ``unsubscribe`` makes it a notification mail: the footer line, and
        :attr:`MailMessage.headers` from its ``one_click``. Its ``label`` is used as
        written, never formatted.
        """
        subject, intro, body, cta, outro = (
            part.format_map(values) for part in (self.subject, self.intro, self.body, self.cta, self.outro)
        )
        html, text = render_message(
            subject=subject,
            lang=lang,
            intro=intro,
            body=body,
            cta=cta,
            link=link,
            outro=outro,
            unsubscribe=unsubscribe,
        )
        headers = unsubscribe.headers if unsubscribe is not None else None
        return MailMessage(subject=subject, text=text, html=html, headers=headers)


def render_mail(
    locale: str | None,
    texts: Mapping[str, MailText],
    *,
    link: str,
    fallback: Sequence[str] = FALLBACK_LOCALES,
    unsubscribe: MailUnsubscribe | None = None,
    **values: object,
) -> MailMessage:
    """The recipient's mail from a table: :func:`pick_entry` for ``locale``, then
    :meth:`MailText.render` with the row's own language in ``<html lang>``::

        message = render_mail(user.locale, RESET_TEXTS, link=link, name=greeting, hours=1)

    A notification mail passes ``unsubscribe`` (:class:`MailUnsubscribe`, its label picked
    for the same ``locale``): the footer line and the ``List-Unsubscribe`` headers::

        message = render_mail(user.locale, SHARED_TEXTS, link=link, actor=name, unsubscribe=way_out)
        await mailer.send(user.email, message, kind="share_granted")
    """
    lang, text = pick_entry(locale, texts, fallback=fallback)
    return text.render(link=link, lang=lang, unsubscribe=unsubscribe, **values)


class AccountNoticeKind(enum.StrEnum):
    """The account mails that carry no one-time token, as the ``kind`` they are sent and
    logged under (KS S5): each TELLS an address something rather than asking it to prove
    anything, so :class:`~eifi1_server_kit.auth.OneTimeTokenKind` has no word for them.
    Not ``MailKind``, which is :mod:`~eifi1_server_kit.user_admin`'s word for the mail an
    admin sends again. None has a switch or an unsubscribe link: they are about the
    account itself.

    The user-admin contract requires the first two of every app with an email change and
    an account deletion; the third is Kurvenschmiede's, for its admin transfer of work
    (notifications §9.3)::

        await mailer.send(user.email, message, kind=AccountNoticeKind.ACCOUNT_DELETION)
    """

    #: To the OLD address when an account asks for a new one (user-admin §6.2): told,
    #: never handed the link. keksdose (``email_change_service.py:176``), kastlan
    #: (``account_service.py:126``) and Kurvenschmiede already send it under this word.
    EMAIL_CHANGE_NOTICE = "email_change_notice"
    #: The answer to the account's own deletion request (user-admin §6.4) — when it will
    #: be deleted, and how to undo it. keksdose's word (``account_deletion_service.py:279``);
    #: kastlan sent ``deletion`` and Kurvenschmiede ``deletion_request``.
    ACCOUNT_DELETION = "account_deletion"
    #: To the former owner when an admin hands their work on to another account
    #: (notifications §9.3), only while that account is ACTIVE: no switch, no unsubscribe
    #: link. Kurvenschmiede's (``admin_service.transfer``); the words stay its own.
    WORK_TRANSFERRED_NOTICE = "work_transferred_notice"
    #: To an author of reports made public by the round-0.38 migration (feedback §9.12):
    #: which reports others now see, what they never see, and how to keep one private.
    #: Once per person, through the app's once-marker (``feedback_published:<user_id>``).
    #: About the person's own data, so no switch and no unsubscribe link (kk, KS).
    FEEDBACK_PUBLISHED = "feedback_published"


# --- the test mail (admin-harmonization §9.2, 0.15) --------------------------------------

#: The ``kind`` the admin's test mail is sent and logged under, and its metric label
#: (keksdose ``record_email``; kastlan's kinds at ``email.py:65``).
MAIL_TEST_KIND = "mail_test"

#: ``POST /admin/mail/test`` (kastlan: ``/platform/mail/test``): 3 per 10 minutes per
#: account, beside the app's other limiters (§9.2.1, §9.2.6).
MAIL_TEST_LIMIT = Budget(3, 600)


class MailTestResult(BaseModel):
    """The test route's answer, ``{sent, mail_backend}`` (§9.2.1). ``sent`` is the app's
    own ``send``'s bool; a console transport answers ``True`` although nothing left the
    server, so the page reads ``mail_backend`` first (§9.2.4). The provider's error stays in
    the server log (§9.2.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sent: bool
    #: The app's transport as its admin page already reports it: ``console``, ``resend``,
    #: ``smtp``.
    mail_backend: str


@dataclass(frozen=True, slots=True)
class _TestMailText:
    subject: str
    intro: str
    body: str
    outro: str


#: The test mail's words (§9.2.3): the app, the transport and the time; never the
#: recipient. ``{app}``, ``{backend}`` and ``{time}`` are filled in.
_TEST_MAIL_TEXTS: Mapping[str, _TestMailText] = {
    "de-CH": _TestMailText(
        subject="Testmail von {app}",
        intro="Dies ist eine Testmail.",
        body="{app} hat sie am {time} über den Mailversand «{backend}» verschickt.",
        outro="Wenn sie bei Ihnen angekommen ist, funktioniert der Mailversand. Sie müssen nicht antworten.",
    ),
    "en": _TestMailText(
        subject="Test mail from {app}",
        intro="This is a test mail.",
        body="{app} sent it through the mail transport “{backend}” on {time}.",
        outro="If it reached you, the app's mail works. There is no need to reply.",
    ),
    "es": _TestMailText(
        subject="Correo de prueba de {app}",
        intro="Este es un correo de prueba.",
        body="{app} lo envió el {time} a través del transporte de correo «{backend}».",
        outro="Si le ha llegado, el envío de correo funciona. No hace falta responder.",
    ),
    "fr": _TestMailText(
        subject="E-mail de test de {app}",
        intro="Ceci est un e-mail de test.",
        body="{app} l’a envoyé le {time} par le transport d’e-mail «\u00a0{backend}\u00a0».",
        outro="S’il vous est parvenu, l’envoi d’e-mails fonctionne. Inutile d’y répondre.",
    ),
    "hu": _TestMailText(
        subject="Tesztlevél – {app}",
        intro="Ez egy tesztlevél.",
        body="A(z) {app} ekkor küldte el a(z) „{backend}” levélküldőn keresztül: {time}.",
        outro="Ha megérkezett Önhöz, a levélküldés működik. Nem kell válaszolnia.",
    ),
    "it": _TestMailText(
        subject="E-mail di prova da {app}",
        intro="Questa è un’e-mail di prova.",
        body="{app} l’ha inviata il {time} tramite il servizio di posta «{backend}».",
        outro="Se l’ha ricevuta, l’invio delle e-mail funziona. Non serve rispondere.",
    ),
    "zh": _TestMailText(
        subject="来自 {app} 的测试邮件",
        intro="这是一封测试邮件。",
        body="{app} 于 {time} 通过邮件通道“{backend}”发送了此邮件。",
        outro="如果您收到了这封邮件，说明邮件发送正常。无需回复。",
    ),
}


def test_mail_message(locale: str | None, *, app_name: str, backend: str, sent_at: datetime) -> HtmlMailMessage:
    """The admin's test mail (§9.2.3) in the admin's language, through :func:`pick_entry`'s
    fallback: subject "Test mail from {app}", a body naming the transport and the time
    (UTC, ``2026-10-19 14:05 UTC``). It names no recipient, so the route's log line and
    the mail never carry the address. No link: a whole escaped document as
    :func:`render_message` lays one out, the plain text beside it.

    The route awaits the app's own send and returns its bool — never an after-commit hook::

        message = test_mail_message(admin.locale, app_name="Kurvenschmiede", backend=backend, sent_at=now)
        sent = await mailer.send(admin.email, message, kind=MAIL_TEST_KIND)
        return MailTestResult(sent=sent, mail_backend=backend)

    ``sent_at`` must be aware; a naive one is a :class:`ValueError`. The answer is an
    :class:`HtmlMailMessage`: its ``html`` is a ``str``, never ``None``.
    """
    if sent_at.tzinfo is None or sent_at.utcoffset() is None:
        raise ValueError("sent_at must carry a time zone")
    lang, row = pick_entry(locale, _TEST_MAIL_TEXTS)
    values = {"app": app_name, "backend": backend, "time": sent_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")}
    subject, intro, body, outro = (part.format_map(values) for part in (row.subject, row.intro, row.body, row.outro))
    tag = _lang_attribute(lang)
    html = (
        f'<!doctype html><html lang="{escape(tag, quote=True)}">'
        f'<head><meta charset="utf-8"><title>{escape(subject)}</title></head>'
        f"<body><p>{escape(intro)}</p><p>{escape(body)}</p>"
        f'<p style="color:#64748b;font-size:12px">{escape(outro)}</p></body></html>'
    )
    return HtmlMailMessage(subject=subject, text=f"{intro}\n\n{body}\n\n{outro}", html=html)


# pytest collects a ``test_*`` function an app's test module imports; this one is not a test.
test_mail_message.__test__ = False  # type: ignore[attr-defined]


class Mailer(Protocol):
    """What sends a mail: :class:`ResendClient`, :class:`ConsoleMailer`, or a test's fake."""

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        """Send one mail; whether it went out. Never raises.

        ``kind`` is what the mail is FOR — a small enum fit for a log line or a metric
        label, which the recipient's address never is: a
        :class:`~eifi1_server_kit.auth.OneTimeTokenKind` (``verification``,
        ``password_reset``, ``invitation``, ``email_change``), an
        :class:`AccountNoticeKind` (``email_change_notice``, ``account_deletion``,
        ``work_transferred_notice``), a
        :class:`~eifi1_server_kit.billing.BillingNoticeKind`, or — for a notification mail —
        its notification type's id (``share_granted``; notifications §3.6). An app whose own
        ``send`` takes a narrower kind widens it to its registry's ids.
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
        if message.headers:
            headers = "".join(f"{name}: {value}\n" for name, value in message.headers.items())
            self._log.log(
                self._level,
                "[email:console] %s to=%s subject=%s\n%s\n%s",
                kind,
                to,
                message.subject,
                headers,
                message.text,
            )
        else:
            self._log.log(
                self._level, "[email:console] %s to=%s subject=%s\n%s", kind, to, message.subject, message.text
            )
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
    a first attempt that did reach Resend before the timeout is not sent twice: the
    message's :attr:`~MailMessage.idempotency_key` where it has one (a job's notification
    mail, notifications §9.4), else a random ``{kind}-{uuid}``. No queue, no delivery
    tracking: a handful of mails, each awaited by somebody at the app.

    **Resend's idempotency** (its docs, 2026-10-14): a key is kept 24 hours; the same key
    with the same body answers the first request's answer, and nothing is sent again; a
    ``409`` is the same key while its first request is still in progress
    (``concurrent_idempotent_requests``) or with another body
    (``invalid_idempotent_request``). A ``409`` is "not sent" here and not retried: the
    request that holds the key has the mail, and a job's next run tries again under the
    next day's key.

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
        if message.headers:
            # Resend's own field for mail headers, beside the request's HTTP headers.
            payload["headers"] = dict(message.headers)
        return payload

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        """Send ``message`` to ``to``; whether Resend accepted it. Never raises."""
        key = message.idempotency_key if message.idempotency_key is not None else f"{kind}-{uuid.uuid4()}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Idempotency-Key": key}
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
