"""The provider's port and what a route checks first (billing contract §14.2, §14.4–§14.6,
§14.8 with §12.37): the deployment's client, the plan sold, a second checkout, the
deletion's cancellation and its undoing, and the way back from a checkout."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from eifi1_server_kit.billing import (
    CHECKOUT_RETURN_PARAM,
    CHECKOUT_RETURN_VALUE,
    BillingError,
    BillingErrorCode,
    BillingProviderClient,
    BillingSettings,
    CheckoutRequest,
    NoProviderClient,
    PaddleClient,
    PaddleEnvironment,
    PlanSpec,
    SubscriptionSource,
    SubscriptionStatus,
    billing_provider_client,
    cancel_for_deletion,
    checkout_return_url,
    plan_catalogue,
    require_new_checkout,
    resume_after_withdrawal,
    sold_plan,
)
from eifi1_server_kit.billing.testing import FakeBillingProvider, PaddleApiFake
from tests.billing._rows import NOW, Row

DAY = timedelta(days=1)
FREE = PlanSpec(code="free", limits={"budgets": 1})
PRO = PlanSpec(code="pro", limits={"budgets": 5}, prices={("CHF", "year"): 3000, ("EUR", "year"): 3000}, sort=1)
LEGACY = PlanSpec(code="legacy", limits={"budgets": 3}, prices={("CHF", "year"): 2000}, sort=2)
PLANS = plan_catalogue([FREE, PRO, LEGACY])


def _checkout(plan: str = "pro", currency: str = "CHF", interval: str = "year") -> CheckoutRequest:
    return CheckoutRequest.model_validate({"plan": plan, "currency": currency, "interval": interval})


# --- the deployment's client ---------------------------------------------------------------


def test_the_fakes_and_the_client_are_the_port() -> None:
    """mypy checks that each is a :class:`BillingProviderClient`."""
    clients: list[BillingProviderClient] = [
        NoProviderClient(),
        FakeBillingProvider(),
        PaddleClient("pdl_sdbx_apikey_test", environment="sandbox"),
    ]
    assert len(clients) == 3


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"billing_provider": "lemonsqueezy", "billing_api_key": "ls_example"},
        {"billing_provider": "paddle"},
        {"billing_provider": "paddle", "billing_api_key": " "},
    ],
)
async def test_without_paddle_and_a_key_there_is_no_client(fields: dict[str, Any]) -> None:
    client = billing_provider_client(BillingSettings(**fields))
    assert isinstance(client, NoProviderClient)
    calls = (
        client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1"}),
        client.portal_url(customer_id="ctm_1"),
        client.cancel(subscription_id="sub_1"),
        client.remove_scheduled_cancel(subscription_id="sub_1"),
    )
    for call in calls:
        with pytest.raises(BillingError, match="No payment provider client") as refused:
            await call
        assert (refused.value.status_code, refused.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)


async def test_an_environment_nobody_can_name_is_no_client_that_says_so() -> None:
    """The factory never raises for the settings, so a deletion's cancellation goes on."""
    client = billing_provider_client(BillingSettings(billing_provider="paddle", billing_api_key="legacy_key"))
    assert isinstance(client, NoProviderClient)
    with pytest.raises(BillingError, match="Set billing_environment"):
        await client.cancel(subscription_id="sub_1")
    assert await cancel_for_deletion(Row(provider_subscription_id="sub_1"), client) == "failed"


async def test_paddle_with_a_key_is_the_kits_client_on_the_settings() -> None:
    paddle = PaddleApiFake()
    settings = BillingSettings(
        billing_enabled=True,
        billing_provider="paddle",
        billing_api_key="pdl_sdbx_apikey_example",
        billing_webhook_secret="pdl_ntfset_example",
        billing_app="garden",
        billing_checkout_page_url="https://pay.garden.example/",
    )
    client = billing_provider_client(settings, client=httpx.AsyncClient(transport=paddle.transport))
    assert isinstance(client, PaddleClient) and client.environment is PaddleEnvironment.SANDBOX
    url = await client.checkout_url(price_id="pri_1", custom_data={"payer_ref": "user:1"})
    assert url.startswith("https://pay.garden.example/?_ptxn=txn_")
    assert str(paddle.requests[-1].url) == "https://sandbox-api.paddle.com/transactions"
    assert paddle.sent["custom_data"] == {"payer_ref": "user:1", "app": "garden"}
    assert paddle.sent["checkout"] == {"url": "https://pay.garden.example/"}


def test_a_hosted_checkout_reaches_the_client() -> None:
    settings = BillingSettings(
        billing_provider="paddle",
        billing_api_key="pdl_live_apikey_example",
        billing_hosted_checkout_url="https://pay.paddle.io/checkout/hsc_01abc",
    )
    client = billing_provider_client(settings)
    assert isinstance(client, PaddleClient) and client.environment is PaddleEnvironment.LIVE


# --- the plan sold -------------------------------------------------------------------------


def test_the_plan_a_checkout_asks_for() -> None:
    assert sold_plan(PLANS, _checkout("PRO", "eur")) is PRO
    assert sold_plan(PLANS, _checkout("legacy"), sold=None) is LEGACY
    assert sold_plan(PLANS, _checkout("pro"), sold=("PRO", "standard")) is PRO


@pytest.mark.parametrize(
    ("request_", "sold"),
    [
        (_checkout("team"), None),  # not in the catalogue
        (_checkout("free"), None),  # free: no price at all
        (_checkout("legacy", "EUR"), None),  # not in that currency
        (_checkout("pro", "CHF", "month"), None),  # not per month
        (_checkout("legacy"), ("pro",)),  # kept for its rows, sold no more (keksdose's SOLD)
    ],
)
def test_a_plan_not_sold_is_422(request_: CheckoutRequest, sold: tuple[str, ...] | None) -> None:
    """kastlan answered 503; the contract and keksdose and Kurvenschmiede 422."""
    with pytest.raises(BillingError, match="is not sold in") as refused:
        sold_plan(PLANS, request_, sold=sold)
    assert (refused.value.status_code, refused.value.code) == (422, BillingErrorCode.BILLING_PLAN_NOT_SOLD)


# --- a second checkout (§14.6) -------------------------------------------------------------


def _subscribed(**fields: Any) -> Row:
    base: dict[str, Any] = {
        "plan_code": "pro",
        "status": SubscriptionStatus.ACTIVE,
        "source": SubscriptionSource.PROVIDER,
        "provider_customer_id": "ctm_1",
        "provider_subscription_id": "sub_1",
        "current_period_end": NOW + 300 * DAY,
    }
    return Row(**(base | fields))


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_a_running_provider_subscription_refuses_a_second_checkout(status: str) -> None:
    with pytest.raises(BillingError) as refused:
        require_new_checkout(_subscribed(status=status), NOW)
    assert (refused.value.status_code, refused.value.code) == (409, BillingErrorCode.BILLING_ALREADY_SUBSCRIBED)


def test_a_payer_who_bought_under_a_grant_is_subscribed_too() -> None:
    """§14.6 settled: while a grant holds the webhook writes the link, the period and the
    cancellation but keeps ``comped`` and the grant's source (§12.11) — the audit's draft
    (``source = provider`` only) let this payer pay twice."""
    for source in (SubscriptionSource.BETA, SubscriptionSource.MANUAL):
        bought = _subscribed(status=SubscriptionStatus.COMPED, source=source, comped_until=NOW + 30 * DAY)
        with pytest.raises(BillingError, match="change it in the customer portal"):
            require_new_checkout(bought, NOW)
        # Its period over (a cancelled subscription writes canceled_at there): free to buy again.
        require_new_checkout(_subscribed(status="comped", source=source, current_period_end=NOW - DAY), NOW)
        # Set to cancel at the period's end: free to buy again, too.
        require_new_checkout(_subscribed(status="comped", source=source, cancel_at_period_end=True), NOW)
        require_new_checkout(_subscribed(status="comped", source=source, current_period_end=None), NOW)
    # A naive period end is read as UTC (kastlan's columns).
    naive = _subscribed(status="comped", source="beta", current_period_end=datetime(2027, 1, 1))
    with pytest.raises(BillingError):
        require_new_checkout(naive, datetime(2026, 12, 31, 23, 0))


@pytest.mark.parametrize(
    "row",
    [
        Row(),  # the cardless trial: nothing at the provider
        Row(status="comped", source="beta"),  # a beta payer who never bought
        _subscribed(cancel_at_period_end=True),  # cancelled for the period's end
        _subscribed(status="canceled"),
        _subscribed(status="expired"),  # Paddle's paused
        _subscribed(provider_subscription_id=None),  # a customer, no subscription
    ],
)
def test_nothing_running_lets_the_checkout_through(row: Row) -> None:
    require_new_checkout(row, NOW)


# --- the deletion's cancellation, and undoing it (§14.8) ------------------------------------


async def test_a_deletion_request_cancels_at_the_periods_end() -> None:
    """§12.37: so a guest's shared item works until then."""
    provider = FakeBillingProvider()
    for status in ("active", "past_due", "trialing", "comped"):
        assert await cancel_for_deletion(_subscribed(status=status), provider) == "cancelled"
    assert provider.cancels == [{"subscription_id": "sub_1", "immediately": False}] * 4


async def test_a_paused_subscription_is_cancelled_at_once() -> None:
    """Paddle cancels a paused subscription only immediately; keksdose skipped it."""
    provider = FakeBillingProvider()
    assert await cancel_for_deletion(_subscribed(status=SubscriptionStatus.EXPIRED), provider) == "cancelled"
    assert provider.cancels == [{"subscription_id": "sub_1", "immediately": True}]


@pytest.mark.parametrize(
    "row",
    [
        Row(),
        _subscribed(provider_subscription_id=None),
        _subscribed(status="canceled"),
        _subscribed(cancel_at_period_end=True),
    ],
)
async def test_nothing_to_cancel_is_none(row: Row) -> None:
    provider = FakeBillingProvider()
    assert await cancel_for_deletion(row, provider) is None
    assert provider.cancels == []


async def test_a_failed_cancellation_never_blocks_the_deletion(caplog: pytest.LogCaptureFixture) -> None:
    """Decision 14: leaving never depends on paying."""
    provider = FakeBillingProvider(fail=True)
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.billing"):
        assert await cancel_for_deletion(_subscribed(), provider) == "failed"
    (record,) = caplog.records
    assert record.levelno == logging.ERROR and "cancel it by hand" in record.getMessage()
    assert "sub_1" in record.getMessage()


def _cancelled(**fields: Any) -> Row:
    """Cancelled for the period's end, which is still ahead (the helper reads the clock)."""
    return _subscribed(**({"cancel_at_period_end": True, "current_period_end": datetime.now(UTC) + 30 * DAY} | fields))


async def test_a_withdrawn_deletion_removes_the_scheduled_cancellation() -> None:
    """§12.37: an operator's reactivation, or a withdrawn request."""
    provider = FakeBillingProvider()
    assert await resume_after_withdrawal(_cancelled(), provider) == "resumed"
    assert await resume_after_withdrawal(_cancelled(status="comped", source="beta"), provider) == "resumed"
    assert await resume_after_withdrawal(_cancelled(current_period_end=None), provider) == "resumed"
    assert provider.resumes == [{"subscription_id": "sub_1"}] * 3


@pytest.mark.parametrize(
    "row",
    [
        Row(),
        _cancelled(provider_subscription_id=None),
        _subscribed(),  # not set to cancel: the payer never asked, or it was undone already
        _cancelled(status="canceled"),  # cancelled at once, or ended: can't be reinstated
        _cancelled(status="expired"),
        _subscribed(cancel_at_period_end=True, current_period_end=datetime(2026, 1, 1)),  # its period is over
    ],
)
async def test_nothing_to_undo_is_none(row: Row) -> None:
    provider = FakeBillingProvider()
    assert await resume_after_withdrawal(row, provider) is None
    assert provider.resumes == []


async def test_a_failed_resume_is_logged_for_the_operator(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.billing"):
        assert await resume_after_withdrawal(_cancelled(), FakeBillingProvider(fail=True)) == "failed"
    (record,) = caplog.records
    assert "remove the scheduled cancellation by hand" in record.getMessage()


# --- the way back (§14.4) ------------------------------------------------------------------


def test_the_checkout_returns_to_the_subscription_page() -> None:
    assert (CHECKOUT_RETURN_PARAM, CHECKOUT_RETURN_VALUE) == ("checkout", "done")
    assert (
        checkout_return_url("https://kastlan.app", "/admin/billing")
        == "https://kastlan.app/admin/billing?checkout=done"
    )
    assert (
        checkout_return_url("https://keksdose.app/", "settings/subscription")
        == "https://keksdose.app/settings/subscription?checkout=done"
    )
    assert (
        checkout_return_url("http://localhost:5173", "/settings/subscription?tab=plan&checkout=old#top")
        == "http://localhost:5173/settings/subscription?tab=plan&checkout=done#top"
    )
