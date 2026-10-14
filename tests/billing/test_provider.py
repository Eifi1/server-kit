"""The provider's port and what a route checks first (billing contract §14.2, §14.4–§14.6,
§14.8 with §12.37): the deployment's client, the plan sold, a second checkout, the
deletion's cancellation and its undoing, and the way back from a checkout."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from eifi1_server_kit.billing import (
    CHECKOUT_RETURN_PARAM,
    CHECKOUT_RETURN_VALUE,
    BillingError,
    BillingErrorCode,
    BillingProvider,
    BillingProviderClient,
    BillingSettings,
    CheckoutRequest,
    NoProviderClient,
    PaddleClient,
    PaddleEnvironment,
    PlanSpec,
    PlanSwitchRequest,
    SubscriptionSource,
    SubscriptionStatus,
    billing_provider_client,
    cancel_for_deletion,
    checkout_return_url,
    plan_catalogue,
    require_new_checkout,
    require_plan_switch,
    resume_after_withdrawal,
    sold_plan,
    switch_price,
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
        {"billing_api_key": "pdl_sdbx_apikey_example"},
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
        client.preview_plan_change(subscription_id="sub_1", price_id="pri_1"),
        client.change_plan(subscription_id="sub_1", price_id="pri_1"),
        client.customer_for(email="payer@example.com"),
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


def test_the_provider_is_read_by_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """kastlan's 0.33 report: a settings object holding the plain text ``"paddle"`` — set
    by a test's ``monkeypatch`` or ``model_construct``, or an app's field typed ``str`` —
    got :class:`NoProviderClient`, compared by identity with the enum."""
    key = SecretStr("pdl_sdbx_apikey_example")
    fields: dict[str, Any] = {"billing_provider": "paddle", "billing_api_key": key}
    constructed = BillingSettings.model_construct(**fields)
    assert constructed.billing_provider == "paddle" and type(constructed.billing_provider) is str
    assert isinstance(billing_provider_client(constructed), PaddleClient)
    patched = BillingSettings(billing_api_key="pdl_sdbx_apikey_example")
    monkeypatch.setattr(patched, "billing_provider", "paddle")
    assert isinstance(billing_provider_client(patched), PaddleClient)
    monkeypatch.setattr(patched, "billing_provider", "lemonsqueezy")  # no provider since 0.8
    assert isinstance(billing_provider_client(patched), NoProviderClient)
    # And the enum, as the environment parses it.
    assert isinstance(
        billing_provider_client(BillingSettings(billing_provider=BillingProvider.PADDLE, billing_api_key=key)),
        PaddleClient,
    )


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
        with pytest.raises(BillingError, match="change its plan instead"):
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
    # §15.2.10: the inverse — nothing to switch, the page offers the checkout.
    with pytest.raises(BillingError) as refused:
        require_plan_switch(row, NOW)
    assert (refused.value.status_code, refused.value.code) == (409, BillingErrorCode.BILLING_NOT_SUBSCRIBED)


# --- the plan switch (§15.2.10) --------------------------------------------------------------

SWITCH_SETTINGS = BillingSettings(
    billing_price_ids={
        "pro": {"CHF": {"year": ["pri_pro_chf_y", "pri_pro_chf_y_2025"], "month": "pri_pro_chf_m"}},
        "legacy": {"CHF": {"year": "pri_legacy_chf_y"}},
        "max": {"CHF": {"year": "pri_max_chf_y"}, "EUR": {"year": "pri_max_eur_y"}},
    }
)
MAX = PlanSpec(code="max", limits={"budgets": 9}, prices={("CHF", "year"): 9000, ("EUR", "year"): 9000}, sort=3)
PRO_MONTHLY = PlanSpec(code="pro", limits={"budgets": 5}, prices={("CHF", "year"): 3000, ("CHF", "month"): 300}, sort=1)
SWITCH_PLANS = plan_catalogue([FREE, PRO_MONTHLY, LEGACY, MAX])


def _switch(plan: str = "max", interval: str = "year") -> PlanSwitchRequest:
    return PlanSwitchRequest.model_validate({"plan": plan, "interval": interval})


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_a_running_subscription_may_switch(status: str) -> None:
    require_plan_switch(_subscribed(status=status), NOW)
    # Bought under a grant: its period says it runs.
    require_plan_switch(_subscribed(status="comped", source="manual", comped_until=NOW + DAY), NOW)


def test_the_switch_takes_the_price_in_the_currency_it_pays() -> None:
    """The plan and interval asked for, in the row's currency: a switch keeps it."""
    row = _subscribed(provider_price_id="pri_pro_chf_y")
    assert switch_price(SWITCH_PLANS, row, _switch(), SWITCH_SETTINGS) == "pri_max_chf_y"
    assert switch_price(SWITCH_PLANS, row, _switch("PRO", "Month"), SWITCH_SETTINGS) == "pri_pro_chf_m"
    # From a retired price to the plan's current one is a switch, not "unchanged".
    retired = _subscribed(provider_price_id="pri_pro_chf_y_2025")
    assert switch_price(SWITCH_PLANS, retired, _switch("pro"), SWITCH_SETTINGS) == "pri_pro_chf_y"
    # A payer on a plan no longer sold may switch away, to a sold one.
    on_legacy = _subscribed(plan_code="legacy", provider_price_id="pri_legacy_chf_y")
    assert switch_price(SWITCH_PLANS, on_legacy, _switch("pro"), SWITCH_SETTINGS, sold=("pro", "max")) == (
        "pri_pro_chf_y"
    )


def test_the_price_it_pays_now_is_unchanged() -> None:
    with pytest.raises(BillingError) as refused:
        switch_price(SWITCH_PLANS, _subscribed(provider_price_id="pri_pro_chf_y"), _switch("pro"), SWITCH_SETTINGS)
    assert (refused.value.status_code, refused.value.code) == (409, BillingErrorCode.BILLING_PLAN_UNCHANGED)


@pytest.mark.parametrize(
    ("request_", "sold"),
    [
        (_switch("legacy"), ("pro", "max")),  # no switch TO a plan no longer sold
        (_switch("unknown"), None),  # not in the catalogue
        (_switch("free"), None),  # free: no price at all
        (_switch("max", "month"), None),  # no monthly price
        (_switch("legacy"), None),  # sold, but not in EUR (below)
    ],
)
def test_a_switch_to_a_plan_not_sold_is_422(request_: PlanSwitchRequest, sold: tuple[str, ...] | None) -> None:
    for paying in ("pri_pro_chf_y", "pri_max_eur_y"):
        row = _subscribed(provider_price_id=paying)
        if paying == "pri_pro_chf_y" and request_.plan == "legacy" and sold is None:
            assert switch_price(SWITCH_PLANS, row, request_, SWITCH_SETTINGS) == "pri_legacy_chf_y"
            continue
        with pytest.raises(BillingError) as refused:
            switch_price(SWITCH_PLANS, row, request_, SWITCH_SETTINGS, sold=sold)
        assert (refused.value.status_code, refused.value.code) == (422, BillingErrorCode.BILLING_PLAN_NOT_SOLD)


@pytest.mark.parametrize("paying", [None, "", "pri_unknown"])
def test_a_row_without_a_known_price_is_not_configured(paying: str | None) -> None:
    """A row from before 0.12 until its next snapshot, or a price the settings lost: 503."""
    with pytest.raises(BillingError) as refused:
        switch_price(SWITCH_PLANS, _subscribed(provider_price_id=paying), _switch(), SWITCH_SETTINGS)
    assert (refused.value.status_code, refused.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)


def test_a_sold_combination_without_its_price_id_is_not_configured() -> None:
    """The catalogue sells max in EUR per year; settings without that id are the deployment's fault."""
    settings = BillingSettings(billing_price_ids={"pro": {"EUR": {"year": "pri_pro_eur_y"}}})
    with pytest.raises(BillingError) as refused:
        switch_price(SWITCH_PLANS, _subscribed(provider_price_id="pri_pro_eur_y"), _switch(), settings)
    assert refused.value.code is BillingErrorCode.BILLING_NOT_CONFIGURED


def test_a_switch_request_takes_no_currency_or_price() -> None:
    for extra in ({"currency": "EUR"}, {"price_id": "pri_x"}):
        with pytest.raises(ValueError, match="Extra inputs"):
            PlanSwitchRequest.model_validate({"plan": "pro", "interval": "year", **extra})


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
    """Cancelled for the period's end, as Paddle's ``subscription.updated`` writes it."""
    return _subscribed(**({"cancel_at_period_end": True} | fields))


def _paddle(paddle: PaddleApiFake, **fields: Any) -> BillingProviderClient:
    """The deployment's client on Paddle's API fake; billing off unless ``fields`` say."""
    settings = BillingSettings(billing_provider="paddle", billing_api_key="pdl_sdbx_apikey_example", **fields)
    return billing_provider_client(settings, client=httpx.AsyncClient(transport=paddle.transport))


async def test_a_withdrawn_deletion_removes_the_scheduled_cancellation() -> None:
    """§12.37: an operator's reactivation, or a withdrawn request — after the webhook wrote
    the cancellation onto the row."""
    provider = FakeBillingProvider()
    assert await resume_after_withdrawal(_cancelled(), provider, NOW) == "resumed"
    assert await resume_after_withdrawal(_cancelled(status="comped", source="beta"), provider, NOW) == "resumed"
    assert await resume_after_withdrawal(_cancelled(current_period_end=None), provider, NOW) == "resumed"
    assert provider.resumes == [{"subscription_id": "sub_1"}] * 3


async def test_a_reactivation_before_the_webhook_is_undone_too() -> None:
    """keksdose's 0.33 report: :func:`cancel_for_deletion` never sets
    ``cancel_at_period_end`` — Paddle's ``subscription.updated`` does — so a reactivation
    before that webhook found "not cancelled" on the row and answered ``None``, while the
    request's audit said ``"cancelled"`` and Paddle still had the cancellation scheduled."""
    provider = FakeBillingProvider()
    row = _subscribed()
    assert await cancel_for_deletion(row, provider) == "cancelled"
    assert not row.cancel_at_period_end  # the webhook hasn't come yet
    assert await resume_after_withdrawal(row, provider, NOW) == "resumed"
    assert provider.resumes == [{"subscription_id": "sub_1"}]


async def test_a_reactivation_with_billing_off_reaches_paddle() -> None:
    """Switched off, the webhook answers 404: the row never learns of the cancellation, and
    the deletion's cancellation and its undoing still reach Paddle (§14.8)."""
    paddle = PaddleApiFake()
    client = _paddle(paddle)
    row = _subscribed()
    assert await cancel_for_deletion(row, client) == "cancelled"
    assert await resume_after_withdrawal(row, client, NOW) == "resumed"
    resume = paddle.requests[-1]
    assert (resume.method, str(resume.url)) == ("PATCH", "https://sandbox-api.paddle.com/subscriptions/sub_1")
    assert paddle.sent == {"scheduled_change": None}


async def test_nothing_scheduled_is_harmless() -> None:
    """Paddle's docs don't say what ``scheduled_change: null`` does when nothing is
    scheduled: setting the field to the value it has answers 200 (the fake's
    ``update_subscription``), and the subscription runs on. Refused instead, it is nothing
    to resume — not a failure for an operator to chase."""
    paddle = PaddleApiFake()
    assert await resume_after_withdrawal(_subscribed(), _paddle(paddle), NOW) == "resumed"
    paddle.fail_with(400, "subscription_scheduled_change_invalid_update")
    assert await resume_after_withdrawal(_subscribed(), _paddle(paddle), NOW) is None


@pytest.mark.parametrize(
    "row",
    [
        Row(),
        _cancelled(provider_subscription_id=None),
        _subscribed(provider_subscription_id=None),
        _cancelled(status="canceled"),  # cancelled at once, or ended: can't be reinstated
        _subscribed(status="canceled"),
        _cancelled(status="expired"),
        _cancelled(current_period_end=NOW - DAY),  # its period is over
        _subscribed(current_period_end=NOW),
    ],
)
async def test_nothing_to_undo_is_none(row: Row) -> None:
    provider = FakeBillingProvider()
    assert await resume_after_withdrawal(row, provider, NOW) is None
    assert provider.resumes == []


async def test_an_ended_subscription_paddle_refuses_is_nothing_to_undo(caplog: pytest.LogCaptureFixture) -> None:
    """Ended at Paddle before its webhook reached the row: Paddle refuses the update
    (``subscription_update_when_canceled``), which is nothing to resume, logged at INFO."""
    paddle = PaddleApiFake()
    paddle.fail_with(400, "subscription_update_when_canceled")
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.billing"):
        assert await resume_after_withdrawal(_cancelled(), _paddle(paddle), NOW) is None
    assert [record.levelno for record in caplog.records if record.name == "eifi1_server_kit.billing"][
        -1
    ] == logging.INFO
    assert "nothing to resume for the subscription sub_1" in caplog.text


async def test_the_period_is_read_at_now_or_the_clock() -> None:
    """``now``, as :func:`require_new_checkout` takes it; unset, the current time. A naive
    end or ``now`` is read as UTC."""
    provider = FakeBillingProvider()
    ends = _cancelled(current_period_end=datetime(2026, 11, 1))
    assert await resume_after_withdrawal(ends, provider, datetime(2026, 10, 31, 23, 0)) == "resumed"
    assert await resume_after_withdrawal(ends, provider, datetime(2026, 11, 1, tzinfo=UTC)) is None
    running = _cancelled(current_period_end=datetime.now(UTC) + 30 * DAY)
    assert await resume_after_withdrawal(running, provider) == "resumed"
    assert await resume_after_withdrawal(_cancelled(current_period_end=datetime(2026, 1, 1)), provider) is None
    assert len(provider.resumes) == 2


@pytest.mark.parametrize("failure", [(500, "internal_error"), (400, "subscription_locked_renewal"), (502, None)])
async def test_a_failed_resume_is_logged_for_the_operator(
    failure: tuple[int, str | None], caplog: pytest.LogCaptureFixture
) -> None:
    """Any other refusal leaves the cancellation in place: "failed", for the operator."""
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.billing"):
        assert await resume_after_withdrawal(_cancelled(), FakeBillingProvider(fail=True), NOW) == "failed"
    (record,) = caplog.records
    assert "remove the scheduled cancellation by hand" in record.getMessage()
    paddle = PaddleApiFake()
    paddle.fail_with(*failure)
    assert await resume_after_withdrawal(_cancelled(), _paddle(paddle), NOW) == "failed"


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
