"""The test helpers the apps import (billing contract §14.9): Paddle's notifications through
the kit's own mapper and signature check, the local CLI's POST, and the two fakes."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from eifi1_server_kit.billing import (
    BillingError,
    BillingErrorCode,
    BillingSettings,
    EventKind,
    PaddleError,
    SubscriptionStatus,
    checkout_custom_data,
    map_paddle_event,
    parse_webhook_event,
    verify_paddle_signature,
)
from eifi1_server_kit.billing.testing import (
    PADDLE_API_EXAMPLES,
    PADDLE_STEP_PERIODS,
    PADDLE_STEPS,
    FakeBillingProvider,
    PaddleApiFake,
    PaddleStep,
    paddle_event,
    post_signed,
    sign_paddle,
    signed_paddle_event,
)

AT = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
SECRET = "pdl_ntfset_01gkpjp8bkm3tm53kdgkx6sms7_example"
DAY = timedelta(days=1)


def _mapped(step: PaddleStep, **fields: Any) -> Any:
    event = map_paddle_event(json.dumps(paddle_event(step, **fields)).encode())
    assert event is not None
    return event


# --- the events ----------------------------------------------------------------------------


def test_the_steps_are_a_subscriptions_life() -> None:
    """keksdose's and Kurvenschmiede's five, and the scheduled cancellation none of them had."""
    assert dict(PADDLE_STEPS) == {
        "trial": ("subscription.trialing", "trialing"),
        "checkout": ("subscription.created", "active"),
        "renewal": ("subscription.updated", "active"),
        "cancel_scheduled": ("subscription.updated", "active"),
        "payment_failed": ("subscription.past_due", "past_due"),
        "cancelled": ("subscription.canceled", "canceled"),
    }
    assert {step: period.days for step, period in PADDLE_STEP_PERIODS.items()} == {
        "trial": 14,
        "checkout": 365,
        "renewal": 730,
        "cancel_scheduled": 365,
        "payment_failed": 365,
    }


@pytest.mark.parametrize(
    ("step", "kind", "status", "cancelling"),
    [
        ("trial", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.TRIALING, False),
        ("checkout", EventKind.SUBSCRIPTION_STARTED, SubscriptionStatus.ACTIVE, False),
        ("renewal", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.ACTIVE, False),
        ("cancel_scheduled", EventKind.SUBSCRIPTION_CANCELED, SubscriptionStatus.ACTIVE, True),
        ("payment_failed", EventKind.PAYMENT_FAILED, SubscriptionStatus.PAST_DUE, False),
        ("cancelled", EventKind.SUBSCRIPTION_EXPIRED, SubscriptionStatus.CANCELED, False),
    ],
)
def test_each_step_maps_as_paddle_would_send_it(
    step: PaddleStep, kind: EventKind, status: SubscriptionStatus, cancelling: bool
) -> None:
    event = _mapped(step, payer_ref="user:42", price_id="pri_pro_y", app="keksdose", at=AT)
    assert (event.kind, event.status, event.cancel_at_period_end) == (kind, status, cancelling)
    assert (event.payer_ref, event.app, event.plan_price_id) == ("user:42", "keksdose", "pri_pro_y")
    assert (event.customer_ref, event.subscription_ref, event.occurred_at) == ("ctm_local", "sub_local", AT)
    expected_end = AT if step == "cancelled" else AT + PADDLE_STEP_PERIODS[step]
    assert event.current_period_end == expected_end
    assert event.event_id == f"evt_local_{step}_{int(AT.timestamp() * 1000)}"


def test_the_cancelled_step_has_no_period_left() -> None:
    """As Paddle sends it: ``current_billing_period`` null and ``canceled_at`` (kastlan's
    kept the period)."""
    data = paddle_event("cancelled", payer_ref="company:7", price_id="pri_1", at=AT, period_end=AT + DAY)["data"]
    assert data["current_billing_period"] is None and data["canceled_at"] == AT.isoformat()
    assert data["custom_data"] == checkout_custom_data("company:7")
    assert data["items"] == [{"status": "active", "quantity": 1, "recurring": True, "price": {"id": "pri_1"}}]
    assert data["currency_code"] == "CHF"


def test_the_fields_can_be_the_tests() -> None:
    ends = AT + 30 * DAY
    body = paddle_event(
        "cancel_scheduled",
        payer_ref="user:1",
        price_id="pri_1",
        at=AT,
        subscription_id="sub_2",
        customer_id="ctm_2",
        event_id="evt_1",
        period_end=ends,
    )
    assert body["event_id"] == "evt_1" and body["notification_id"].startswith("ntf_local_cancel_scheduled_")
    assert body["data"]["scheduled_change"] == {"action": "cancel", "effective_at": ends.isoformat(), "resume_at": None}
    assert (body["data"]["id"], body["data"]["customer_id"]) == ("sub_2", "ctm_2")
    assert "app" not in body["data"]["custom_data"]
    # Now by default.
    fresh = paddle_event("renewal", payer_ref="user:1", price_id="pri_1")
    assert abs(datetime.fromisoformat(fresh["occurred_at"]) - datetime.now(UTC)) < timedelta(minutes=1)


# --- the signature -------------------------------------------------------------------------


def test_the_signature_is_paddles() -> None:
    """The kit's own check accepts it, which checks the docs' recipe."""
    raw = b'{"event_id":"evt_1"}'
    headers = sign_paddle(raw, SECRET, now=1_791_460_800)
    assert headers["Content-Type"] == "application/json"
    assert headers["Paddle-Signature"].startswith("ts=1791460800;h1=")
    verify_paddle_signature(raw, headers["Paddle-Signature"], SECRET, now=1_791_460_800)
    with pytest.raises(BillingError, match="no h1 matches"):
        verify_paddle_signature(raw + b" ", headers["Paddle-Signature"], SECRET, now=1_791_460_800)
    now = sign_paddle(raw, SECRET)["Paddle-Signature"]
    verify_paddle_signature(raw, now, SECRET, now=time.time())


def test_a_signed_event_passes_the_apps_webhook_checks() -> None:
    settings = BillingSettings(billing_webhook_secret=SECRET, billing_app="keksdose", billing_signature_tolerance=60)
    raw, headers = signed_paddle_event(
        "checkout", secret=SECRET, payer_ref="user:42", price_id="pri_pro_y", app="keksdose", at=AT
    )
    settings.verify_billing_webhook("paddle", raw, headers)
    event = parse_webhook_event("paddle", raw, app=settings.billing_app)
    assert event is not None and (event.kind, event.payer_ref) == (EventKind.SUBSCRIPTION_STARTED, "user:42")
    # Another app's checkout, on the same account: dropped.
    raw, headers = signed_paddle_event(
        "checkout", secret=SECRET, payer_ref="user:42", price_id="p", app="kurvenschmiede"
    )
    settings.verify_billing_webhook("paddle", raw, headers)
    assert parse_webhook_event("paddle", raw, app=settings.billing_app) is None


# --- the local POST ------------------------------------------------------------------------


class _Webhook(BaseHTTPRequestHandler):
    """The app's webhook, as far as the CLI sees it: 200, or 400 for a bad signature."""

    received: list[tuple[str, bytes, dict[str, str]]] = []  # noqa: RUF012 - one per test run, reset below

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append((self.path, body, dict(self.headers)))
        status = 400 if self.path.endswith("/bad") else 200
        answer = json.dumps({"detail": "Invalid webhook signature"} if status == 400 else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet."""


@pytest.fixture
def local_webhook() -> Iterator[str]:
    _Webhook.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_cli_posts_to_its_own_instance(local_webhook: str) -> None:
    raw, headers = signed_paddle_event("renewal", secret=SECRET, payer_ref="user:1", price_id="pri_1")
    assert post_signed(f"{local_webhook}/api/v1/webhooks/paddle", raw, headers, timeout=5) == (200, "{}")
    ((path, body, sent),) = _Webhook.received
    assert (path, body) == ("/api/v1/webhooks/paddle", raw)
    assert sent["Paddle-Signature"] == headers["Paddle-Signature"]
    # A refusal is answered, not raised.
    assert post_signed(f"{local_webhook}/bad", raw, headers) == (400, '{"detail": "Invalid webhook signature"}')


@pytest.mark.parametrize(
    "url",
    [
        "https://keksdose.app/api/v1/webhooks/paddle",
        "http://10.0.0.5:8000/api/v1/webhooks/paddle",
        "file:///etc/passwd",
        "http://localhost.example.com/",
    ],
)
def test_the_cli_never_posts_to_a_deployed_instance(url: str) -> None:
    with pytest.raises(ValueError, match="local instance only"):
        post_signed(url, b"{}", {})


# --- the provider's fake -------------------------------------------------------------------


async def test_the_fake_provider_records_every_call() -> None:
    provider = FakeBillingProvider(
        checkout_url="https://pay.example/?_ptxn=txn_1", portal_url="https://portal.example/p"
    )
    custom = checkout_custom_data("user:1", app="keksdose")
    assert (
        await provider.checkout_url(price_id="pri_1", custom_data=custom, locale="de")
        == "https://pay.example/?_ptxn=txn_1"
    )
    assert await provider.portal_url(customer_id="ctm_1", subscription_id="sub_1", target="cancel") == (
        "https://portal.example/p"
    )
    await provider.cancel(subscription_id="sub_1", immediately=True)
    await provider.remove_scheduled_cancel(subscription_id="sub_1")
    assert provider.checkouts == [{"price_id": "pri_1", "custom_data": custom, "customer_id": None, "locale": "de"}]
    assert provider.portals == [{"customer_id": "ctm_1", "subscription_id": "sub_1", "target": "cancel"}]
    assert provider.cancels == [{"subscription_id": "sub_1", "immediately": True}]
    assert provider.resumes == [{"subscription_id": "sub_1"}]


async def test_the_fake_provider_fails_as_paddle_does() -> None:
    provider = FakeBillingProvider(fail=True)
    calls = (
        provider.checkout_url(price_id="pri_1", custom_data={}),
        provider.portal_url(customer_id="ctm_1"),
        provider.cancel(subscription_id="sub_1"),
        provider.remove_scheduled_cancel(subscription_id="sub_1"),
    )
    for call in calls:
        with pytest.raises(PaddleError) as failed:
            await call
        assert (failed.value.status_code, failed.value.code) == (502, BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE)
    assert (len(provider.checkouts), len(provider.portals), len(provider.cancels), len(provider.resumes)) == (
        1,
        1,
        1,
        1,
    )


async def test_the_fake_provider_has_no_portal_before_the_provider() -> None:
    provider = FakeBillingProvider()
    with pytest.raises(BillingError) as refused:
        await provider.portal_url(customer_id=None)
    assert refused.value.code is BillingErrorCode.BILLING_NOT_AT_PROVIDER and provider.portals == []


# --- the API's fake ------------------------------------------------------------------------


async def test_the_api_fake_answers_paddles_examples() -> None:
    paddle = PaddleApiFake()
    async with httpx.AsyncClient(transport=paddle.transport, base_url="https://sandbox-api.paddle.com") as http:
        created = await http.post("/transactions", json={"items": [], "custom_data": {"payer_ref": "user:1"}})
        assert created.status_code == 201
        transaction = created.json()["data"]
        assert transaction["checkout"]["url"] == f"https://pay.example.com/?_ptxn={transaction['id']}"
        assert (transaction["custom_data"], transaction["customer_id"]) == ({"payer_ref": "user:1"}, None)
        portal = await http.post("/customers/ctm_1/portal-sessions", json={})
        assert portal.json() == PADDLE_API_EXAMPLES["create_portal_session"]
        assert (await http.post("/subscriptions/sub_1/cancel", json={})).status_code == 200
        assert (await http.patch("/subscriptions/sub_1", json={"scheduled_change": None})).status_code == 200
        unknown = await http.get("/subscriptions/sub_1")
        assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
        assert paddle.sent == {}  # the GET had no body
    assert [request.method for request in paddle.requests] == ["POST", "POST", "POST", "PATCH", "GET"]


async def test_the_api_fake_fails_on_demand() -> None:
    paddle = PaddleApiFake()
    async with httpx.AsyncClient(transport=paddle.transport) as http:
        paddle.fail_with(409, "subscription_locked_pending_changes")
        refused = await http.post("https://sandbox-api.paddle.com/subscriptions/sub_1/cancel", json={})
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "subscription_locked_pending_changes")
        paddle.fail_with(502)
        proxy = await http.post("https://sandbox-api.paddle.com/transactions", json={})
        assert (proxy.status_code, proxy.headers["content-type"]) == (502, "text/html")
        paddle.unreachable()
        with pytest.raises(httpx.ConnectError):
            await http.post("https://sandbox-api.paddle.com/transactions", json={})
    assert len(paddle.requests) == 3
