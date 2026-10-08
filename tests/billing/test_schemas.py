"""Billing's wire shapes (billing contract §4, §6, §12.5)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from eifi1_server_kit.billing import (
    BillingCurrency,
    BillingInterval,
    BillingOverview,
    BillingStatus,
    CheckoutAnswer,
    CheckoutRequest,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanIntervalPrices,
    PlanOut,
    PlanPrices,
    PlanSpec,
    SubscriptionSource,
    SubscriptionStatus,
    SyncRefusal,
    plan_catalogue,
    plans_out,
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


def test_the_overview_shows_a_beta_rows_end_from_the_launch_date() -> None:
    """§3.2: a beta row stored without an end shows — and is in good standing until — the
    launch plus 12 months; without a launch date it shows no end."""
    beta = Row(plan_code="pro", status="comped", source="beta")
    launch = datetime(2026, 11, 1, tzinfo=UTC)
    shown = BillingOverview.from_row(beta, plan=PRO, usage={}, currency="CHF", now=NOW, launch=launch)
    assert shown.comped_until == datetime(2027, 11, 1, tzinfo=UTC) and shown.in_good_standing
    ended = BillingOverview.from_row(
        beta, plan=PRO, usage={}, currency="CHF", now=datetime(2027, 11, 1, tzinfo=UTC), launch=launch
    )
    assert not ended.in_good_standing
    unknown = BillingOverview.from_row(beta, plan=PRO, usage={}, currency="CHF", now=NOW + 9999 * DAY)
    assert unknown.comped_until is None and unknown.in_good_standing


def test_the_overview_reads_the_rows_vocabulary() -> None:
    row = Row(plan_code="pro", status="comped", source="beta", comped_until=NOW + DAY)
    overview = BillingOverview.from_row(row, plan=PRO, usage={"budgets": 9}, currency="CHF", now=NOW)
    assert (overview.status, overview.source) == (SubscriptionStatus.COMPED, SubscriptionSource.BETA)
    assert overview.comped_until == NOW + DAY and overview.usage == {"budgets": 9}


GIB = 1024**3


def _kastlan(code: str, units: int, seats: int, storage_gib: int, month: int, sort: int) -> PlanSpec:
    """A plan of §13.1: the same gross figure in CHF and EUR, yearly is ten months."""
    prices = {
        (currency, interval): month * (10 if interval == "year" else 1)
        for currency in ("EUR", "CHF")
        for interval in ("year", "month")
    }
    return PlanSpec(
        code=code,
        limits={"units": units, "seats": seats, "storage": storage_gib * GIB},
        prices=prices,
        sort=sort,
    )


STARTER = _kastlan("starter", 40, 2, 5, 2900, sort=0)
STANDARD = _kastlan("standard", 150, 5, 25, 7900, sort=1)
PROFESSIONAL = _kastlan("professional", 500, 15, 100, 19900, sort=2)


def test_the_plans_go_over_the_wire_with_nested_prices() -> None:
    """§4 ``GET /billing/plans``, the §13.1 catalogue: currency → interval → gross minor
    units, ui-kit's ``PlanPrices`` — no tuple keys, no list to convert in the page."""
    plans = plans_out(plan_catalogue([PROFESSIONAL, STARTER, STANDARD]))
    wire = json.loads(TypeAdapter(list[PlanOut]).dump_json(plans))
    assert wire == [
        {
            "code": "starter",
            "prices": {"CHF": {"month": 2900, "year": 29000}, "EUR": {"month": 2900, "year": 29000}},
            "limits": {"units": 40, "seats": 2, "storage": 5_368_709_120},
            "sort": 0,
        },
        {
            "code": "standard",
            "prices": {"CHF": {"month": 7900, "year": 79000}, "EUR": {"month": 7900, "year": 79000}},
            "limits": {"units": 150, "seats": 5, "storage": 26_843_545_600},
            "sort": 1,
        },
        {
            "code": "professional",
            "prices": {"CHF": {"month": 19900, "year": 199000}, "EUR": {"month": 19900, "year": 199000}},
            "limits": {"units": 500, "seats": 15, "storage": 107_374_182_400},
            "sort": 2,
        },
    ]
    # The same order on every run, whatever order the spec lists its prices in.
    assert [list(plan["prices"]) for plan in wire] == [["CHF", "EUR"]] * 3
    assert all(list(by_interval) == ["month", "year"] for plan in wire for by_interval in plan["prices"].values())
    # Back again: the page's JSON validates, and its prices are the spec's.
    for out, spec in zip(
        TypeAdapter(list[PlanOut]).validate_python(wire), (STARTER, STANDARD, PROFESSIONAL), strict=True
    ):
        assert out == PlanOut.from_spec(spec)
        assert out.prices["CHF"] == {"month": spec.price("CHF", "month"), "year": spec.price("CHF", "year")}


def test_a_plan_on_the_wire_leaves_out_what_it_does_not_sell() -> None:
    """A combination not sold is absent, never 0 (the page reads 0 as free); unlimited is
    null; a plan without prices is ``{}``; the order is ``sort``'s, then the code's."""
    yearly = PlanSpec(code="Personal", limits={"curves": 10}, prices={("EUR", "year"): 9000}, sort=1)
    unlimited = PlanSpec(code="professional", limits={"curves": None}, sort=1)
    free = PlanSpec(code="free", limits={"curves": 1}, sort=0)
    by_hand = {"professional": unlimited, "personal": yearly, "free": free}  # not plan_catalogue's order
    assert [plan.model_dump(mode="json") for plan in plans_out(by_hand)] == [
        {"code": "free", "prices": {}, "limits": {"curves": 1}, "sort": 0},
        {"code": "personal", "prices": {"EUR": {"year": 9000}}, "limits": {"curves": 10}, "sort": 1},
        {"code": "professional", "prices": {}, "limits": {"curves": None}, "sort": 1},
    ]
    for prices in (
        {"EUR": {"year": 90.0}},  # money is never a float (§4)
        {"USD": {"year": 9000}},  # a currency the apps don't charge in
        {"EUR": {"week": 900}},
    ):
        with pytest.raises(ValidationError):
            PlanOut.model_validate({"code": "personal", "prices": prices, "limits": {}, "sort": 0})


def test_the_plans_schema_names_every_currency_and_period() -> None:
    """keksdose's 0.32 report: openapi-typescript drops ``propertyNames``, so a dict keyed
    by the enums generates ``{[key: string]: …}``, not assignable to ui-kit's
    ``PlanPrices``. Named, optional keys and no others generate ``{CHF?: {month?: number;
    year?: number}; EUR?: …}`` — ui-kit's type as it is."""
    assert sorted(PlanPrices.__optional_keys__) == [currency.value for currency in BillingCurrency]
    assert sorted(PlanIntervalPrices.__optional_keys__) == [interval.value for interval in BillingInterval]
    assert not PlanPrices.__required_keys__ and not PlanIntervalPrices.__required_keys__
    for mode in ("validation", "serialization"):
        schema = TypeAdapter(list[PlanOut]).json_schema(mode=mode)
        defs = schema["$defs"]
        assert defs["PlanOut"]["properties"]["prices"] == {"$ref": "#/$defs/PlanPrices"}
        assert defs["PlanPrices"]["properties"] == {
            "CHF": {"$ref": "#/$defs/PlanIntervalPrices"},
            "EUR": {"$ref": "#/$defs/PlanIntervalPrices"},
        }
        assert defs["PlanIntervalPrices"]["properties"] == {
            "month": {"minimum": 0, "title": "Month", "type": "integer"},
            "year": {"minimum": 0, "title": "Year", "type": "integer"},
        }
        for name in ("PlanPrices", "PlanIntervalPrices"):
            assert defs[name]["additionalProperties"] is False and "required" not in defs[name]


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
