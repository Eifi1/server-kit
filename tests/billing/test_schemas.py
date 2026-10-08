"""Billing's wire shapes (billing contract §4, §6, §12.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eifi1_server_kit.billing import (
    BillingCurrency,
    BillingInterval,
    BillingOverview,
    BillingStatus,
    CheckoutAnswer,
    CheckoutRequest,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanSpec,
    SubscriptionSource,
    SubscriptionStatus,
    SyncRefusal,
)
from tests.billing._rows import NOW, Row

DAY = timedelta(days=1)
FREE = PlanSpec(code="free", limits={"budgets": 1})
PRO = PlanSpec(code="pro", limits={"budgets": 5}, prices={("CHF", "year"): 3000})


def test_billing_status_is_the_switch() -> None:
    assert BillingStatus(billing_enabled=False).model_dump() == {"billing_enabled": False}


def test_the_overview_is_built_from_the_row() -> None:
    """§4's fields, with the standing as of now."""
    row = Row(plan_code="FREE", trial_ends_at=datetime(2026, 10, 18, 12, 0))
    overview = BillingOverview.from_row(row, plan=FREE, usage={"budgets": 1}, currency="chf", now=NOW)
    assert overview.model_dump(mode="json") == {
        "plan": "free",
        "status": "trialing",
        "source": "trial",
        "in_good_standing": True,
        "trial_ends_at": "2026-10-18T12:00:00Z",
        "comped_until": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
        "limits": {"budgets": 1},
        "usage": {"budgets": 1},
        "currency": "CHF",
    }
    assert not BillingOverview.from_row(
        row, plan=FREE, usage={}, currency=BillingCurrency.EUR, now=NOW + 10 * DAY
    ).in_good_standing
    late = Row(
        plan_code="pro",
        status="past_due",
        source="provider",
        current_period_end=NOW - 20 * DAY,
        cancel_at_period_end=None,
    )
    assert not BillingOverview.from_row(
        late, plan=PRO, usage={}, currency="EUR", now=NOW, retry_grace=timedelta(days=14)
    ).in_good_standing
    with pytest.raises(ValueError, match="the row's plan is 'free', not 'pro'"):
        BillingOverview.from_row(Row(), plan=PRO, usage={}, currency="CHF", now=NOW)


def test_the_overview_reads_the_rows_vocabulary() -> None:
    row = Row(plan_code="pro", status="comped", source="beta", comped_until=NOW + DAY)
    overview = BillingOverview.from_row(row, plan=PRO, usage={"budgets": 9}, currency="CHF", now=NOW)
    assert (overview.status, overview.source) == (SubscriptionStatus.COMPED, SubscriptionSource.BETA)
    assert overview.comped_until == NOW + DAY and overview.usage == {"budgets": 9}


def test_a_checkout_request_is_normalised_and_closed() -> None:
    request = CheckoutRequest.model_validate({"plan": "PRO", "interval": "Year", "currency": "chf"})
    assert (request.plan, request.interval, request.currency) == ("pro", BillingInterval.YEAR, BillingCurrency.CHF)
    for body in (
        {"plan": "pro", "interval": "week", "currency": "CHF"},
        {"plan": "pro", "interval": "year", "currency": "USD"},
        {"plan": "pro", "interval": "year", "currency": "CHF", "price": 1},  # the price is the settings'
        {"plan": "pro plan", "interval": "year", "currency": "CHF"},
    ):
        with pytest.raises(ValidationError):
            CheckoutRequest.model_validate(body)


def test_a_checkout_answer_is_the_providers_page() -> None:
    assert CheckoutAnswer(url="https://sandbox-checkout.paddle.com/c/abc").url.startswith("https://")
    with pytest.raises(ValidationError, match="http"):
        CheckoutAnswer(url="javascript:alert(1)")


def test_a_plan_change_is_acknowledged_and_closed() -> None:
    """§6: AdminAction.PLAN at the acknowledge level; no confirm_email."""
    change = PlanChangeRequest.model_validate(
        {"plan": "Pro", "comped_until": "2027-10-08T00:00:00", "acknowledged": True}
    )
    assert change.plan == "pro" and change.comped_until == datetime(2027, 10, 8, tzinfo=UTC) and change.acknowledged
    assert PlanChangeRequest(plan="free").model_dump() == {"plan": "free", "comped_until": None, "acknowledged": False}
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlanChangeRequest.model_validate({"plan": "pro", "confirm_email": "ada@example.com"})


def test_a_plan_change_answer_reports_a_downgrade_below_use() -> None:
    """§3.4, §6: not an error, and nothing is cleaned up."""
    down = PlanChangeResponse.of("PRO", FREE, {"budgets": 3})
    assert down.model_dump() == {"previous_plan": "pro", "plan": "free", "limits": {"budgets": 1}, "over_limit": True}
    assert not PlanChangeResponse.of("free", PRO, {"budgets": 3}).over_limit


def test_a_sync_refusal_names_the_refused_changes() -> None:
    """§12.5: the refused change ids and the code, beside the updates."""
    change = uuid.UUID("6f1c2a52-0d7e-4c43-9a51-5d1f1f3b3a10")
    refusal = SyncRefusal(change_ids=[change, "handover-room-7"])
    assert refusal.model_dump() == {
        "code": "billing_read_only",
        "detail": "Read-only for now: changes need an active plan",
        "change_ids": ["6f1c2a52-0d7e-4c43-9a51-5d1f1f3b3a10", "handover-room-7"],
    }
    assert (
        SyncRefusal(code="plan_limit", detail="The free plan's limit on budgets is 1", change_ids=["b1"]).code
        == "plan_limit"
    )
    with pytest.raises(ValidationError):
        SyncRefusal(change_ids=[])
    with pytest.raises(ValidationError):
        SyncRefusal(change_ids=[""])
