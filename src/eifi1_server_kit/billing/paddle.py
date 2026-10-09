"""Paddle Billing's API: the checkout, the customer portal, a cancellation and undoing one.

``docs/billing-harmonization.md`` §14.2 in ``Eifi1/ui-kit``, lifted from kastlan's
``adapters/billing/paddle_client.py`` (c0b1a24). :class:`PaddleClient` is the kit's one
:class:`~eifi1_server_kit.billing.BillingProviderClient`; build it through
:func:`~eifi1_server_kit.billing.billing_provider_client`, which reads the settings.

**httpx is the ``billing`` extra** (``eifi1-server-kit[billing]``), imported lazily as
:class:`~eifi1_server_kit.mail.ResendClient` does, so ``import eifi1_server_kit.billing``
never needs it; only building a client does.

**The key's minimum permissions** (Paddle's 2025 keys, Paddle > Developer tools >
Authentication): transaction write (the checkout), customer-portal-session write (the
portal) and subscription write (a cancellation, and undoing one). The key's prefix says its
environment, ``pdl_sdbx_apikey_`` or ``pdl_live_apikey_`` (:func:`paddle_environment_of`);
a sandbox key works only against the sandbox's API and a live key only against live's.

**Every call** sends ``Authorization: Bearer <key>`` and ``Paddle-Version: 1``, through
the app's one ``httpx.AsyncClient`` when it passes one (else a client per call), and is
tried ONCE: creating a transaction is not idempotent, so a retry could open two. A failure
is a :class:`PaddleError` — 502 ``billing_provider_unavailable``, or 503
``billing_not_configured`` for a 401/403 — logged once with the method, the path, the
status, Paddle's error code and its request id; never the body, never the key.

Paddle's docs (developer.paddle.com, read 2026-10-09): api-reference/transactions/
create-transaction, build/transactions/default-payment-link, paddle-js/about/
hosted-checkout, api-reference/customer-portals/create-customer-portal-session,
api-reference/subscriptions/cancel-subscription, build/subscriptions/cancel-subscriptions
(a scheduled cancellation is removed with ``scheduled_change: null``; a cancelled
subscription "can't be reinstated"), api-reference/about/authentication (the key
prefixes), api-reference/about/errors.
"""

from __future__ import annotations

import logging
import types
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, get_args
from urllib.parse import quote, urlencode

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.events import APP_KEY
from eifi1_server_kit.billing.provider import PortalTarget
from eifi1_server_kit.billing.settings import PaddleEnvironment

if TYPE_CHECKING:
    import httpx

__all__ = [
    "PADDLE_API_BASES",
    "PADDLE_API_VERSION",
    "PADDLE_TIMEOUT_SECONDS",
    "PaddleClient",
    "PaddleError",
    "paddle_environment_of",
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

#: A 2025 key's prefix and its environment (api-reference/about/authentication).
_KEY_PREFIXES: Mapping[str, PaddleEnvironment] = types.MappingProxyType(
    {"pdl_sdbx_apikey_": PaddleEnvironment.SANDBOX, "pdl_live_apikey_": PaddleEnvironment.LIVE}
)
_PORTAL_TARGETS: frozenset[str] = frozenset(get_args(PortalTarget))
#: The portal session's per-subscription link for each target but the overview.
_PORTAL_LINKS: Mapping[str, str] = types.MappingProxyType(
    {"cancel": "cancel_subscription", "payment_method": "update_subscription_payment_method"}
)


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
    permission: the deployment's fault, not "try again".

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
        super().__init__(
            BillingErrorCode.BILLING_NOT_CONFIGURED
            if status in (401, 403)
            else BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE
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
    """A 2xx from Paddle: its status, ``data`` and ``meta``."""

    status: int
    data: Mapping[str, Any]
    meta: Mapping[str, Any]


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
      pay page: the checkout answers it with ``?transaction_id=…`` (and ``&locale=…``).
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
    ) -> str:
        """Create the checkout's transaction — ``POST /transactions`` with one ``price_id``
        (quantity 1), ``custom_data`` plus the app's tag, the payer's ``customer_id`` when
        known, and the pay page as ``checkout.url`` — and answer the URL that opens it:
        with a hosted checkout, ``<hosted>?transaction_id=<txn>`` (``&locale=<locale>`` when
        given); otherwise the transaction's ``checkout.url``, the pay page plus
        ``?_ptxn=<txn>``. A 2xx without the id or the URL is a :class:`PaddleError` (502).
        """
        data: dict[str, str] = dict(custom_data)
        if self._app is not None:
            data[APP_KEY] = self._app
        body: dict[str, Any] = {"items": [{"price_id": price_id, "quantity": 1}], "custom_data": data}
        if customer_id:
            body["customer_id"] = customer_id
        if self._checkout_page_url and not self._hosted_checkout_url:
            body["checkout"] = {"url": self._checkout_page_url}
        answer = await self._call("POST", "/transactions", body)
        transaction_id = _text(answer.data.get("id"))
        if transaction_id is None:
            raise _malformed("POST", "/transactions", answer, "data.id")
        if self._hosted_checkout_url:
            query = {"transaction_id": transaction_id, **({"locale": locale} if locale else {})}
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

    # --- the one request ---------------------------------------------------------------

    async def _call(self, method: str, path: str, body: Mapping[str, Any]) -> _Answer:
        """One request: Paddle's 2xx with its ``data``, or a :class:`PaddleError`."""
        import httpx

        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._key}", "Paddle-Version": PADDLE_API_VERSION}
        try:
            if self._client is not None:
                response = await self._client.request(method, url, json=body, headers=headers, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    response = await http.request(method, url, json=body, headers=headers)
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
        answer = _Answer(response.status_code, data if isinstance(data, Mapping) else {}, meta)
        if not isinstance(data, Mapping):
            raise _malformed(method, path, answer, "data")
        return answer


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
