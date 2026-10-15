"""Test helpers for billing: Paddle's notifications, signed, and fakes of the provider.

``docs/billing-harmonization.md`` §14.9 in ``Eifi1/ui-kit``. In the wheel, because the
apps' local CLIs import it at runtime to review billing without a provider account, and
NOT re-exported from :mod:`eifi1_server_kit.billing`: import it as
``eifi1_server_kit.billing.testing``. The standard library only; :class:`PaddleApiFake`
imports httpx when it is used.

It replaces four copies of one recipe: keksdose's and Kurvenschmiede's
``billing_fixtures.py`` (``STEPS``, ``_PERIOD``, ``paddle_event``, ``sign_paddle``),
kastlan's hand signing in ``tests/api/test_billing.py``, and the kit's own. Each app keeps
only its CLI — argparse, the payer lookup, the local-only guard, ``--end-grant`` — about
50 lines::

    raw, headers = signed_paddle_event(
        "checkout", secret=settings.billing_webhook_key(), payer_ref=payer_ref(user.id),
        price_id=settings.billing_price_id("pro", "CHF", "year"), app=settings.billing_app,
    )
    status, text = post_signed("http://localhost:8000/api/v1/webhooks/paddle", raw, headers)

* :func:`paddle_event` — one subscription notification as Paddle sends it, for each step of
  a subscription's life (:data:`PADDLE_STEPS`), the scheduled cancellation included;
* :func:`sign_paddle`, :func:`signed_paddle_event` — its ``Paddle-Signature``;
* :func:`post_signed` — the local CLI's POST, refused for any host but this machine;
* :class:`FakeBillingProvider` — a :class:`~eifi1_server_kit.billing.BillingProviderClient`
  without HTTP that records every call, for the app's module-level seam (§14.8) — the plan
  switch, its preview and the shared customer included (§15);
* :class:`PaddleApiFake` — Paddle's API as an ``httpx.MockTransport``, answering Paddle's
  documented examples (:data:`PADDLE_API_EXAMPLES`), for testing a
  :class:`~eifi1_server_kit.billing.PaddleClient` itself.

Synthetic data only: no real customer, subscription or address.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
import types
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.events import checkout_custom_data
from eifi1_server_kit.billing.paddle import PaddleError
from eifi1_server_kit.billing.provider import PortalTarget
from eifi1_server_kit.billing.schemas import ProviderSwitchPreview
from eifi1_server_kit.billing.signatures import PADDLE_SIGNATURE_HEADER

if TYPE_CHECKING:
    import httpx

__all__ = [
    "PADDLE_API_EXAMPLES",
    "PADDLE_STEPS",
    "PADDLE_STEP_PERIODS",
    "FakeBillingProvider",
    "PaddleApiFake",
    "PaddleStep",
    "paddle_event",
    "post_signed",
    "sign_paddle",
    "signed_paddle_event",
]

#: A step of a subscription's life, in the order it lives them (§13.4).
PaddleStep = Literal["trial", "checkout", "renewal", "plan_switched", "cancel_scheduled", "payment_failed", "cancelled"]

#: Each step's Paddle event type and the subscription status it carries.
#: ``cancel_scheduled`` is the payer cancelling for the period's end — a
#: ``subscription.updated``, still ``active``, with ``scheduled_change.action`` ``cancel`` —
#: which the kit promotes to ``subscription_canceled`` (§12.26's "Cancel" produces it).
#: ``plan_switched`` is a plan switch landing (§15.2.10): a ``subscription.updated``,
#: ``active``, on the new ``price_id``, its period as it was.
PADDLE_STEPS: Mapping[PaddleStep, tuple[str, str]] = types.MappingProxyType(
    {
        "trial": ("subscription.trialing", "trialing"),
        "checkout": ("subscription.created", "active"),
        "renewal": ("subscription.updated", "active"),
        "plan_switched": ("subscription.updated", "active"),
        "cancel_scheduled": ("subscription.updated", "active"),
        "payment_failed": ("subscription.past_due", "past_due"),
        "cancelled": ("subscription.canceled", "canceled"),
    }
)

#: How far each step's billing period reaches from the event (keksdose's and
#: Kurvenschmiede's). ``cancelled`` has none: Paddle sends ``current_billing_period: null``
#: and ``canceled_at``.
PADDLE_STEP_PERIODS: Mapping[PaddleStep, timedelta] = types.MappingProxyType(
    {
        "trial": timedelta(days=14),
        "checkout": timedelta(days=365),
        "renewal": timedelta(days=730),
        "plan_switched": timedelta(days=365),
        "cancel_scheduled": timedelta(days=365),
        "payment_failed": timedelta(days=365),
    }
)

#: The hosts :func:`post_signed` posts to: this machine only.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def paddle_event(
    step: PaddleStep,
    *,
    payer_ref: str,
    price_id: str,
    app: str | None = None,
    at: datetime | None = None,
    subscription_id: str = "sub_local",
    customer_id: str = "ctm_local",
    event_id: str | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    """One Paddle notification for ``step``, as Paddle sends it: the envelope
    (``event_id``, ``event_type``, ``occurred_at``, ``notification_id``) and the
    subscription in ``data`` — its id, status, customer, ``currency_code``, one recurring
    item on ``price_id``, the billing period, ``scheduled_change``, ``canceled_at`` and the
    checkout's custom data, :func:`~eifi1_server_kit.billing.checkout_custom_data`
    (``payer_ref``, ``app=app``).

    * ``at`` — when it happened (now by default); the period starts there.
    * ``period_end`` — the period's end, else ``at`` plus :data:`PADDLE_STEP_PERIODS`'
      length; ``cancel_scheduled``'s ``scheduled_change.effective_at`` too.
    * ``cancelled`` — ``current_billing_period`` ``None`` and ``canceled_at`` ``at``, as
      Paddle sends it (kastlan's kept the period); ``period_end`` doesn't apply.
    * ``cancel_scheduled`` — ``subscription.updated``, ``active``, with
      ``scheduled_change`` ``{"action": "cancel", "effective_at": <period end>}``.

    ``event_id`` defaults to ``evt_local_<step>_<ms>``. Serialise it with ``json.dumps``
    before signing (:func:`signed_paddle_event` does both).
    """
    moment = at or datetime.now(UTC)
    event_type, status = PADDLE_STEPS[step]
    data: dict[str, Any] = {
        "id": subscription_id,
        "status": status,
        "customer_id": customer_id,
        "currency_code": "CHF",
        "items": [{"status": "active", "quantity": 1, "recurring": True, "price": {"id": price_id}}],
        "scheduled_change": None,
        "canceled_at": None,
        "custom_data": checkout_custom_data(payer_ref, app=app),
    }
    if step == "cancelled":
        data["current_billing_period"] = None
        data["canceled_at"] = moment.isoformat()
    else:
        ends_at = period_end or moment + PADDLE_STEP_PERIODS[step]
        data["current_billing_period"] = {"starts_at": moment.isoformat(), "ends_at": ends_at.isoformat()}
        if step == "cancel_scheduled":
            data["scheduled_change"] = {"action": "cancel", "effective_at": ends_at.isoformat(), "resume_at": None}
    stamp = int(moment.timestamp() * 1000)
    return {
        "event_id": event_id or f"evt_local_{step}_{stamp}",
        "event_type": event_type,
        "occurred_at": moment.isoformat(),
        "notification_id": f"ntf_local_{step}_{stamp}",
        "data": data,
    }


def sign_paddle(raw_body: bytes, secret: str, *, now: float | None = None) -> dict[str, str]:
    """The headers Paddle sends with ``raw_body``: ``{"Paddle-Signature": "ts=…;h1=…",
    "Content-Type": "application/json"}`` — ``h1`` the hex HMAC-SHA256 of ``"<ts>:<body>"``
    under the notification destination's secret, ``ts`` ``now`` (Unix seconds, the current
    time by default)."""
    stamp = str(int(time.time() if now is None else now))
    digest = hmac.new(secret.encode("utf-8"), stamp.encode("ascii") + b":" + raw_body, hashlib.sha256).hexdigest()
    return {PADDLE_SIGNATURE_HEADER: f"ts={stamp};h1={digest}", "Content-Type": "application/json"}


def signed_paddle_event(
    step: PaddleStep, *, secret: str, now: float | None = None, **fields: Any
) -> tuple[bytes, dict[str, str]]:
    """:func:`paddle_event` for ``step`` with ``fields``, as the raw body and the headers
    :func:`sign_paddle` gives it — what the webhook route receives."""
    raw = json.dumps(paddle_event(step, **fields)).encode("utf-8")
    return raw, sign_paddle(raw, secret, now=now)


def post_signed(url: str, raw_body: bytes, headers: Mapping[str, str], *, timeout: float = 30) -> tuple[int, str]:
    """POST a signed event to the app's own webhook — for the apps' local CLIs — and answer
    the status and the body's text. A refusal (4xx, 5xx) is answered too, not raised.

    Refuses (:class:`ValueError`) any URL but ``http(s)://`` on ``localhost``,
    ``127.0.0.1`` or ``::1``: fixture events are for a local instance, never a deployed
    one. A server that doesn't answer raises :class:`urllib.error.URLError`.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOCAL_HOSTS:
        raise ValueError(f"fixture events go to a local instance only, not {url!r}")
    request = urllib.request.Request(url, data=raw_body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return int(answer.status), answer.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode("utf-8", errors="replace")


class FakeBillingProvider:
    """A :class:`~eifi1_server_kit.billing.BillingProviderClient` without HTTP, for the
    app's tests behind its module-level seam (§14.8): it records every call's arguments
    and answers ``checkout_url`` / ``portal_url``. With ``fail``, each call is recorded and
    then fails as Paddle not answering does: a
    :class:`~eifi1_server_kit.billing.PaddleError`, 502 ``billing_provider_unavailable``.

    A portal request without a customer is 409 ``billing_not_at_provider``, as the real
    client answers it, and not recorded: it never reaches the provider.

    The plan switch (§15.2.10): ``preview_plan_change`` and ``change_plan`` answer
    ``preview`` (by default a charge of 10.00 CHF now and 79.00 on 2027-10-08) and record
    into :attr:`previews` and :attr:`switches`. The shared customer (§15.2.11):
    ``customer_for`` records into :attr:`customers` and answers ``ctm_fake_<n>``, the same
    id for the same address; a checkout with ``customer_email`` and no ``customer_id``
    asks it, as the real client does, and records the id it got.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        checkout_url: str = "https://checkout.example/txn_test",
        portal_url: str = "https://portal.example/ctm_test",
        preview: ProviderSwitchPreview | None = None,
    ) -> None:
        self.fail = fail
        self.checkout_answer = checkout_url
        self.portal_answer = portal_url
        self.preview_answer = preview or ProviderSwitchPreview(
            currency="CHF", now_minor=1000, next_minor=7900, next_billed_at=datetime(2027, 10, 8, 9, 0, tzinfo=UTC)
        )
        #: Every ``preview_plan_change`` call's arguments.
        self.previews: list[dict[str, object]] = []
        #: Every ``change_plan`` call's arguments.
        self.switches: list[dict[str, object]] = []
        #: Every ``customer_for`` call's arguments.
        self.customers: list[dict[str, object]] = []
        self._customer_ids: dict[str, str] = {}
        #: Every ``checkout_url`` call's arguments.
        self.checkouts: list[dict[str, object]] = []
        #: Every ``portal_url`` call's arguments.
        self.portals: list[dict[str, object]] = []
        #: Every ``cancel`` call's arguments.
        self.cancels: list[dict[str, object]] = []
        #: Every ``remove_scheduled_cancel`` call's arguments.
        self.resumes: list[dict[str, object]] = []

    def _failing(self) -> None:
        if self.fail:
            raise PaddleError(0)

    async def checkout_url(
        self,
        *,
        price_id: str,
        custom_data: Mapping[str, str],
        customer_id: str | None = None,
        locale: str | None = None,
        customer_email: str | None = None,
    ) -> str:
        checkout: dict[str, object] = {
            "price_id": price_id,
            "custom_data": dict(custom_data),
            "customer_id": customer_id,
            "locale": locale,
            "customer_email": customer_email,
        }
        self.checkouts.append(checkout)
        self._failing()
        if not customer_id and customer_email and customer_email.strip():
            checkout["customer_id"] = await self.customer_for(email=customer_email)
        return self.checkout_answer

    async def portal_url(
        self,
        *,
        customer_id: str | None,
        subscription_id: str | None = None,
        target: PortalTarget = "overview",
    ) -> str:
        if not customer_id:
            raise BillingError(BillingErrorCode.BILLING_NOT_AT_PROVIDER)
        self.portals.append({"customer_id": customer_id, "subscription_id": subscription_id, "target": target})
        self._failing()
        return self.portal_answer

    async def cancel(self, *, subscription_id: str, immediately: bool = False) -> None:
        self.cancels.append({"subscription_id": subscription_id, "immediately": immediately})
        self._failing()

    async def remove_scheduled_cancel(self, *, subscription_id: str) -> None:
        self.resumes.append({"subscription_id": subscription_id})
        self._failing()

    async def preview_plan_change(self, *, subscription_id: str, price_id: str) -> ProviderSwitchPreview:
        self.previews.append({"subscription_id": subscription_id, "price_id": price_id})
        self._failing()
        return self.preview_answer

    async def change_plan(self, *, subscription_id: str, price_id: str) -> ProviderSwitchPreview:
        self.switches.append({"subscription_id": subscription_id, "price_id": price_id})
        self._failing()
        return self.preview_answer

    async def customer_for(self, *, email: str) -> str:
        self.customers.append({"email": email})
        self._failing()
        return self._customer_ids.setdefault(email.strip(), f"ctm_fake_{len(self._customer_ids) + 1}")


#: Paddle's documented answers (developer.paddle.com/api-reference: create-transaction,
#: create-customer-portal-session, cancel-subscription, update-subscription, and since 0.12
#: preview-subscription, list-customers, create-customer; errors), trimmed to what the
#: kit's client reads — kastlan's ``test_paddle_client.py``. Not recordings: swapped for
#: recorded sandbox answers after Marcel's sandbox run (§15.6, P5).
PADDLE_API_EXAMPLES: Mapping[str, dict[str, Any]] = types.MappingProxyType(
    {
        "create_transaction": {
            "data": {
                "id": "txn_01hv8wptq8987qeep44cyrewp9",
                "status": "ready",
                "customer_id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4",
                "currency_code": "CHF",
                "collection_mode": "automatic",
                "custom_data": {"payer_ref": "company:7", "app": "kastlan"},
                "items": [{"price": {"id": "pri_01gsz8x8sawmvhz1pv30nge1ke"}, "quantity": 1}],
                "checkout": {"url": "https://pay.example.com/?_ptxn=txn_01hv8wptq8987qeep44cyrewp9"},
            },
            "meta": {"request_id": "4e3b9d3a-0a6f-4a50-a5a0-9e4e4c6a7c3e"},
        },
        "create_portal_session": {
            "data": {
                "id": "cpls_01h4ge9r64c22exjsx0fy8b48b",
                "customer_id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4",
                "urls": {
                    "general": {
                        "overview": "https://customer-portal.paddle.com/cpl_01j7zbyqs3vah3aafp4jf62qaw?token=pga_x"
                    },
                    "subscriptions": [
                        {
                            "id": "sub_01h04vsc0qhwtsbsxh3422wjs4",
                            "cancel_subscription": (
                                "https://customer-portal.paddle.com/cpl_01j7zbyqs3vah3aafp4jf62qaw"
                                "/subscriptions/sub_01h04vsc0qhwtsbsxh3422wjs4/cancel?token=pga_x"
                            ),
                            "update_subscription_payment_method": (
                                "https://customer-portal.paddle.com/cpl_01j7zbyqs3vah3aafp4jf62qaw"
                                "/subscriptions/sub_01h04vsc0qhwtsbsxh3422wjs4/update-payment-method?token=pga_x"
                            ),
                        }
                    ],
                },
                "created_at": "2026-10-09T10:00:00.000Z",
            },
            "meta": {"request_id": "9b0c5c1e-6a57-4b0e-9d0a-1f8d0b1c2a3d"},
        },
        "cancel_subscription": {
            "data": {
                "id": "sub_01h04vsc0qhwtsbsxh3422wjs4",
                "status": "active",
                "scheduled_change": {"action": "cancel", "effective_at": "2027-10-08T09:00:00Z", "resume_at": None},
            },
            "meta": {"request_id": "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"},
        },
        "update_subscription": {
            "data": {"id": "sub_01h04vsc0qhwtsbsxh3422wjs4", "status": "active", "scheduled_change": None},
            "meta": {"request_id": "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a"},
        },
        "preview_subscription_update": {
            "data": {
                "id": "sub_01h04vsc0qhwtsbsxh3422wjs4",
                "status": "active",
                "customer_id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4",
                "currency_code": "CHF",
                "next_billed_at": "2027-10-08T09:00:00Z",
                "items": [{"status": "active", "quantity": 1, "price": {"id": "pri_01gsz8x8sawmvhz1pv30nge1ke"}}],
                "update_summary": {
                    "credit": {"amount": "3900", "currency_code": "CHF"},
                    "charge": {"amount": "8000", "currency_code": "CHF"},
                    "result": {"action": "charge", "amount": "4100", "currency_code": "CHF"},
                },
                "immediate_transaction": {"details": {"totals": {"total": "4100", "grand_total": "4100"}}},
                "next_transaction": {
                    "billing_period": {"starts_at": "2027-10-08T09:00:00Z", "ends_at": "2028-10-08T09:00:00Z"},
                    "details": {"totals": {"total": "12000", "credit": "0", "grand_total": "12000"}},
                },
            },
            "meta": {"request_id": "3e4f5a6b-7c8d-4e9f-0a1b-2c3d4e5f6a7b"},
        },
        "list_customers": {
            "data": [
                {"id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4", "email": "payer@example.com", "status": "active"},
            ],
            "meta": {
                "request_id": "5a6b7c8d-9e0f-4a1b-2c3d-4e5f6a7b8c9d",
                "pagination": {"per_page": 50, "next": None, "has_more": False, "estimated_total": 1},
            },
        },
        "create_customer": {
            "data": {"id": "ctm_01hv6y1jedq4p1n0yqn5ba3ky4", "email": "payer@example.com", "status": "active"},
            "meta": {"request_id": "6b7c8d9e-0f1a-4b2c-3d4e-5f6a7b8c9d0e"},
        },
        "error": {
            "error": {
                "type": "request_error",
                "code": "not_found",
                "detail": "Entity ctm_x not found",
                "documentation_url": "https://developer.paddle.com/v1/errors/shared/not_found",
            },
            "meta": {"request_id": "77aa0b1c-2d3e-4f5a-6b7c-8d9e0f1a2b3c"},
        },
    }
)


class PaddleApiFake:
    """Paddle's API as an ``httpx.MockTransport`` handler, for testing a
    :class:`~eifi1_server_kit.billing.PaddleClient` without Paddle::

        paddle = PaddleApiFake()
        client = PaddleClient("pdl_sdbx_apikey_test", environment="sandbox",
                              client=httpx.AsyncClient(transport=paddle.transport))
        await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1"})
        assert paddle.sent["items"] == [{"price_id": "pri_1", "quantity": 1}]

    It answers :data:`PADDLE_API_EXAMPLES`: ``POST /transactions`` 201 — its
    ``checkout.url`` the request's ``checkout.url`` (else ``https://pay.example.com/``) with
    ``?_ptxn=<txn>``, its ``custom_data`` and ``customer_id`` the request's;
    ``POST /customers/{ctm}/portal-sessions`` 201; ``POST /subscriptions/{sub}/cancel``
    200; ``PATCH /subscriptions/{sub}`` 200; ``PATCH /subscriptions/{sub}/preview`` 200 —
    :attr:`preview`'s ``data`` when set, else the example (a charge); ``GET
    /customers?email=`` 200 with the customer :attr:`customers` has for the address, or
    none; ``POST /customers`` 201 creating ``ctm_fake_<n>`` into :attr:`customers`, or
    409 ``customer_already_exists`` naming the one it has; anything else 404
    ``not_found``. Every request is kept in :attr:`requests`. :meth:`fail_with` and
    :meth:`unreachable` make every later request fail.
    """

    def __init__(self) -> None:
        #: Every request received, in order.
        self.requests: list[httpx.Request] = []
        #: Paddle's customers, e-mail address → ``ctm_…``; seed it for an existing one.
        self.customers: dict[str, str] = {}
        #: A preview's ``data`` in place of the example's (a credit, a field missing).
        self.preview: dict[str, Any] | None = None
        self._failure: tuple[int, str | None] | None = None
        self._unreachable = False

    @property
    def transport(self) -> httpx.MockTransport:
        """The transport for ``httpx.AsyncClient(transport=…)``."""
        import httpx

        return httpx.MockTransport(self)

    @property
    def sent(self) -> dict[str, Any]:
        """The last request's JSON body; ``{}`` for one without a body."""
        body: dict[str, Any] = json.loads(self.requests[-1].content or b"{}")
        return body

    def fail_with(self, status: int, code: str | None = None) -> None:
        """Answer every later request with ``status`` and Paddle's error shape, ``code`` as
        its ``error.code`` (none: a body without the error, as a proxy's page)."""
        self._failure = (status, code)

    def unreachable(self) -> None:
        """Answer every later request with no answer: a connection error."""
        self._unreachable = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import httpx

        self.requests.append(request)
        if self._unreachable:
            raise httpx.ConnectError("Paddle is unreachable (PaddleApiFake)", request=request)
        if self._failure is not None:
            status, code = self._failure
            error = copy.deepcopy(PADDLE_API_EXAMPLES["error"])
            if code is None:
                return httpx.Response(status, text="<html>Bad gateway</html>", headers={"content-type": "text/html"})
            error["error"]["code"] = code
            return httpx.Response(status, json=error)
        status, body = self._answer(request)
        return httpx.Response(status, json=body)

    def _answer(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        path = request.url.path.strip("/").split("/")
        if request.method == "POST" and path == ["transactions"]:
            body = copy.deepcopy(PADDLE_API_EXAMPLES["create_transaction"])
            sent = self.sent
            transaction = body["data"]
            page = (sent.get("checkout") or {}).get("url") or "https://pay.example.com/"
            transaction["checkout"] = {"url": f"{page}?_ptxn={transaction['id']}"}
            transaction["custom_data"] = sent.get("custom_data")
            transaction["customer_id"] = sent.get("customer_id")
            return 201, body
        if request.method == "POST" and len(path) == 3 and path[0] == "customers" and path[2] == "portal-sessions":
            return 201, copy.deepcopy(PADDLE_API_EXAMPLES["create_portal_session"])
        if request.method == "POST" and len(path) == 3 and path[0] == "subscriptions" and path[2] == "cancel":
            return 200, copy.deepcopy(PADDLE_API_EXAMPLES["cancel_subscription"])
        if request.method == "PATCH" and len(path) == 2 and path[0] == "subscriptions":
            return 200, copy.deepcopy(PADDLE_API_EXAMPLES["update_subscription"])
        if request.method == "PATCH" and len(path) == 3 and path[0] == "subscriptions" and path[2] == "preview":
            body = copy.deepcopy(PADDLE_API_EXAMPLES["preview_subscription_update"])
            if self.preview is not None:
                body["data"] = copy.deepcopy(self.preview)
            return 200, body
        if path == ["customers"]:
            return self._customer(request)
        return 404, copy.deepcopy(PADDLE_API_EXAMPLES["error"])

    def _customer(self, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        """``GET /customers?email=`` and ``POST /customers``."""
        if request.method == "GET":
            body = copy.deepcopy(PADDLE_API_EXAMPLES["list_customers"])
            email = request.url.params.get("email", "")
            known = self.customers.get(email)
            body["data"] = [] if known is None else [{"id": known, "email": email, "status": "active"}]
            return 200, body
        email = str(self.sent.get("email", ""))
        if email in self.customers:
            error = copy.deepcopy(PADDLE_API_EXAMPLES["error"])
            error["error"]["code"] = "customer_already_exists"
            error["error"]["detail"] = f"customer email conflicts with customer of id {self.customers[email]}"
            return 409, error
        customer_id = self.customers[email] = f"ctm_fake_{len(self.customers) + 1}"
        body = copy.deepcopy(PADDLE_API_EXAMPLES["create_customer"])
        body["data"].update(id=customer_id, email=email)
        return 201, body
