"""The Paddle client (billing contract §14.2), lifted from kastlan's
``tests/unit/test_paddle_client.py`` (c0b1a24), against Paddle's documented answers
(:data:`~eifi1_server_kit.billing.testing.PADDLE_API_EXAMPLES`) — not recordings; they are
swapped for recorded sandbox answers once the sandbox account exists."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

import httpx
import pytest

from eifi1_server_kit.billing import (
    PADDLE_API_BASES,
    PADDLE_API_VERSION,
    PADDLE_TIMEOUT_SECONDS,
    BillingError,
    BillingErrorCode,
    PaddleClient,
    PaddleEnvironment,
    PaddleError,
    paddle_environment_of,
)
from eifi1_server_kit.billing.testing import PADDLE_API_EXAMPLES, PaddleApiFake

KEY = "pdl_sdbx_apikey_01gtgztp8f4kek3yd4g1wrksa3_example"
SANDBOX = "https://sandbox-api.paddle.com"
TXN = PADDLE_API_EXAMPLES["create_transaction"]["data"]["id"]


def _client(paddle: PaddleApiFake | None = None, **kwargs: Any) -> tuple[PaddleClient, PaddleApiFake]:
    fake = paddle or PaddleApiFake()
    http = httpx.AsyncClient(transport=fake.transport)
    return PaddleClient(KEY, environment="sandbox", client=http, **kwargs), fake


def _answering(status: int, body: dict[str, Any]) -> PaddleClient:
    """A client whose Paddle answers ``body`` to everything."""

    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return PaddleClient(KEY, environment="sandbox", client=httpx.AsyncClient(transport=httpx.MockTransport(answer)))


# --- the environment -----------------------------------------------------------------------


def test_the_api_is_the_kits_constant() -> None:
    """Decision 24: no base URL to mistype."""
    assert PADDLE_API_BASES == {
        PaddleEnvironment.SANDBOX: "https://sandbox-api.paddle.com",
        PaddleEnvironment.LIVE: "https://api.paddle.com",
    }
    assert (PADDLE_API_VERSION, PADDLE_TIMEOUT_SECONDS) == ("1", 15.0)


def test_the_keys_prefix_names_its_environment() -> None:
    assert paddle_environment_of(KEY) is PaddleEnvironment.SANDBOX
    assert paddle_environment_of(" pdl_live_apikey_01gtgztp8f ") is PaddleEnvironment.LIVE
    assert paddle_environment_of("a" * 50) is None  # a legacy key, from before 2025-05-06
    assert paddle_environment_of("pdl_test") is None  # kastlan's test key


def test_a_key_of_the_other_environment_is_refused() -> None:
    with pytest.raises(ValueError, match="a sandbox key can't call Paddle's live API"):
        PaddleClient(KEY, environment="live")
    with pytest.raises(ValueError, match="needs an API key"):
        PaddleClient(" ", environment="sandbox")
    with pytest.raises(ValueError, match="app tag"):
        PaddleClient(KEY, environment="sandbox", app="")
    with pytest.raises(ValueError):
        PaddleClient(KEY, environment="staging")
    assert PaddleClient("pdl_test", environment="sandbox").environment is PaddleEnvironment.SANDBOX


def test_without_httpx_the_client_says_which_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import httpx`` fail as if it were not installed."""
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ImportError, match=r"eifi1-server-kit\[billing\]"):
        PaddleClient(KEY, environment="sandbox")


def test_importing_billing_needs_no_httpx() -> None:
    """§14.1: httpx is imported lazily, by the client and the API fake only."""
    code = (
        "import sys; import eifi1_server_kit.billing, eifi1_server_kit.billing.testing; "
        "assert 'httpx' not in sys.modules, 'httpx was imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- the checkout --------------------------------------------------------------------------


async def test_a_checkout_is_a_transaction_for_one_price_with_the_payers_data() -> None:
    client, paddle = _client(app="kastlan")
    url = await client.checkout_url(
        price_id="pri_01gsz8x8sawmvhz1pv30nge1ke", custom_data={"payer_ref": "company:7"}, customer_id="ctm_1"
    )
    assert url == f"https://pay.example.com/?_ptxn={TXN}"
    (request,) = paddle.requests
    assert (request.method, str(request.url)) == ("POST", f"{SANDBOX}/transactions")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Paddle-Version"] == "1"
    assert request.extensions["timeout"]["read"] == PADDLE_TIMEOUT_SECONDS
    assert paddle.sent == {
        "items": [{"price_id": "pri_01gsz8x8sawmvhz1pv30nge1ke", "quantity": 1}],
        "custom_data": {"payer_ref": "company:7", "app": "kastlan"},
        "customer_id": "ctm_1",
    }


async def test_a_first_checkout_names_no_customer_and_no_page() -> None:
    client, paddle = _client()
    await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1"}, customer_id=None)
    assert paddle.sent == {"items": [{"price_id": "pri_1", "quantity": 1}], "custom_data": {"payer_ref": "user:1"}}


async def test_the_apps_tag_wins_over_the_callers() -> None:
    """§14.3: the client adds its app itself, so an app can't forget it, or send another."""
    client, paddle = _client(app="keksdose")
    await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1", "app": "kurvenschmiede"})
    assert paddle.sent["custom_data"] == {"payer_ref": "user:1", "app": "keksdose"}


async def test_the_checkout_opens_on_the_apps_pay_page() -> None:
    """§14.4: the pay page is the transaction's ``checkout.url``; Paddle adds ``_ptxn``."""
    client, paddle = _client(checkout_page_url="https://pay.keksdose.example/")
    url = await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1"}, locale="de")
    assert paddle.sent["checkout"] == {"url": "https://pay.keksdose.example/"}
    assert url == f"https://pay.keksdose.example/?_ptxn={TXN}"  # the transaction carries no locale


async def test_with_a_hosted_checkout_the_transaction_opens_on_paddles_page() -> None:
    client, paddle = _client(
        hosted_checkout_url="https://pay.paddle.io/checkout/hsc_01abc", checkout_page_url="https://pay.kastlan.example/"
    )
    url = await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "company:7"}, locale="de")
    assert url == f"https://pay.paddle.io/checkout/hsc_01abc?transaction_id={TXN}&locale=de"
    assert "checkout" not in paddle.sent  # the hosted checkout wins over the pay page
    plain, _ = _client(hosted_checkout_url="https://pay.paddle.io/checkout/hsc_01abc?theme=dark")
    assert await plain.checkout_url(price_id="pri_1", custom_data={}) == (
        f"https://pay.paddle.io/checkout/hsc_01abc?theme=dark&transaction_id={TXN}"
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"data": {"checkout": {"url": "https://pay.example.com/?_ptxn=txn_1"}}}, "data.id"),
        ({"data": {"id": "txn_1", "checkout": None}}, "data.checkout.url"),
        ({"data": {"id": "txn_1", "checkout": {"url": ""}}}, "data.checkout.url"),
        ({"meta": {"request_id": "r-1"}}, "without data "),
        ({"data": ["txn_1"]}, "without data "),
    ],
)
async def test_a_2xx_without_the_fields_read_is_502(
    body: dict[str, Any], field: str, caplog: pytest.LogCaptureFixture
) -> None:
    """kastlan read the answer raw, so a missing field was a KeyError: a 500."""
    with pytest.raises(PaddleError) as failed:
        await _answering(201, body).checkout_url(price_id="pri_1", custom_data={})
    assert (failed.value.status_code, failed.value.code) == (502, BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE)
    assert failed.value.status == 201
    assert field in caplog.records[-1].getMessage() + " "


async def test_a_hosted_checkout_needs_the_transactions_id_only() -> None:
    client = PaddleClient(
        KEY,
        environment="sandbox",
        hosted_checkout_url="https://pay.paddle.io/checkout/hsc_01abc",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(201, json={"data": {"id": "txn_9"}}))
        ),
    )
    assert await client.checkout_url(price_id="pri_1", custom_data={}) == (
        "https://pay.paddle.io/checkout/hsc_01abc?transaction_id=txn_9"
    )


# --- the portal ----------------------------------------------------------------------------

PORTAL = PADDLE_API_EXAMPLES["create_portal_session"]["data"]["urls"]
SUB = PORTAL["subscriptions"][0]["id"]


async def test_the_portal_session_answers_the_overview_link() -> None:
    client, paddle = _client()
    url = await client.portal_url(customer_id="ctm_1", subscription_id=SUB)
    assert url == PORTAL["general"]["overview"]
    assert str(paddle.requests[-1].url) == f"{SANDBOX}/customers/ctm_1/portal-sessions"
    assert paddle.sent == {"subscription_ids": [SUB]}
    await client.portal_url(customer_id="ctm_1")
    assert paddle.sent == {}


async def test_the_portals_targets_open_the_subscriptions_links() -> None:
    """§14.5: "Cancel subscription" opens its cancel link, the payment-failed banner the
    payment method's."""
    client, _ = _client()
    assert (
        await client.portal_url(customer_id="ctm_1", subscription_id=SUB, target="cancel")
        == (PORTAL["subscriptions"][0]["cancel_subscription"])
    )
    assert (
        await client.portal_url(customer_id="ctm_1", subscription_id=SUB, target="payment_method")
        == (PORTAL["subscriptions"][0]["update_subscription_payment_method"])
    )
    # Without a subscription named, the session's first one.
    assert (
        await client.portal_url(customer_id="ctm_1", target="cancel")
        == PORTAL["subscriptions"][0]["cancel_subscription"]
    )
    # Another subscription than the session's: the overview.
    assert (
        await client.portal_url(customer_id="ctm_1", subscription_id="sub_other", target="cancel")
        == (PORTAL["general"]["overview"])
    )


@pytest.mark.parametrize(
    "subscriptions",
    [
        [],  # Paddle gives none for a paused or cancelled subscription
        [{"id": SUB, "cancel_subscription": None}],
        ["sub_1"],
        None,
    ],
)
async def test_a_target_paddle_gives_no_link_for_opens_the_overview(subscriptions: object) -> None:
    body = {"data": {"urls": {"general": {"overview": "https://portal.example/o"}, "subscriptions": subscriptions}}}
    url = await _answering(201, body).portal_url(customer_id="ctm_1", subscription_id=SUB, target="cancel")
    assert url == "https://portal.example/o"


@pytest.mark.parametrize(
    "data", [{}, {"urls": []}, {"urls": {"general": None}}, {"urls": {"general": {"overview": 1}}}]
)
async def test_a_portal_session_without_the_overview_is_502(data: dict[str, Any]) -> None:
    with pytest.raises(PaddleError, match="Try again"):
        await _answering(201, {"data": data}).portal_url(customer_id="ctm_1")


async def test_no_customer_is_not_at_the_provider_without_a_request() -> None:
    """§14.5: 409 ``billing_not_at_provider``, for a stale page."""
    client, paddle = _client()
    for customer in (None, ""):
        with pytest.raises(BillingError) as refused:
            await client.portal_url(customer_id=customer)
        assert (refused.value.status_code, refused.value.code) == (409, BillingErrorCode.BILLING_NOT_AT_PROVIDER)
    with pytest.raises(ValueError, match="a portal target is one of"):
        await client.portal_url(customer_id="ctm_1", target="invoices")  # type: ignore[arg-type]
    assert paddle.requests == []


# --- cancelling, and undoing it ------------------------------------------------------------


async def test_cancelling_takes_effect_at_the_periods_end_by_default() -> None:
    client, paddle = _client()
    await client.cancel(subscription_id="sub_1")
    assert (paddle.requests[-1].method, str(paddle.requests[-1].url)) == (
        "POST",
        f"{SANDBOX}/subscriptions/sub_1/cancel",
    )
    assert paddle.sent == {"effective_from": "next_billing_period"}
    await client.cancel(subscription_id="sub_1", immediately=True)
    assert paddle.sent == {"effective_from": "immediately"}


async def test_undoing_a_cancellation_removes_the_scheduled_change() -> None:
    """§14.8: Paddle's ``PATCH /subscriptions/{id} {"scheduled_change": null}``."""
    client, paddle = _client()
    await client.remove_scheduled_cancel(subscription_id="sub_1")
    request = paddle.requests[-1]
    assert (request.method, str(request.url)) == ("PATCH", f"{SANDBOX}/subscriptions/sub_1")
    assert json.loads(request.content) == {"scheduled_change": None}


async def test_an_id_stays_one_path_segment() -> None:
    client, paddle = _client()
    with pytest.raises(PaddleError):  # the fake knows no such subscription
        await client.cancel(subscription_id="sub_1/../../transactions")
    assert paddle.requests[-1].url.raw_path == b"/subscriptions/sub_1%2F..%2F..%2Ftransactions/cancel"


# --- failures ------------------------------------------------------------------------------


async def test_paddles_error_is_carried_without_the_key_or_the_body(caplog: pytest.LogCaptureFixture) -> None:
    client, paddle = _client()
    paddle.fail_with(404, "not_found")
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.billing"), pytest.raises(PaddleError) as failed:
        await client.portal_url(customer_id="ctm_x")
    error = failed.value
    assert (error.status, error.paddle_code, error.paddle_detail) == (404, "not_found", "Entity ctm_x not found")
    assert error.request_id == PADDLE_API_EXAMPLES["error"]["meta"]["request_id"]
    assert (error.status_code, error.code) == (502, BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE)
    assert str(error) == "The payment provider is not available. Try again."  # the kit's words, not Paddle's
    (record,) = caplog.records
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert (
        message == f"Paddle POST /customers/ctm_x/portal-sessions answered 404 not_found (request {error.request_id})"
    )
    assert "pdl_" not in message + str(error) + repr(error) and "Entity" not in message


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_key_is_the_deployments_fault(status: int, caplog: pytest.LogCaptureFixture) -> None:
    """A wrong key, or one without the permission: 503, not "try again", logged at ERROR."""
    client, paddle = _client()
    paddle.fail_with(status, "forbidden")
    with pytest.raises(PaddleError) as failed:
        await client.cancel(subscription_id="sub_1")
    assert (failed.value.status_code, failed.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize("status", [400, 409, 422, 429, 500, 503])
async def test_any_other_refusal_is_try_again(status: int) -> None:
    client, paddle = _client()
    paddle.fail_with(status, "some_code")
    with pytest.raises(PaddleError) as failed:
        await client.checkout_url(price_id="pri_1", custom_data={})
    assert (failed.value.status_code, failed.value.status, failed.value.paddle_code) == (502, status, "some_code")
    assert len(paddle.requests) == 1, "no retry: a transaction is not idempotent"


async def test_a_proxy_error_page_and_no_answer_are_errors_too(caplog: pytest.LogCaptureFixture) -> None:
    client, paddle = _client()
    paddle.fail_with(502)
    with pytest.raises(PaddleError) as failed:
        await client.cancel(subscription_id="sub_1")
    assert (failed.value.status, failed.value.paddle_code, failed.value.request_id) == (502, None, None)

    paddle.unreachable()
    with pytest.raises(PaddleError) as failed:
        await client.checkout_url(price_id="pri_1", custom_data={})
    assert (failed.value.status, failed.value.status_code) == (0, 502)
    assert caplog.records[-1].getMessage() == "Paddle POST /transactions: no answer (ConnectError)"
    assert failed.value.__suppress_context__  # the request, its headers and the key stay out of the trace


async def test_a_json_answer_that_is_no_object_is_a_failure_too() -> None:
    def answer(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=["no"])

    client = PaddleClient(KEY, environment="sandbox", client=httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    with pytest.raises(PaddleError) as failed:
        await client.cancel(subscription_id="sub_1")
    assert (failed.value.status, failed.value.paddle_code) == (500, None)


async def test_without_a_client_each_call_opens_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """As ``ResendClient``: the app passes its one shared client, or none."""
    paddle = PaddleApiFake()
    real = httpx.AsyncClient
    opened: list[dict[str, object]] = []

    def with_fake_transport(**kwargs: object) -> httpx.AsyncClient:
        opened.append(kwargs)
        return real(transport=paddle.transport)

    monkeypatch.setattr(httpx, "AsyncClient", with_fake_transport)
    client = PaddleClient("pdl_live_apikey_example", environment=PaddleEnvironment.LIVE, timeout=3.0)
    await client.cancel(subscription_id="sub_1")
    await client.remove_scheduled_cancel(subscription_id="sub_1")
    assert opened == [{"timeout": 3.0}, {"timeout": 3.0}]
    assert str(paddle.requests[0].url) == "https://api.paddle.com/subscriptions/sub_1/cancel"
