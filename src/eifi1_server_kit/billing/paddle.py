"""Paddle Billing's API: the checkout, the customer portal, a cancellation and undoing one,
a plan switch and its preview, and the shared customer.

``docs/billing-harmonization.md`` §14.2 in ``Eifi1/ui-kit``, lifted from kastlan's
``adapters/billing/paddle_client.py`` (c0b1a24). :class:`PaddleClient` is the kit's one
:class:`~eifi1_server_kit.billing.BillingProviderClient`; build it through
:func:`~eifi1_server_kit.billing.billing_provider_client`, which reads the settings.

**httpx is the ``billing`` extra** (``eifi1-server-kit[billing]``), imported lazily as
:class:`~eifi1_server_kit.mail.ResendClient` does, so ``import eifi1_server_kit.billing``
never needs it; only building a client does.

**The key's minimum permissions** (Paddle's 2025 keys, Paddle > Developer tools >
Authentication): transaction write (the checkout), customer-portal-session write (the
portal), subscription write (a cancellation, undoing one, a plan switch and its preview)
and, since 0.12, customer read and write (the shared customer, §15.2.11). The key's prefix says its
environment, ``pdl_sdbx_apikey_`` or ``pdl_live_apikey_`` (:func:`paddle_environment_of`);
a sandbox key works only against the sandbox's API and a live key only against live's.

**Every call** sends ``Authorization: Bearer <key>`` and ``Paddle-Version: 1``, through
the app's one ``httpx.AsyncClient`` when it passes one (else a client per call), and is
tried ONCE: creating a transaction is not idempotent, so a retry could open two. A failure
is a :class:`PaddleError` — 502 ``billing_provider_unavailable``, or 503
``billing_not_configured`` for a 401/403 and for a 4xx whose code says the account or its
setup refuses (:data:`PADDLE_CONFIGURATION_CODES`) — logged once with the method, the
path, the status, Paddle's error code and its request id; never the body, never the key.

Paddle's docs (developer.paddle.com, read 2026-10-09): api-reference/transactions/
create-transaction, build/transactions/default-payment-link, paddle-js/about/
hosted-checkout, api-reference/customer-portals/create-customer-portal-session,
api-reference/subscriptions/cancel-subscription, build/subscriptions/cancel-subscriptions
(a scheduled cancellation is removed with ``scheduled_change: null``; a cancelled
subscription "can't be reinstated"), api-reference/about/authentication (the key
prefixes), api-reference/about/errors.

Read 2026-10-14 for the plan switch and the customer (§15): api-reference/subscriptions/
preview-subscription (``PATCH /subscriptions/{id}/preview``: ``update_summary.result``
``{action: credit | charge, amount}``, ``next_transaction.details.totals``, amounts integer
strings in minor units), api-reference/subscriptions/update-subscription (its answer is
the subscription, without the figures), api-reference/customers/list-customers
(``?email=`` matches exactly, active customers only, ``data`` a list),
errors/customers/customer_already_exists (409, "customer email conflicts with customer of
id ctm_…").
"""

from __future__ import annotations

import logging
import re
import types
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, get_args
from urllib.parse import quote, urlencode

from pydantic import ValidationError

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.events import APP_KEY
from eifi1_server_kit.billing.provider import PortalTarget
from eifi1_server_kit.billing.schemas import ProviderSwitchPreview
from eifi1_server_kit.billing.settings import PaddleEnvironment

if TYPE_CHECKING:
    import httpx

__all__ = [
    "PADDLE_API_BASES",
    "PADDLE_API_VERSION",
    "PADDLE_CONFIGURATION_CODES",
    "PADDLE_TIMEOUT_SECONDS",
    "PaddleClient",
    "PaddleError",
    "paddle_environment_of",
    "paddle_locale",
]

logger = logging.getLogger("eifi1_server_kit.billing")

#: Paddle's API per environment. The kit's constant, never a setting (decision 24): a typo
#: or another host can't be configured.
PADDLE_API_BASES: Mapping[PaddleEnvironment, str] = types.MappingProxyType(
    {
        PaddleEnvironment.SANDBOX: "https://sandbox-api.paddle.com",
        PaddleEnvironment.LIVE: "https://api.paddle.com",
    }
)
#: The API version every request names, the ``Paddle-Version`` header.
PADDLE_API_VERSION = "1"
#: How long one call may take, in seconds (kastlan's).
PADDLE_TIMEOUT_SECONDS = 15.0
#: Paddle's 4xx error codes that say the ACCOUNT or the deployment's setup refuses the
#: request, not Paddle being down: a :class:`PaddleError` with one of them is 503
#: ``billing_not_configured`` ("the deployment's fault"), logged at ERROR with the code,
#: like a 401/403 — not 502 ``billing_provider_unavailable`` ("try again"), which no retry
#: would fix. Picked from https://developer.paddle.com/errors (read 2026-10-16), each
#: page's status in brackets, for the calls this client makes:
#:
#: * ``transaction_checkout_url_domain_is_not_approved`` [400] — ``checkout.url``'s domain
#:   isn't approved (``checkout_page_url``; the sandbox's answer for an unapproved pay
#:   host, keksdose 2026-10-16);
#: * ``transaction_default_checkout_url_not_set`` [400] — no default payment link set;
#: * ``transaction_checkout_not_enabled`` [400] — checkouts not enabled, onboarding
#:   incomplete;
#: * ``transaction_payout_account_required`` [400] — no payout account;
#: * ``transaction_creation_blocked`` [400] — legal or compliance restrictions on the
#:   account;
#: * ``paddle_billing_not_enabled`` [403] — Paddle Billing not activated for the account;
#: * ``transaction_price_not_found`` [400] and ``subscription_price_not_found`` [400] — a
#:   price id the settings name doesn't exist (a typo, or the other environment's id).
#:
#: Not listed, deliberately: what a buyer or a subscription's state causes
#: (``customer_email_domain_not_allowed``, ``subscription_locked_*``,
#: ``subscription_payment_declined``…) and codes of calls the kit never makes (products,
#: checkout domains, notifications, ``not_available_in_sandbox``). A 5xx or no answer is
#: 502 whatever the code.
PADDLE_CONFIGURATION_CODES: frozenset[str] = frozenset(
    {
        "transaction_checkout_url_domain_is_not_approved",
        "transaction_default_checkout_url_not_set",
        "transaction_checkout_not_enabled",
        "transaction_payout_account_required",
        "transaction_creation_blocked",
        "paddle_billing_not_enabled",
        "transaction_price_not_found",
        "subscription_price_not_found",
    }
)

#: A 2025 key's prefix and its environment (api-reference/about/authentication).
_KEY_PREFIXES: Mapping[str, PaddleEnvironment] = types.MappingProxyType(
    {"pdl_sdbx_apikey_": PaddleEnvironment.SANDBOX, "pdl_live_apikey_": PaddleEnvironment.LIVE}
)
_PORTAL_TARGETS: frozenset[str] = frozenset(get_args(PortalTarget))
#: The portal session's per-subscription link for each target but the overview.
_PORTAL_LINKS: Mapping[str, str] = types.MappingProxyType(
    {"cancel": "cancel_subscription", "payment_method": "update_subscription_payment_method"}
)
#: A plan switch, both ways (§15.2.10): charged or credited now, prorated; a declined
#: charge leaves the subscription as it was.
_SWITCH_BILLING = types.MappingProxyType(
    {"proration_billing_mode": "prorated_immediately", "on_payment_failure": "prevent_change"}
)
#: The existing customer Paddle names when a create conflicts (errors/customers/
#: customer_already_exists).
_EXISTING_CUSTOMER = re.compile(r"\bctm_[0-9a-z]+")


#: The kit's seven languages (ui-kit's ``KitLanguageCode``), in ui-kit's order.
_KIT_LANGUAGES = ("de-CH", "en", "es", "fr", "it", "hu", "zh")
#: The kit's language → Paddle's checkout locale: ui-kit's ``PADDLE_LOCALES``
#: (``src/billing/pay-page.ts``), so a hosted checkout speaks what the pay page does.
#: Paddle's checkout locales (read 2026-10-09): ar, zh-Hans, zh-TW, da, nl, en, fr, de,
#: it, ja, ko, no, pl, pt, pt-BR, tr, ru, es, sv — no Hungarian, so a Hungarian buyer gets
#: English always, not Paddle's guess from the browser.
_PADDLE_LOCALES: Mapping[str, str] = types.MappingProxyType(
    {"en": "en", "de-CH": "de", "fr": "fr", "it": "it", "es": "es", "zh": "zh-Hans", "hu": "en"}
)


def _kit_language_of(tag: str) -> str | None:
    """The kit language ``tag`` names, or ``None`` — ui-kit's ``kitLanguageOf``: exact
    first, ignoring case and reading ``_`` as ``-``; then by language, so every German is
    ``de-CH``, every Chinese ``zh``, and any regional form its base (``fr-CA`` → ``fr``)."""
    lower = tag.strip().replace("_", "-").lower()
    if not lower:
        return None
    exact = next((code for code in _KIT_LANGUAGES if code.lower() == lower), None)
    if exact is not None:
        return exact
    language = lower.split("-", 1)[0]
    return next((code for code in _KIT_LANGUAGES if code.split("-", 1)[0].lower() == language), None)


def paddle_locale(kit_locale: str) -> str:
    """The kit's locale → Paddle's checkout locale, as ui-kit's ``paddleLocale``:
    ``de-CH`` → ``de``, ``zh`` → ``zh-Hans``, ``hu`` → ``en`` (Paddle has no Hungarian),
    the others as they are. Any tag is read as the kit reads it (``de-AT`` and ``de`` are
    the one German, ``zh-TW`` the one Chinese); one the kit doesn't ship is English.
    :class:`PaddleClient` sends it as a hosted checkout's ``locale``."""
    return _PADDLE_LOCALES[_kit_language_of(kit_locale) or "en"]


def paddle_environment_of(api_key: str) -> PaddleEnvironment | None:
    """The environment a Paddle API key's prefix names — ``pdl_sdbx_apikey_…`` is the
    sandbox, ``pdl_live_apikey_…`` live — or ``None`` for a key without one: a legacy key
    (from before 2025-05-06, 50 random characters), which needs ``billing_environment``
    set."""
    key = api_key.strip()
    return next((environment for prefix, environment in _KEY_PREFIXES.items() if key.startswith(prefix)), None)


class PaddleError(BillingError):
    """A Paddle call that failed: ``billing_provider_unavailable`` (502) — no answer, a 429
    or 5xx, a request Paddle refused, a 2xx without the fields the kit reads — or
    ``billing_not_configured`` (503) for a 401/403, a wrong key or one without the
    permission, and for a 4xx with one of :data:`PADDLE_CONFIGURATION_CODES` (an
    unapproved checkout domain, an account not set up): the deployment's fault, not "try
    again".

    The client-facing ``detail`` is the kit's (``str(error)``, what the contract's error
    handlers answer). Paddle's own words stay on the exception, for the log and for Paddle
    support; never the key or the body.
    """

    #: Paddle's HTTP status; ``0``: no answer.
    status: int
    #: Paddle's ``error.code`` (``not_found``, ``forbidden``…), when it sent one.
    paddle_code: str | None
    #: Paddle's ``error.detail``, when it sent one.
    paddle_detail: str | None
    #: Paddle's ``meta.request_id``: what Paddle support asks for.
    request_id: str | None

    def __init__(
        self,
        status: int,
        *,
        paddle_code: str | None = None,
        paddle_detail: str | None = None,
        request_id: str | None = None,
    ) -> None:
        configuration = status in (401, 403) or (400 <= status < 500 and paddle_code in PADDLE_CONFIGURATION_CODES)
        super().__init__(
            BillingErrorCode.BILLING_NOT_CONFIGURED if configuration else BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE
        )
        self.status = status
        self.paddle_code = paddle_code
        self.paddle_detail = paddle_detail
        self.request_id = request_id


def _require_httpx() -> None:
    try:
        import httpx  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "PaddleClient needs httpx: install the kit with its billing extra, eifi1-server-kit[billing]."
        ) from exc


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class _Answer(NamedTuple):
    """A 2xx from Paddle: its status, ``data`` and ``meta`` — and, for a list, ``data``'s
    entries as ``rows`` (``data`` then empty)."""

    status: int
    data: Mapping[str, Any]
    meta: Mapping[str, Any]
    rows: tuple[Any, ...] = ()


class PaddleClient:
    """Paddle Billing's API, the kit's
    :class:`~eifi1_server_kit.billing.BillingProviderClient` (§14.2).

    * ``api_key`` — the deployment's secret, with the permissions the module names; its
      prefix must not name the other ``environment`` (:class:`ValueError`).
    * ``environment`` — ``sandbox`` or ``live``: the API is
      :data:`PADDLE_API_BASES`' entry, never a URL of the app's.
    * ``app`` — the settings' ``billing_app``, put into every checkout's custom data as
      ``"app"`` whatever the caller passed (§14.3), so an app can't forget it.
    * ``checkout_page_url`` — the kit's pay page on the app's pay host (§14.4), sent as the
      transaction's ``checkout.url``; unset, the account's default payment link.
    * ``hosted_checkout_url`` — a Paddle hosted checkout's launch URL; it wins over the
      pay page: the checkout answers it with ``?transaction_id=…`` (and ``&locale=…``,
      the buyer's language as Paddle names it, :func:`paddle_locale`).
    * ``client`` — an ``httpx.AsyncClient`` the app owns and closes; without it each call
      opens and closes its own (as :class:`~eifi1_server_kit.mail.ResendClient`).
    """

    def __init__(
        self,
        api_key: str,
        *,
        environment: PaddleEnvironment | str,
        app: str | None = None,
        checkout_page_url: str | None = None,
        hosted_checkout_url: str | None = None,
        timeout: float = PADDLE_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _require_httpx()
        if not api_key.strip():
            raise ValueError("PaddleClient needs an API key")
        self._environment = PaddleEnvironment(environment)
        of_key = paddle_environment_of(api_key)
        if of_key is not None and of_key is not self._environment:
            raise ValueError(f"a {of_key} key can't call Paddle's {self._environment} API")
        if app is not None and not app.strip():
            raise ValueError("an app tag is a non-empty string")
        self._key = api_key.strip()
        self._base = PADDLE_API_BASES[self._environment]
        self._app = app
        self._checkout_page_url = checkout_page_url
        self._hosted_checkout_url = hosted_checkout_url
        self._timeout = timeout
        self._client = client

    @property
    def environment(self) -> PaddleEnvironment:
        """The API this client calls."""
        return self._environment

    async def checkout_url(
        self,
        *,
        price_id: str,
        custom_data: Mapping[str, str],
        customer_id: str | None = None,
        locale: str | None = None,
        customer_email: str | None = None,
    ) -> str:
        """Create the checkout's transaction — ``POST /transactions`` with one ``price_id``
        (quantity 1), ``custom_data`` plus the app's tag, the payer's customer, and the pay
        page as ``checkout.url`` — and answer the URL that opens it. The customer is
        ``customer_id`` when given, else :meth:`customer_for` ``customer_email`` when given
        (§15.2.11), else none (Paddle's checkout asks for an e-mail). The URL is
        with a hosted checkout, ``<hosted>?transaction_id=<txn>`` (and, when ``locale`` is
        given, ``&locale=`` its :func:`paddle_locale`: the app's ``de-CH`` is Paddle's
        ``de``); otherwise the transaction's ``checkout.url``, the pay page plus
        ``?_ptxn=<txn>`` — the pay page maps the language itself (ui-kit's ``payPageUrl``).
        A 2xx without the id or the URL is a :class:`PaddleError` (502).
        """
        data: dict[str, str] = dict(custom_data)
        if self._app is not None:
            data[APP_KEY] = self._app
        body: dict[str, Any] = {"items": [{"price_id": price_id, "quantity": 1}], "custom_data": data}
        if not customer_id and customer_email and customer_email.strip():
            customer_id = await self.customer_for(email=customer_email)
        if customer_id:
            body["customer_id"] = customer_id
        if self._checkout_page_url and not self._hosted_checkout_url:
            body["checkout"] = {"url": self._checkout_page_url}
        answer = await self._call("POST", "/transactions", body)
        transaction_id = _text(answer.data.get("id"))
        if transaction_id is None:
            raise _malformed("POST", "/transactions", answer, "data.id")
        if self._hosted_checkout_url:
            language = paddle_locale(locale) if locale and locale.strip() else None
            query = {"transaction_id": transaction_id, **({"locale": language} if language else {})}
            joiner = "&" if "?" in self._hosted_checkout_url else "?"
            return f"{self._hosted_checkout_url}{joiner}{urlencode(query)}"
        checkout = answer.data.get("checkout")
        url = _text(checkout.get("url")) if isinstance(checkout, Mapping) else None
        if url is None:
            raise _malformed("POST", "/transactions", answer, "data.checkout.url")
        return url

    async def portal_url(
        self,
        *,
        customer_id: str | None,
        subscription_id: str | None = None,
        target: PortalTarget = "overview",
    ) -> str:
        """A fresh customer-portal session's link — ``POST
        /customers/{ctm}/portal-sessions`` with the subscription when given. Its token is
        temporary: never store the link, and never embed the portal in a frame (Paddle).

        ``overview`` answers ``urls.general.overview``; ``cancel`` and ``payment_method``
        the subscription's ``cancel_subscription`` or ``update_subscription_payment_method``
        link, or the overview where Paddle gives none (a paused or cancelled
        subscription). No ``customer_id`` — the payer never reached Paddle — is 409
        ``billing_not_at_provider`` (§14.5), without a request.
        """
        if target not in _PORTAL_TARGETS:
            raise ValueError(f"a portal target is one of {', '.join(sorted(_PORTAL_TARGETS))}: {target!r}")
        if not customer_id:
            raise BillingError(BillingErrorCode.BILLING_NOT_AT_PROVIDER)
        path = f"/customers/{quote(customer_id, safe='')}/portal-sessions"
        body: dict[str, Any] = {"subscription_ids": [subscription_id]} if subscription_id else {}
        answer = await self._call("POST", path, body)
        urls = answer.data.get("urls")
        general = urls.get("general") if isinstance(urls, Mapping) else None
        overview = _text(general.get("overview")) if isinstance(general, Mapping) else None
        if overview is None:
            raise _malformed("POST", path, answer, "data.urls.general.overview")
        if target == "overview":
            return overview
        subscriptions = urls.get("subscriptions") if isinstance(urls, Mapping) else None
        for entry in subscriptions if isinstance(subscriptions, list) else []:
            if not isinstance(entry, Mapping) or (subscription_id and entry.get("id") != subscription_id):
                continue
            link = _text(entry.get(_PORTAL_LINKS[target]))
            if link is not None:
                return link
        return overview

    async def cancel(self, *, subscription_id: str, immediately: bool = False) -> None:
        """Cancel a subscription — ``POST /subscriptions/{sub}/cancel`` with
        ``effective_from`` ``next_billing_period`` (the default: it stays ``active`` with
        ``scheduled_change.action = cancel`` until its period ends) or ``immediately``
        (Paddle cancels a paused subscription only so). A ``past_due`` one can be cancelled
        too; it stays ``past_due`` to its period's end."""
        effective_from = "immediately" if immediately else "next_billing_period"
        await self._call(
            "POST", f"/subscriptions/{quote(subscription_id, safe='')}/cancel", {"effective_from": effective_from}
        )

    async def remove_scheduled_cancel(self, *, subscription_id: str) -> None:
        """Undo a cancellation at the period's end — ``PATCH /subscriptions/{sub}
        {"scheduled_change": null}`` (§14.8). The subscription stays ``active``, and Paddle
        sends ``subscription.updated``, which the row follows. A cancelled subscription
        can't be reinstated: Paddle refuses, a :class:`PaddleError`."""
        await self._call("PATCH", f"/subscriptions/{quote(subscription_id, safe='')}", {"scheduled_change": None})

    async def preview_plan_change(self, *, subscription_id: str, price_id: str) -> ProviderSwitchPreview:
        """What switching to ``price_id`` would do (§15.2.10) — ``PATCH
        /subscriptions/{sub}/preview`` with the switch's body (:meth:`change_plan`), which
        changes nothing: ``currency`` Paddle's ``currency_code``; ``now_minor``
        ``update_summary.result.amount``, negative where its ``action`` is ``credit``
        (``0`` with no summary); ``next_minor`` ``next_transaction.details.totals.total`` (the
        plan's price; ``grand_total`` ignores the switch's own credit); ``next_billed_at`` Paddle's. A 2xx without them
        is a :class:`PaddleError` (502).
        (developer.paddle.com/api-reference/subscriptions/preview-subscription)"""
        path = f"/subscriptions/{quote(subscription_id, safe='')}/preview"
        answer = await self._call("PATCH", path, _switch_body(price_id))
        return _switch_preview("PATCH", path, answer)

    async def change_plan(self, *, subscription_id: str, price_id: str) -> ProviderSwitchPreview:
        """Switch the subscription to ``price_id`` (§15.2.10): ``PATCH /subscriptions/{sub}
        {items: [{price_id, quantity: 1}], proration_billing_mode: "prorated_immediately",
        on_payment_failure: "prevent_change"}`` — the items replace the subscription's;
        an upgrade is charged now, a downgrade credited to the customer's balance; a
        declined charge changes nothing and Paddle refuses (a :class:`PaddleError`).

        Paddle's answer is the subscription, without the figures, so the switch is
        previewed first (:meth:`preview_plan_change`, which changes nothing) and its
        figures answered: what the payer just agreed to, as Paddle reckons it now. Two
        requests; a failed preview changes nothing.
        (developer.paddle.com/api-reference/subscriptions/update-subscription)"""
        figures = await self.preview_plan_change(subscription_id=subscription_id, price_id=price_id)
        await self._call("PATCH", f"/subscriptions/{quote(subscription_id, safe='')}", _switch_body(price_id))
        return figures

    async def customer_for(self, *, email: str) -> str:
        """The shared customer for ``email`` (§15.2.11): ``GET /customers?email=`` (an
        exact match, active customers only), else ``POST /customers {email}``; a create
        that conflicts — another app's checkout created it in between — answers the
        customer Paddle names in its 409 ``customer_already_exists``. An app never changes
        the customer: name, address and e-mail are the buyer's, in Paddle's checkout or
        portal. The address is never logged.
        (developer.paddle.com/api-reference/customers/list-customers, …/create-customer)"""
        address = email.strip()
        if not address:
            raise ValueError("a customer's e-mail address is a non-empty string")
        found = await self._call("GET", "/customers", None, params={"email": address}, listing=True)
        for entry in found.rows:
            customer_id = _text(entry.get("id")) if isinstance(entry, Mapping) else None
            if customer_id is not None:
                return customer_id
        try:
            created = await self._call("POST", "/customers", {"email": address})
        except PaddleError as exc:
            existing = _EXISTING_CUSTOMER.search(exc.paddle_detail or "")
            if exc.paddle_code == "customer_already_exists" and existing is not None:
                return existing.group(0)
            raise
        customer_id = _text(created.data.get("id"))
        if customer_id is None:
            raise _malformed("POST", "/customers", created, "data.id")
        return customer_id

    # --- the one request ---------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        params: Mapping[str, str] | None = None,
        listing: bool = False,
    ) -> _Answer:
        """One request: Paddle's 2xx with its ``data`` — a list's entries as ``rows`` when
        ``listing`` — or a :class:`PaddleError`. ``body`` ``None`` sends none (a GET);
        ``params`` go into the query, never into a log line."""
        import httpx

        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._key}", "Paddle-Version": PADDLE_API_VERSION}
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, json=body, params=params, headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    response = await http.request(method, url, json=body, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Paddle %s %s: no answer (%s)", method, path, type(exc).__name__)
            raise PaddleError(0) from None
        payload = _json_object(response)
        meta = payload.get("meta")
        meta = meta if isinstance(meta, Mapping) else {}
        if response.status_code >= 400:
            error = payload.get("error")
            error = error if isinstance(error, Mapping) else {}
            failure = PaddleError(
                response.status_code,
                paddle_code=_text(error.get("code")),
                paddle_detail=_text(error.get("detail")),
                request_id=_text(meta.get("request_id")),
            )
            logger.log(
                logging.ERROR if failure.code is BillingErrorCode.BILLING_NOT_CONFIGURED else logging.WARNING,
                "Paddle %s %s answered %d %s (request %s)",
                method,
                path,
                failure.status,
                failure.paddle_code or "-",
                failure.request_id or "-",
            )
            raise failure
        data = payload.get("data")
        if listing:
            answer = _Answer(response.status_code, {}, meta, tuple(data) if isinstance(data, list) else ())
            if not isinstance(data, list):
                raise _malformed(method, path, answer, "data")
            return answer
        answer = _Answer(response.status_code, data if isinstance(data, Mapping) else {}, meta)
        if not isinstance(data, Mapping):
            raise _malformed(method, path, answer, "data")
        return answer


def _switch_body(price_id: str) -> dict[str, Any]:
    """A plan switch's body (§15.2.10): the one item, prorated immediately, nothing changed
    on a declined charge."""
    return {"items": [{"price_id": price_id, "quantity": 1}], **_SWITCH_BILLING}


class _MissingFieldError(Exception):
    """A field the kit reads, missing from Paddle's 2xx: its path."""


def _minor(parent: object, key: str, where: str) -> int:
    """An amount Paddle sends as an integer string in minor units (``"103431"``)."""
    value = parent.get(key) if isinstance(parent, Mapping) else None
    if not isinstance(value, str) or not value.isdecimal():
        raise _MissingFieldError(where)
    return int(value)


def _switch_preview(method: str, path: str, answer: _Answer) -> ProviderSwitchPreview:
    """A preview's figures (:meth:`PaddleClient.preview_plan_change`), or a 502."""
    data = answer.data
    try:
        summary = data.get("update_summary")
        result = summary.get("result") if isinstance(summary, Mapping) else None
        now_minor = 0
        if result is not None:
            action = result.get("action") if isinstance(result, Mapping) else None
            if action not in ("charge", "credit"):
                raise _MissingFieldError("data.update_summary.result.action")
            amount = _minor(result, "amount", "data.update_summary.result.amount")
            now_minor = -amount if action == "credit" else amount
        following = data.get("next_transaction")
        next_minor: int | None = None
        if following is not None:
            details = following.get("details") if isinstance(following, Mapping) else None
            totals = details.get("totals") if isinstance(details, Mapping) else None
            # The plan's price, not `grand_total`: Paddle's preview takes the customer's
            # EXISTING balance off `grand_total` but not the credit this very switch creates
            # (sandbox 2026-10-16: a downgrade crediting 84.00 showed grand_total 4.41 against
            # total 5.00), so `grand_total` would understate what the next period costs.
            next_minor = _minor(totals, "total", "data.next_transaction.details.totals.total")
        return ProviderSwitchPreview.model_validate(
            {
                "currency": data.get("currency_code"),
                "now_minor": now_minor,
                "next_minor": next_minor,
                "next_billed_at": data.get("next_billed_at"),
            }
        )
    except _MissingFieldError as missing:
        raise _malformed(method, path, answer, str(missing)) from None
    except ValidationError:
        raise _malformed(method, path, answer, "data.currency_code or data.next_billed_at") from None


def _malformed(method: str, path: str, answer: _Answer, field: str) -> PaddleError:
    """A 2xx without a field the kit reads: 502, logged like a refusal (kastlan read the
    answer raw, so a missing field was a ``KeyError``, a 500)."""
    request_id = _text(answer.meta.get("request_id"))
    logger.warning(
        "Paddle %s %s answered %d without %s (request %s)", method, path, answer.status, field, request_id or "-"
    )
    return PaddleError(answer.status, request_id=request_id)


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    """The answer's JSON object, or an empty one: a proxy's HTML error page is no JSON."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, Mapping) else {}
