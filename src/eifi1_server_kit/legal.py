"""A change to the terms or the privacy policy: announced 30 days ahead, by email and in the app.

``docs/legal-harmonization.md`` decision 10 in ``Eifi1/ui-kit``. The terms' ``changes``
section promises: "We announce a material change at least 30 days before it takes effect,
by email and in the app, together with the new text. If you do not agree, you can object
or delete your account before that date, and you can export your data until then."
keksdose's legal audit against the app (Marcel, 2026-11-09) found that no app could mail
every account. Marcel kept both channels; the kits provide the means: ui-kit draws the
in-app notice, and this module serves the list it reads and sends the mail.

* **The change is the app's record** (:class:`LegalChange`): the page, the day it is
  announced, the day it takes effect, and where the new text is. A notice shorter than
  :data:`NOTICE_DAYS` is refused when the record is made, so a mistyped date fails the
  app's own tests, never a person's notice.
* **The in-app list** is ``GET /api/v1/legal/changes`` (:data:`LEGAL_CHANGES_PATH`): a list
  of :class:`LegalChangeAnnouncement`, ``{page, effective, href}``, the shape ui-kit's
  notice reads, from :func:`current_announcements`. No router: the route is three lines
  over the app's own tuple of changes, as the admin's test mail route is over its
  transport (``mail``).
* **The mail** (:func:`legal_change_message`): the kit's words in the seven languages of
  the test mail, naming the page and the date, linking the new text, and saying that one
  may object (by replying) or delete the account before that date and export until then.
  Sent under :attr:`~eifi1_server_kit.mail.AccountNoticeKind.LEGAL_CHANGE`. It is a notice
  the terms owe every account, not a subscription: no switch and no ``List-Unsubscribe``,
  as for every account mail (``mail``).
* **Every account, once** (:func:`send_legal_change_mails`): the app hands over the active
  accounts and the ids it already mailed for this change; the kit skips those, sends the
  rest a few a second, records each send through the app's callback as it goes, and
  answers sent, skipped or failed per account. A send is refused once the 30 days can no
  longer be kept (:class:`LegalNoticeError`, 409): the app then moves the date.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from collections.abc import Awaitable, Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Literal, get_args
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from eifi1_server_kit.mail import (
    AccountNoticeKind,
    HtmlMailMessage,
    Mailer,
    MailText,
    pick_entry,
    render_message,
)

__all__ = [
    "LEGAL_CHANGES_PATH",
    "LEGAL_CHANGE_BATCH",
    "LEGAL_CHANGE_PAUSE",
    "LEGAL_CHANGE_STOP_AFTER",
    "NOTICE_DAYS",
    "LegalChange",
    "LegalChangeAnnouncement",
    "LegalChangeOutcome",
    "LegalChangePage",
    "LegalChangeRecipient",
    "LegalChangeResult",
    "LegalChangeRun",
    "LegalNoticeError",
    "current_announcements",
    "legal_change_message",
    "send_legal_change_mails",
]

logger = logging.getLogger("eifi1_server_kit.legal")

#: The terms' promise (decision 10): a material change is announced at least this many
#: days before it takes effect, by email and in the app.
NOTICE_DAYS = 30
#: The route, under the app's API prefix (``/api/v1/legal/changes``).
LEGAL_CHANGES_PATH = "/legal/changes"
#: The pages a change is announced for. The imprint names the operator and owes no notice.
LegalChangePage = Literal["terms", "privacy"]
#: How many mails one :func:`send_legal_change_mails` call sends at most: 200 at
#: :data:`LEGAL_CHANGE_PAUSE` take under three minutes, inside Cloud Run's default request
#: timeout (five), so a jobs route or an admin action finishes and is called again for the
#: rest. Skipped accounts do not count.
LEGAL_CHANGE_BATCH = 200
#: Seconds between two mails: two a second. Resend allows 10 requests a second per team,
#: shared with every sign-up and reset mail the app sends meanwhile (its docs, 2026-11-09).
LEGAL_CHANGE_PAUSE = 0.5
#: Failed sends in a row after which a run stops: the provider is down, the key is wrong
#: or the month's quota is spent, and every further try would fail the same way.
LEGAL_CHANGE_STOP_AFTER = 5

_PAGES: tuple[str, ...] = get_args(LegalChangePage)
#: A path in the app (``/terms``), never ``//`` or a backslash after the first slash.
_APP_PATH = re.compile(r"^/(?![/\\])")
#: What a browser strips from a URL or reads as ``/``, the other control characters, and
#: any space: none belongs in a link the server writes.
_UNSAFE_IN_HREF = re.compile(r"[\x00-\x1f\x7f\\\s]")
#: A user id given as a string (a UUID): it goes into the send's ``Idempotency-Key``, so no
#: colon, which separates the key's parts, and no space (``notifications.MailMarker``'s rule).
_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: The app's host as the key carries it: ASCII (an IDN host in its ``xn--`` form).
_HOST = re.compile(r"^[a-z0-9.-]{1,100}$")


class LegalNoticeError(ValueError):
    """The notice the terms promise cannot be kept (→ 409): a :class:`LegalChange` that
    takes effect fewer than :data:`NOTICE_DAYS` days after it is announced, or a mail run
    on a day before the announcement or later than :attr:`LegalChange.mail_by`.

    A :class:`ValueError`, so a record made at import fails the app's start and its tests;
    answered at 409 through :func:`~eifi1_server_kit.errors.install_contract_error_handlers`
    when an admin action runs the send.
    """

    status_code: int = 409


def _day(value: date, what: str) -> date:
    """``value``, refused when it is a ``datetime``: a notice counts whole days."""
    if isinstance(value, datetime):
        raise TypeError(f"{what} is a date, not a datetime: pass .date() ({value!r})")
    return value


def _check_href(href: str) -> str:
    """A path in the app (``/terms``) or an http(s) URL, in one piece: never a scheme a
    link must not have (``javascript:``), ``//`` (another host), a backslash, a space or a
    control character."""
    if (_APP_PATH.match(href) or href.startswith(("https://", "http://"))) and not _UNSAFE_IN_HREF.search(href):
        return href
    raise ValueError(f"a legal change's href is a path in the app (/…) or an http(s) URL: {href[:60]!r}")


class LegalChangeAnnouncement(BaseModel):
    """One entry of ``GET /api/v1/legal/changes``: ``{page, effective, href}``, the shape
    ui-kit's in-app notice reads (decision 10). Built by :attr:`LegalChange.announcement`
    and :func:`current_announcements`, so the notice period has been checked before an
    app can serve one; the wire carries no announcement day.

    ``effective`` is ISO (``"2026-12-15"``) on the wire. ``href`` is where the new text can
    be read: a path in the app (``/terms?version=2026-12-15``) or an http(s) URL; ``None``
    when the page itself (``/terms``, ``/privacy``) already shows it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The page that changes: ``terms`` or ``privacy``.
    page: LegalChangePage
    #: The day the new text takes effect.
    effective: date
    #: Where the new text can be read; ``None`` for the page itself.
    href: str | None = None

    @field_validator("href")
    @classmethod
    def _href(cls, value: str | None) -> str | None:
        return None if value is None else _check_href(value)


@dataclass(frozen=True, slots=True)
class LegalChange:
    """A change to the terms or the privacy policy, as the app records it: in its code
    beside the new text, one per change::

        LEGAL_CHANGES = (
            LegalChange("terms", announced=date(2026, 11, 10), effective=date(2026, 12, 15)),
        )

    ``announced`` is the day the release carrying the new text ships: the in-app notice
    shows from that day (:func:`current_announcements`) and the mail goes from then until
    :attr:`mail_by`. ``effective`` is the day the new text applies. ``href`` as on
    :class:`LegalChangeAnnouncement`.

    A :class:`LegalNoticeError` when ``effective`` is fewer than :data:`NOTICE_DAYS` days
    after ``announced``: a record with too short a notice cannot be made, so the app's own
    tests fail before a release does. Give a few days' margin (35): the mail must go out by
    :attr:`mail_by`, and a run that fails on a provider's outage wants a day to be retried.
    A :class:`ValueError` for another page or a bad ``href``; a :class:`TypeError` for a
    ``datetime``.
    """

    #: The page that changes: ``terms`` or ``privacy``.
    page: LegalChangePage
    #: The day the release carrying the new text ships; the notice runs from it.
    announced: date
    #: The day the new text applies: at least :data:`NOTICE_DAYS` days after ``announced``.
    effective: date
    #: Where the new text can be read; ``None`` for the page itself.
    href: str | None = None

    def __post_init__(self) -> None:
        if self.page not in _PAGES:
            raise ValueError(f"a legal change is to the terms or the privacy policy: {self.page!r}")
        notice = (_day(self.effective, "effective") - _day(self.announced, "announced")).days
        if notice < NOTICE_DAYS:
            name = "terms" if self.page == "terms" else "privacy policy"
            raise LegalNoticeError(
                f"a change to the {name} takes effect at least {NOTICE_DAYS} days after it is announced: "
                f"{self.announced.isoformat()} → {self.effective.isoformat()} is {notice} days"
            )
        if self.href is not None:
            _check_href(self.href)

    @property
    def key(self) -> str:
        """``<page>:<effective>`` (``terms:2026-12-15``): the change's name in the app's
        record of who was mailed, and in each send's ``Idempotency-Key``. It fits
        :data:`~eifi1_server_kit.notifications.MAIL_EVENT_PATTERN`."""
        return f"{self.page}:{self.effective.isoformat()}"

    @property
    def mail_by(self) -> date:
        """The last day a mail still gives :data:`NOTICE_DAYS` days' notice: ``effective``
        less 30 days. :func:`send_legal_change_mails` refuses after it."""
        return self.effective - timedelta(days=NOTICE_DAYS)

    @property
    def announcement(self) -> LegalChangeAnnouncement:
        """This change on the wire: ``{page, effective, href}``."""
        return LegalChangeAnnouncement(page=self.page, effective=self.effective, href=self.href)


def current_announcements(changes: Iterable[LegalChange], *, today: date) -> list[LegalChangeAnnouncement]:
    """What ``GET /api/v1/legal/changes`` answers: every change announced by ``today`` that
    is not yet past its ``effective`` day (that day included), soonest first::

        @router.get(LEGAL_CHANGES_PATH, response_model=list[LegalChangeAnnouncement])
        def legal_changes() -> list[LegalChangeAnnouncement]:
            return current_announcements(LEGAL_CHANGES, today=local_today())

    ``today`` is the app's own day, in its zone (Europe/Zurich), as its jobs count days;
    a ``datetime`` is a :class:`TypeError`. An empty list when nothing is announced. The
    route is public: it names pages and dates, nothing of a person.
    """
    day = _day(today, "today")
    current = [change for change in changes if change.announced <= day <= change.effective]
    current.sort(key=lambda change: (change.effective, _PAGES.index(change.page)))
    return [change.announcement for change in current]


# --- the mail ----------------------------------------------------------------------------

#: The months by language, for the date in the mail's own words.
_MONTHS: Mapping[str, Sequence[str]] = {
    "de": [
        "Januar",
        "Februar",
        "März",
        "April",
        "Mai",
        "Juni",
        "Juli",
        "August",
        "September",
        "Oktober",
        "November",
        "Dezember",
    ],
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "es": [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ],
    "fr": [
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ],
    "hu": [
        "január",
        "február",
        "március",
        "április",
        "május",
        "június",
        "július",
        "augusztus",
        "szeptember",
        "október",
        "november",
        "december",
    ],
    "it": [
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre",
    ],
}


def _long_date(lang: str, day: date) -> str:
    """``day`` written out as the legal pages' as-of line writes it in ``lang`` (a key of
    the texts below): "15 December 2026", "15. Dezember 2026", "1er décembre 2026",
    "2026. december 15.", "2026年12月15日"."""
    language = lang.split("-", 1)[0].lower()
    if language == "zh":
        return f"{day.year}年{day.month}月{day.day}日"
    month = _MONTHS[language][day.month - 1]
    if language == "de":
        return f"{day.day}. {month} {day.year}"
    if language == "es":
        return f"{day.day} de {month} de {day.year}"
    if language == "hu":
        return f"{day.year}. {month} {day.day}."
    first = {"fr": "1er", "it": "1º"}.get(language) if day.day == 1 else None
    return f"{first or day.day} {month} {day.year}"


#: The terms' change mail (decision 10), ``{app}`` and ``{date}`` filled in. The sentences
#: after the first follow the terms' ``changes`` section in ui-kit's catalogue for each
#: language; "reply to this email" is the way to object, which the transport's
#: ``Reply-To`` (``support@``) brings to a person. Hungarian puts the date after a colon,
#: where it takes no case ending.
_TERMS_TEXTS: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="{app}: Unsere Nutzungsbedingungen ändern sich am {date}",
        intro="Wir passen die Nutzungsbedingungen von {app} an. Die neuen Bedingungen gelten ab dem {date}.",
        body=(
            "Wenn Sie nicht einverstanden sind, können Sie vor diesem Datum widersprechen oder Ihr Konto löschen "
            "und bis dahin Ihre Daten exportieren. Um zu widersprechen, antworten Sie einfach auf diese E-Mail. "
            "Widersprechen Sie, können wir Ihr Konto auf dieses Datum schliessen. Nutzen Sie {app} nach diesem "
            "Datum weiter, gelten die neuen Bedingungen."
        ),
        cta="Neue Nutzungsbedingungen lesen",
        outro="Sie erhalten diese Mitteilung, weil Sie ein Konto bei {app} haben.",
    ),
    "en": MailText(
        subject="{app}: our terms of service change on {date}",
        intro="We are updating the terms of service of {app}. The new terms apply from {date}.",
        body=(
            "If you do not agree, you can object or delete your account before that date, and you can export "
            "your data until then. To object, simply reply to this email. If you object, we may end your account "
            "on that date. If you keep using {app} after that date, the new terms apply."
        ),
        cta="Read the new terms",
        outro="You are receiving this notice because you have an account with {app}.",
    ),
    "es": MailText(
        subject="{app}: nuestras condiciones de uso cambian el {date}",
        intro=(
            "Estamos actualizando las condiciones de uso de {app}. Las nuevas condiciones se aplican a partir "
            "del {date}."
        ),
        body=(
            "Si no está de acuerdo, puede oponerse o eliminar su cuenta antes de esa fecha, y puede exportar sus "
            "datos hasta entonces. Para oponerse, basta con responder a este correo. Si se opone, podemos cerrar "
            "su cuenta en esa fecha. Si sigue usando {app} después de esa fecha, se aplican las nuevas condiciones."
        ),
        cta="Leer las nuevas condiciones",
        outro="Recibe este aviso porque tiene una cuenta en {app}.",
    ),
    "fr": MailText(
        subject="Les conditions d’utilisation de {app} changent le {date}",
        intro=(
            "Nous mettons à jour les conditions d’utilisation de {app}. Les nouvelles conditions s’appliquent "
            "à partir du {date}."
        ),
        body=(
            "Si vous n’êtes pas d’accord, vous pouvez vous y opposer ou supprimer votre compte avant cette date, "
            "et exporter vos données jusque-là. Pour vous y opposer, répondez simplement à cet e-mail. Si vous "
            "vous y opposez, nous pouvons fermer votre compte à cette date. Si vous continuez à utiliser {app} "
            "après cette date, les nouvelles conditions s’appliquent."
        ),
        cta="Lire les nouvelles conditions",
        outro="Vous recevez ce message parce que vous avez un compte sur {app}.",
    ),
    "hu": MailText(
        subject="{app}: új felhasználási feltételek – hatálybalépés: {date}",
        intro="Módosítjuk a(z) {app} felhasználási feltételeit. Az új feltételek hatálybalépésének napja: {date}",
        body=(
            "Ha nem ért egyet, ezen időpont előtt tiltakozhat, vagy törölheti a fiókját, és addig exportálhatja "
            "az adatait. A tiltakozáshoz egyszerűen válaszoljon erre az e-mailre. Ha tiltakozik, a hatálybalépés "
            "napjával megszüntethetjük a fiókját. Ha ezen időpont után is használja a(z) {app} szolgáltatást, az "
            "új feltételek érvényesek."
        ),
        cta="Az új feltételek elolvasása",
        outro="Azért kapja ezt az értesítést, mert van fiókja a(z) {app} szolgáltatásban.",
    ),
    "it": MailText(
        subject="{app}: le condizioni d’uso cambiano il {date}",
        intro="Stiamo aggiornando le condizioni d’uso di {app}. Le nuove condizioni si applicano a partire dal {date}.",
        body=(
            "Se non è d’accordo, può opporsi o eliminare il Suo account prima di tale data, e può esportare i Suoi "
            "dati fino ad allora. Per opporsi, basta rispondere a questa e-mail. Se si oppone, possiamo chiudere "
            "il Suo account in tale data. Se continua a usare {app} dopo tale data, si applicano le nuove condizioni."
        ),
        cta="Leggi le nuove condizioni",
        outro="Riceve questo avviso perché ha un account su {app}.",
    ),
    "zh": MailText(
        subject="{app}：使用条款将于 {date} 变更",
        intro="我们正在更新 {app} 的使用条款。新条款自 {date} 起生效。",
        body=(
            "如您不同意，可在该日期前提出异议或删除您的账户，并可在此之前导出您的数据。如需提出异议，直接回复本邮件即可。"
            "如您提出异议，我们可在该日期关闭您的账户。如您在该日期后继续使用 {app}，则适用新条款。"
        ),
        cta="阅读新条款",
        outro="您收到此通知，是因为您在 {app} 拥有账户。",
    ),
}

#: The privacy policy's change mail: the same notice, without the terms' sentences on an
#: account ended by an objection and on continued use, which the policy has no clause for.
_PRIVACY_TEXTS: Mapping[str, MailText] = {
    "de-CH": MailText(
        subject="{app}: Unsere Datenschutzerklärung ändert sich am {date}",
        intro="Wir passen die Datenschutzerklärung von {app} an. Die neue Fassung gilt ab dem {date}.",
        body=(
            "Wenn Sie nicht einverstanden sind, können Sie vor diesem Datum widersprechen oder Ihr Konto löschen "
            "und bis dahin Ihre Daten exportieren. Um zu widersprechen, antworten Sie einfach auf diese E-Mail. "
            "Ab diesem Datum beschreibt die neue Fassung, wie {app} mit Ihren Daten umgeht."
        ),
        cta="Neue Datenschutzerklärung lesen",
        outro="Sie erhalten diese Mitteilung, weil Sie ein Konto bei {app} haben.",
    ),
    "en": MailText(
        subject="{app}: our privacy policy changes on {date}",
        intro="We are updating the privacy policy of {app}. The new version applies from {date}.",
        body=(
            "If you do not agree, you can object or delete your account before that date, and you can export "
            "your data until then. To object, simply reply to this email. From that date on, the new version "
            "describes how {app} handles your data."
        ),
        cta="Read the new privacy policy",
        outro="You are receiving this notice because you have an account with {app}.",
    ),
    "es": MailText(
        subject="{app}: nuestra política de privacidad cambia el {date}",
        intro=(
            "Estamos actualizando la política de privacidad de {app}. La nueva versión se aplica a partir del {date}."
        ),
        body=(
            "Si no está de acuerdo, puede oponerse o eliminar su cuenta antes de esa fecha, y puede exportar sus "
            "datos hasta entonces. Para oponerse, basta con responder a este correo. A partir de esa fecha, la "
            "nueva versión describe cómo trata {app} sus datos."
        ),
        cta="Leer la nueva política de privacidad",
        outro="Recibe este aviso porque tiene una cuenta en {app}.",
    ),
    "fr": MailText(
        subject="La politique de confidentialité de {app} change le {date}",
        intro=(
            "Nous mettons à jour la politique de confidentialité de {app}. La nouvelle version s’applique à "
            "partir du {date}."
        ),
        body=(
            "Si vous n’êtes pas d’accord, vous pouvez vous y opposer ou supprimer votre compte avant cette date, "
            "et exporter vos données jusque-là. Pour vous y opposer, répondez simplement à cet e-mail. À partir "
            "de cette date, la nouvelle version décrit la manière dont {app} traite vos données."
        ),
        cta="Lire la nouvelle politique de confidentialité",
        outro="Vous recevez ce message parce que vous avez un compte sur {app}.",
    ),
    "hu": MailText(
        subject="{app}: új adatvédelmi tájékoztató – hatálybalépés: {date}",
        intro="Módosítjuk a(z) {app} adatvédelmi tájékoztatóját. Az új változat hatálybalépésének napja: {date}",
        body=(
            "Ha nem ért egyet, ezen időpont előtt tiltakozhat, vagy törölheti a fiókját, és addig exportálhatja "
            "az adatait. A tiltakozáshoz egyszerűen válaszoljon erre az e-mailre. Ettől az időponttól az új "
            "változat írja le, hogyan kezeli a(z) {app} az Ön adatait."
        ),
        cta="Az új adatvédelmi tájékoztató elolvasása",
        outro="Azért kapja ezt az értesítést, mert van fiókja a(z) {app} szolgáltatásban.",
    ),
    "it": MailText(
        subject="{app}: l’informativa sulla privacy cambia il {date}",
        intro=(
            "Stiamo aggiornando l’informativa sulla privacy di {app}. La nuova versione si applica a partire "
            "dal {date}."
        ),
        body=(
            "Se non è d’accordo, può opporsi o eliminare il Suo account prima di tale data, e può esportare i Suoi "
            "dati fino ad allora. Per opporsi, basta rispondere a questa e-mail. A partire da tale data, la nuova "
            "versione descrive come {app} tratta i Suoi dati."
        ),
        cta="Leggi la nuova informativa sulla privacy",
        outro="Riceve questo avviso perché ha un account su {app}.",
    ),
    "zh": MailText(
        subject="{app}：隐私政策将于 {date} 变更",
        intro="我们正在更新 {app} 的隐私政策。新版本自 {date} 起适用。",
        body=(
            "如您不同意，可在该日期前提出异议或删除您的账户，并可在此之前导出您的数据。如需提出异议，直接回复本邮件即可。"
            "自该日期起，新版本将说明 {app} 如何处理您的数据。"
        ),
        cta="阅读新版隐私政策",
        outro="您收到此通知，是因为您在 {app} 拥有账户。",
    ),
}

_TEXTS: Mapping[str, Mapping[str, MailText]] = {"terms": _TERMS_TEXTS, "privacy": _PRIVACY_TEXTS}


def _base(app_base_url: str) -> str:
    if not app_base_url.startswith(("https://", "http://")) or _UNSAFE_IN_HREF.search(app_base_url):
        raise ValueError(f"the app's base URL is http(s): {app_base_url[:60]!r}")
    return app_base_url.rstrip("/")


def _link(change: LegalChange, app_base_url: str) -> str:
    """Where the mail's button goes: ``href`` when it is a URL, the app's base URL and
    ``href`` when it is a path, else the page itself (``/terms``, ``/privacy``; §3.1)."""
    base = _base(app_base_url)
    if change.href is None:
        return f"{base}/{change.page}"
    return change.href if change.href.startswith(("https://", "http://")) else f"{base}{change.href}"


def legal_change_message(
    locale: str | None, change: LegalChange, *, app_name: str, app_base_url: str
) -> HtmlMailMessage:
    """The mail announcing ``change`` (decision 10) in the recipient's language, through
    :func:`~eifi1_server_kit.mail.pick_entry`'s fallback: the page and the date it takes
    effect, written out in that language ("15 December 2026", "15. Dezember 2026"), that
    one may object (by replying) or delete the account before then and export until then,
    and a button to the new text (``href``, or ``app_base_url`` and ``/terms`` or
    ``/privacy``). Laid out by :func:`~eifi1_server_kit.mail.render_message`, so the app's
    name is escaped. Seven languages, as the admin's test mail: ``de-CH``, ``en``, ``es``,
    ``fr``, ``hu``, ``it``, ``zh``.

    No ``List-Unsubscribe`` and no unsubscribe line: a notice the terms owe the account,
    not a mail anyone subscribed to. :func:`send_legal_change_mails` adds the send's
    ``idempotency_key``. A :class:`ValueError` unless ``app_base_url`` is http(s).
    """
    lang, row = pick_entry(locale, _TEXTS[change.page])
    values = {"app": app_name, "date": _long_date(lang, change.effective)}
    subject, intro, body, cta, outro = (
        part.format_map(values) for part in (row.subject, row.intro, row.body, row.cta, row.outro)
    )
    html, text = render_message(
        subject=subject, lang=lang, intro=intro, body=body, cta=cta, link=_link(change, app_base_url), outro=outro
    )
    return HtmlMailMessage(subject=subject, text=text, html=html)


# --- every account, once -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegalChangeRecipient[UserIdT: int | str]:
    """One account to tell: its id (what the app records), its address and its language.
    The app chooses them (:func:`send_legal_change_mails`). The address is left out of the
    ``repr``, so a log line that prints a recipient never names it. A :class:`ValueError`
    for a string ``user_id`` with a colon, a space or nothing in it (a number or a UUID is
    right): it is part of the send's ``Idempotency-Key``."""

    #: The account's id, as the app records it: a number or a UUID.
    user_id: UserIdT
    #: The account's address; never in the ``repr``.
    email: str = field(repr=False)
    #: The account's language (``de-CH``, ``fr``); ``None`` reads German, as every mail.
    locale: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.user_id, str) and not _USER_ID.fullmatch(self.user_id):
            raise ValueError(f"a user id is a number or a key with no colon or space: {self.user_id[:80]!r}")


class LegalChangeOutcome(enum.StrEnum):
    """What a run did for one account."""

    #: The transport took the mail; the app's ``record_sent`` was called.
    SENT = "sent"
    #: Mailed for this change before (in ``already_sent``), or listed twice: nothing sent.
    SKIPPED = "skipped"
    #: The transport did not take it; nothing recorded, so the next run tries again.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LegalChangeResult[UserIdT: int | str]:
    """One account's outcome, by id only: what the app may log or keep."""

    #: The account's id.
    user_id: UserIdT
    #: Sent, skipped or failed.
    outcome: LegalChangeOutcome


@dataclass(frozen=True, slots=True)
class LegalChangeRun[UserIdT: int | str]:
    """What one :func:`send_legal_change_mails` call did, an account per result in the
    order they came. ``complete`` is ``False`` when it stopped with accounts left to mail
    (the batch's ``limit``, or ``stop_after`` failures in a row): call it again, and those
    already sent are skipped. Failed ones are tried again by every later run."""

    #: One per account read, in the order they came.
    results: tuple[LegalChangeResult[UserIdT], ...]
    #: ``False`` when the run stopped with accounts left to mail: call it again.
    complete: bool

    def _count(self, outcome: LegalChangeOutcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)

    @property
    def sent(self) -> int:
        """How many were sent."""
        return self._count(LegalChangeOutcome.SENT)

    @property
    def skipped(self) -> int:
        """How many were skipped."""
        return self._count(LegalChangeOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        """How many failed."""
        return self._count(LegalChangeOutcome.FAILED)


def _host(app_base_url: str) -> str:
    host = urlsplit(_base(app_base_url)).hostname or ""
    if not _HOST.fullmatch(host):
        raise ValueError(f"the app's base URL has an ASCII host: {app_base_url[:60]!r}")
    return host


async def send_legal_change_mails[UserIdT: int | str](
    change: LegalChange,
    recipients: Iterable[LegalChangeRecipient[UserIdT]],
    mailer: Mailer,
    *,
    app_name: str,
    app_base_url: str,
    today: date,
    already_sent: Container[UserIdT],
    record_sent: Callable[[UserIdT], Awaitable[object]],
    limit: int | None = LEGAL_CHANGE_BATCH,
    pause: float = LEGAL_CHANGE_PAUSE,
    stop_after: int = LEGAL_CHANGE_STOP_AFTER,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> LegalChangeRun[UserIdT]:
    """Mail ``change`` to every account in ``recipients`` not mailed for it before
    (decision 10), :func:`legal_change_message` in each one's language, under
    :attr:`~eifi1_server_kit.mail.AccountNoticeKind.LEGAL_CHANGE`::

        sent = await legal_change_sent_ids(session, change.key)  # the app's record
        run = await send_legal_change_mails(
            change,
            [LegalChangeRecipient(u.id, u.email, u.locale) for u in active_accounts(session)],
            mailer,
            app_name="Kurvenschmiede",
            app_base_url=settings.app_base_url,
            today=local_today(),
            already_sent=sent,
            record_sent=lambda user_id: record_legal_change_sent(session, change.key, user_id),
        )

    **The app picks the recipients**: active accounts with an address, never a deactivated,
    erased or demo one (as feedback §9.11 drops them). **The app keeps the record**: one row
    per (change, account), the change by :attr:`LegalChange.key`, written by
    ``record_sent`` (``INSERT … ON CONFLICT DO NOTHING``, then commit) right after each mail
    the transport took and before the next is sent, so a run that dies half-way loses no
    record. Keep the rows: they show the notice went out. ``already_sent`` holds the ids it
    already has for this change (a set, or anything with ``in``); those, and an id listed
    twice, are skipped, so a rerun never mails anyone twice. Each send also carries the
    ``Idempotency-Key`` ``legal_change:<host>:<page>:<effective>:<user_id>``, so even a
    rerun within Resend's 24 hours whose record was lost sends nothing new; the app's host
    keeps two apps on one Resend team apart.

    **Throttled for a transactional provider**: ``pause`` seconds between two mails
    (:data:`LEGAL_CHANGE_PAUSE`, two a second), at most ``limit`` mails per call
    (:data:`LEGAL_CHANGE_BATCH`; ``None`` for no limit), and a stop after ``stop_after``
    failures in a row (:data:`LEGAL_CHANGE_STOP_AFTER`). Either stop leaves
    :attr:`LegalChangeRun.complete` ``False``: call it again. Recipients are read one at a
    time, and none past the stop.

    **The notice holds**: a :class:`LegalNoticeError` (409) before anything is read or sent
    when ``today`` is before ``change.announced`` or after :attr:`LegalChange.mail_by` — a
    mail then would give fewer than :data:`NOTICE_DAYS` days. Accounts still unmailed then
    need a new change with a later ``effective``. The mail asks for an objection by reply,
    so the transport's ``Reply-To`` must reach a person (``ResendClient``'s ``reply_to``,
    :func:`~eifi1_server_kit.mail.support_address`).

    One log line at the end: the change's key and the three counts, never an address. A
    :class:`ValueError` for a ``limit`` or ``stop_after`` below 1, a negative ``pause``, or
    an ``app_base_url`` that is not http(s) with an ASCII host; a :class:`TypeError` for a
    ``datetime``. What ``record_sent`` raises is raised.
    """
    day = _day(today, "today")
    if day < change.announced:
        raise LegalNoticeError(
            f"the change {change.key} is announced on {change.announced.isoformat()}: mail it from that day on"
        )
    if day > change.mail_by:
        raise LegalNoticeError(
            f"the change {change.key} needed its mail by {change.mail_by.isoformat()} for {NOTICE_DAYS} days' "
            f"notice; announce it again with a later effective date"
        )
    if limit is not None and limit < 1:
        raise ValueError(f"limit is at least 1, or None: {limit}")
    if stop_after < 1:
        raise ValueError(f"stop_after is at least 1: {stop_after}")
    if pause < 0:
        raise ValueError(f"pause is 0 or more: {pause}")
    prefix = f"{AccountNoticeKind.LEGAL_CHANGE}:{_host(app_base_url)}:{change.key}"

    messages: dict[str | None, HtmlMailMessage] = {}
    seen: set[UserIdT] = set()
    results: list[LegalChangeResult[UserIdT]] = []
    attempts = failures_in_a_row = 0
    complete = True
    for recipient in recipients:
        user_id = recipient.user_id
        if user_id in seen or user_id in already_sent:
            seen.add(user_id)
            results.append(LegalChangeResult(user_id, LegalChangeOutcome.SKIPPED))
            continue
        if (limit is not None and attempts >= limit) or failures_in_a_row >= stop_after:
            complete = False
            break
        seen.add(user_id)
        if attempts and pause:
            await sleep(pause)
        attempts += 1
        message = messages.get(recipient.locale)
        if message is None:
            message = legal_change_message(recipient.locale, change, app_name=app_name, app_base_url=app_base_url)
            messages[recipient.locale] = message
        keyed = replace(message, idempotency_key=f"{prefix}:{user_id}")
        if await mailer.send(recipient.email, keyed, kind=AccountNoticeKind.LEGAL_CHANGE):
            await record_sent(user_id)
            failures_in_a_row = 0
            results.append(LegalChangeResult(user_id, LegalChangeOutcome.SENT))
        else:
            failures_in_a_row += 1
            results.append(LegalChangeResult(user_id, LegalChangeOutcome.FAILED))
    run = LegalChangeRun(tuple(results), complete)
    logger.log(
        logging.INFO if complete and not run.failed else logging.WARNING,
        "legal change %s: %d sent, %d skipped, %d failed%s",
        change.key,
        run.sent,
        run.skipped,
        run.failed,
        "" if complete else "; stopped with accounts left, run it again",
    )
    return run
