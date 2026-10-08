"""The plan catalogue, its limits and minor units (billing contract §3.1, §3.4, §4, §12.16),
and the coded refusals (§2.9, §3.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from eifi1_server_kit.billing import (
    BILLING_ERROR_DETAIL,
    BILLING_ERROR_STATUS,
    CURRENCY_EXPONENTS,
    PLAN_LIMIT_CODE,
    BillingCurrency,
    BillingError,
    BillingErrorCode,
    BillingInterval,
    PlanLimitError,
    PlanSpec,
    check_limit,
    dimensions_over_limit,
    minor_to_decimal,
    normalize_plan,
    plan_catalogue,
)

FREE = PlanSpec(code="free", limits={"budgets": 1}, sort=0)
PRO = PlanSpec(
    code="PRO",
    limits={"budgets": 5},
    prices={("chf", "YEAR"): 3000, ("EUR", "year"): 3000, ("CHF", "month"): 300},
    sort=1,
)
UNLIMITED = PlanSpec(code="unlimited", limits={"budgets": None}, sort=2)


# --- the codes ---------------------------------------------------------------------------


def test_the_billing_codes_and_statuses_are_the_contracts() -> None:
    """§2.9 a switched-off billing is "not there"; §3.3 read-only is 402; a deployment
    without the setting a request needs is 503; a bad webhook signature 400 (§5)."""
    assert {str(code): BILLING_ERROR_STATUS[code] for code in BillingErrorCode} == {
        "billing_disabled": 404,
        "billing_read_only": 402,
        "billing_not_configured": 503,
        "invalid_signature": 400,
    }
    assert set(BILLING_ERROR_DETAIL) == set(BillingErrorCode)
    # The read-only detail names no status and no owner: a guest reads it too (§12.6).
    assert "plan" in BILLING_ERROR_DETAIL[BillingErrorCode.BILLING_READ_ONLY]
    assert "owner" not in BILLING_ERROR_DETAIL[BillingErrorCode.BILLING_READ_ONLY]


def test_a_billing_error_carries_its_code_and_status() -> None:
    error = BillingError(BillingErrorCode.BILLING_DISABLED)
    assert (error.status_code, error.code, str(error)) == (404, "billing_disabled", "Billing is not available")
    said = BillingError("billing_not_configured", "No price for pro, CHF, month")  # type: ignore[arg-type]
    assert said.code is BillingErrorCode.BILLING_NOT_CONFIGURED and str(said) == "No price for pro, CHF, month"
    assert isinstance(error, ValueError)
    with pytest.raises(ValueError):
        BillingError("billing_broke")  # type: ignore[arg-type]


def test_a_plan_limit_names_what_it_refuses() -> None:
    error = PlanLimitError(dimension="units", plan="starter", limit=20, used=20)
    assert (error.status_code, error.code) == (402, PLAN_LIMIT_CODE)
    assert error.extra == {"dimension": "units", "plan": "starter", "limit": 20, "used": 20}
    assert (error.dimension, error.plan, error.limit, error.used) == ("units", "starter", 20, 20)
    assert str(error) == "The starter plan's limit on units is 20"
    assert str(PlanLimitError(dimension="units", plan="starter", limit=20, used=20, detail="Full")) == "Full"


# --- plans -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "stored"),
    [("free", "free"), ("PRO", "pro"), ("  Unlimited ", "unlimited"), ("team_5", "team_5"), ("a-b", "a-b")],
)
def test_normalize_plan_is_lowercase_and_case_insensitive(given: str, stored: str) -> None:
    """§12.16: codes become lowercase; keksdose's uppercase history reads the same."""
    assert normalize_plan(given) == stored


@pytest.mark.parametrize("given", ["", "   ", "5pro", "pro plan", "pro!", "p" * 33, "prö"])
def test_normalize_plan_refuses_what_is_no_code(given: str) -> None:
    with pytest.raises(ValueError, match="plan code"):
        normalize_plan(given)


def test_a_plan_spec_normalises_its_code_and_keys() -> None:
    assert PRO.code == "pro"
    assert PRO.prices == {
        (BillingCurrency.CHF, BillingInterval.YEAR): 3000,
        (BillingCurrency.EUR, BillingInterval.YEAR): 3000,
        (BillingCurrency.CHF, BillingInterval.MONTH): 300,
    }
    assert PRO.price("chf", "Year") == 3000 and PRO.price(BillingCurrency.EUR, BillingInterval.MONTH) is None
    assert FREE.price("CHF", "month") is None
    assert PRO.limit("budgets") == 5 and UNLIMITED.limit("budgets") is None
    with pytest.raises(ValueError, match="names no limit for 'budget'"):
        PRO.limit("budget")


@pytest.mark.parametrize(
    "fields",
    [
        {"code": "pro", "prices": {("CHF", "year"): 9.9}},  # never a float (§4)
        {"code": "pro", "prices": {("CHF", "year"): True}},
        {"code": "pro", "prices": {("CHF", "year"): -1}},
        {"code": "pro", "prices": {("USD", "year"): 100}},  # CHF and EUR (§2.6)
        {"code": "pro", "prices": {("CHF", "week"): 100}},
        {"code": "pro", "limits": {"budgets": -1}},
        {"code": "pro", "limits": {"budgets": 1.5}},
        {"code": "pro plan"},
        {"code": "pro", "name": "Pro"},  # display names live in the app's i18n (§3.1)
    ],
)
def test_a_plan_spec_refuses_what_the_contract_does_not_allow(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(fields)


def test_a_plan_spec_is_frozen() -> None:
    with pytest.raises(ValidationError):
        PRO.sort = 3  # type: ignore[misc]


def test_the_catalogue_is_one_read_only_mapping_in_sort_order() -> None:
    plans = plan_catalogue([UNLIMITED, PRO, FREE])
    assert list(plans) == ["free", "pro", "unlimited"]
    assert plans.get(normalize_plan("PRO")) is PRO
    with pytest.raises(TypeError):
        plans["team"] = FREE  # type: ignore[index]


def test_the_catalogue_refuses_what_would_hide_a_limit() -> None:
    with pytest.raises(ValueError, match="at least one plan"):
        plan_catalogue([])
    with pytest.raises(ValueError, match="two plans have the code 'pro'"):
        plan_catalogue([PRO, PlanSpec(code="Pro", limits={"budgets": 9}, sort=7)])
    with pytest.raises(ValueError, match="same dimensions"):
        plan_catalogue([FREE, PlanSpec(code="team", limits={"budget": 9}, sort=3)])


# --- limits ------------------------------------------------------------------------------


def test_check_limit_gates_the_create_past_the_limit() -> None:
    """keksdose's rule: ``used >= limit`` refuses the next budget (§3.4)."""
    check_limit(FREE, "budgets", 0)
    with pytest.raises(PlanLimitError) as refused:
        check_limit(FREE, "budgets", 1)
    assert refused.value.extra == {"dimension": "budgets", "plan": "free", "limit": 1, "used": 1}
    check_limit(UNLIMITED, "budgets", 10_000)


def test_check_limit_counts_what_a_create_adds() -> None:
    """kastlan's storage: the file's size is what the create adds."""
    storage = PlanSpec(code="starter", limits={"storage": 1000})
    check_limit(storage, "storage", 400, adding=600)
    with pytest.raises(PlanLimitError):
        check_limit(storage, "storage", 400, adding=601)
    check_limit(storage, "storage", 1000, adding=0)


def test_check_limit_refuses_a_programming_error() -> None:
    with pytest.raises(ValueError, match="names no limit"):
        check_limit(FREE, "curves", 0)
    with pytest.raises(ValueError, match="zero or more"):
        check_limit(FREE, "budgets", -1)
    with pytest.raises(ValueError, match="zero or more"):
        check_limit(FREE, "budgets", 0, adding=-1)


def test_dimensions_over_limit_reports_a_downgrade_without_refusing_it() -> None:
    kastlan = PlanSpec(code="starter", limits={"units": 20, "seats": 3, "storage": None})
    assert dimensions_over_limit(kastlan, {"units": 21, "seats": 3, "storage": 10**12}) == ["units"]
    assert dimensions_over_limit(kastlan, {"seats": 4}) == ["seats"]
    assert dimensions_over_limit(kastlan, {}) == []


# --- money -------------------------------------------------------------------------------


def test_minor_units_become_an_exact_decimal() -> None:
    """§4: integer minor units on the wire, never a float."""
    assert CURRENCY_EXPONENTS == {"CHF": 2, "EUR": 2}
    assert minor_to_decimal(990, "CHF") == Decimal("9.90")
    assert str(minor_to_decimal(990, BillingCurrency.EUR)) == "9.90"
    assert str(minor_to_decimal(3000, " chf ")) == "30.00"
    assert minor_to_decimal(0, "EUR") == 0
    with pytest.raises(ValueError, match="no minor-unit exponent for 'JPY'"):
        minor_to_decimal(100, "JPY")
    with pytest.raises(TypeError, match="not float"):
        minor_to_decimal(9.9, "CHF")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="not bool"):
        minor_to_decimal(True, "CHF")
