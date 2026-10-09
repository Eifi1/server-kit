"""Billing's settings (billing contract §3.1, §4, §10): off by default, on only with the
secrets, the price ids both ways, and the standing behind the switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from eifi1_server_kit.billing import (
    BillingCurrency,
    BillingError,
    BillingErrorCode,
    BillingInterval,
    BillingProvider,
    BillingSettings,
    PriceRef,
    SubscriptionSource,
    SubscriptionStatus,
)
from tests.billing._rows import NOW, Row

SECRETS: dict[str, Any] = {
    "billing_provider": "paddle",
    "billing_api_key": "pdl_live_apikey_example",
    "billing_webhook_secret": "pdl_ntfset_example",
}
PRICES = {
    "PRO": {"chf": {"year": "pri_pro_chf_y", "Month": ["pri_pro_chf_m", "pri_pro_chf_m_2025"]}},
    "team": {"EUR": {"year": 12345}},
}


def test_billing_is_off_by_default() -> None:
    """§4: a fresh deployment has no billing at all."""
    settings = BillingSettings()
    assert not settings.billing_enabled and settings.billing_provider is None
    assert settings.billing_price_ids == {} and settings.billing_launch_at is None
    with pytest.raises(BillingError) as refused:
        settings.require_billing_enabled()
    assert (refused.value.status_code, refused.value.code) == (404, BillingErrorCode.BILLING_DISABLED)


def test_switching_on_requires_the_provider_and_the_secrets() -> None:
    """§10: a missing secret fails at start, not at the first webhook."""
    with pytest.raises(ValidationError, match="billing_enabled needs billing_provider, billing_api_key"):
        BillingSettings(billing_enabled=True)
    with pytest.raises(ValidationError, match="needs billing_webhook_secret"):
        BillingSettings(billing_enabled=True, **{**SECRETS, "billing_webhook_secret": "  "})
    on = BillingSettings(billing_enabled=True, **SECRETS)
    on.require_billing_enabled()
    assert on.billing_provider is BillingProvider.PADDLE
    assert "pdl_ntfset_example" not in repr(on)  # SecretStr: never in a log line
    assert on.billing_webhook_key() == "pdl_ntfset_example"


def test_an_app_inherits_it_or_reads_its_own_fields() -> None:
    class Settings(BillingSettings):
        billing_enabled: bool = True
        billing_provider: BillingProvider | None = BillingProvider.LEMONSQUEEZY

    with pytest.raises(ValidationError, match="billing_api_key, billing_webhook_secret"):
        Settings()

    class Own:
        billing_enabled = False
        billing_webhook_secret = "ls_signing_secret"

    assert BillingSettings.model_validate(Own(), from_attributes=True).billing_webhook_key() == "ls_signing_secret"
    with pytest.raises(BillingError, match="No webhook secret") as refused:
        BillingSettings().billing_webhook_key()
    assert refused.value.status_code == 503


def test_the_price_ids_are_normalised_and_looked_up_both_ways() -> None:
    """§3.1: plan → currency → interval → id; a list keeps retired ids findable."""
    settings = BillingSettings(billing_price_ids=PRICES)
    assert settings.billing_price_ids == {
        "pro": {
            BillingCurrency.CHF: {
                BillingInterval.YEAR: ["pri_pro_chf_y"],
                BillingInterval.MONTH: ["pri_pro_chf_m", "pri_pro_chf_m_2025"],
            }
        },
        "team": {BillingCurrency.EUR: {BillingInterval.YEAR: ["12345"]}},
    }
    assert settings.billing_price_id("Pro", "CHF", "month") == "pri_pro_chf_m"
    assert settings.billing_price_id("team", BillingCurrency.EUR, BillingInterval.YEAR) == "12345"
    assert settings.billing_price_ref("pri_pro_chf_m_2025") == PriceRef(
        "pro", BillingCurrency.CHF, BillingInterval.MONTH
    )
    assert settings.billing_plan_for_price("12345") == "team"
    assert settings.billing_price_ref("pri_unknown") is None and settings.billing_plan_for_price("pri_unknown") is None


@pytest.mark.parametrize(
    ("plan", "currency", "interval"),
    [("pro", "EUR", "year"), ("free", "CHF", "year"), ("pro", "USD", "year"), ("pro", "CHF", "week")],
)
def test_a_checkout_without_a_price_id_is_not_configured(plan: str, currency: str, interval: str) -> None:
    settings = BillingSettings(billing_price_ids=PRICES)
    with pytest.raises(BillingError, match="No price id for") as refused:
        settings.billing_price_id(plan, currency, interval)
    assert (refused.value.status_code, refused.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)


@pytest.mark.parametrize(
    "prices",
    [
        {"pro": {"CHF": {"year": "pri_1"}}, "team": {"CHF": {"year": "pri_1"}}},  # one id, two plans
        {"pro": {"CHF": {"year": "pri_1", "month": "pri_1"}}},  # one id, two periods
        {"pro": {"CHF": {"year": []}}},
        {"pro": {"CHF": {"year": ""}}},
        {"pro": {"CHF": {"year": True}}},
        {"pro": {"USD": {"year": "pri_1"}}},
        {"pro plan": {"CHF": {"year": "pri_1"}}},
    ],
)
def test_the_price_ids_refuse_what_would_make_a_lookup_ambiguous(prices: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BillingSettings(billing_price_ids=prices)


def test_the_standing_is_everyones_while_billing_is_off() -> None:
    """§4: the read-only gate is off with the switch."""
    lapsed = Row(status=SubscriptionStatus.EXPIRED)
    assert BillingSettings().billing_standing(lapsed, NOW)
    on = BillingSettings(billing_enabled=True, **SECRETS)
    assert not on.billing_standing(lapsed, NOW)
    assert on.billing_standing(None, NOW)
    late = Row(status=SubscriptionStatus.PAST_DUE, current_period_end=NOW - timedelta(days=20))
    assert on.billing_standing(late, NOW)
    assert not on.billing_standing(late, NOW, retry_grace=timedelta(days=14))


def test_the_standing_reads_a_beta_rows_end_from_the_launch_date() -> None:
    """§3.2: a beta row stored without an end runs to the settings' launch + 12 months."""
    beta = Row(status=SubscriptionStatus.COMPED, source=SubscriptionSource.BETA)
    on = BillingSettings(billing_enabled=True, **SECRETS)
    assert on.billing_standing(beta, NOW + 9999 * timedelta(days=1))  # no launch date yet
    launched = BillingSettings(billing_enabled=True, billing_launch_at=datetime(2026, 11, 1), **SECRETS)
    assert launched.billing_standing(beta, datetime(2027, 10, 31, tzinfo=UTC))
    assert not launched.billing_standing(beta, datetime(2027, 11, 1, tzinfo=UTC))


class _GardenSettings(BaseSettings, BillingSettings):
    """An app's settings as the README shows them: pydantic-settings, the mixin, a prefix."""

    model_config = SettingsConfigDict(env_prefix="GARDEN_")

    garden_name: str = "Ada's Garden Planner"


_BILLING_VARIABLES = [f"GARDEN_{name.upper()}" for name in BillingSettings.model_fields]


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [*_BILLING_VARIABLES, "GARDEN_GARDEN_NAME"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_empty_variable_is_an_unset_one(environment: pytest.MonkeyPatch) -> None:
    """keksdose's 0.32 report: an ``.env`` template lists the variables empty until billing
    goes on. ``BILLING_PRICE_IDS=`` failed in pydantic-settings' JSON decoding, and
    ``BILLING_LAUNCH_AT=`` was no datetime; every billing variable left empty now reads as
    its default."""
    for name in _BILLING_VARIABLES:
        environment.setenv(name, "")
    environment.setenv("GARDEN_BILLING_LAUNCH_AT", "  ")
    environment.setenv("GARDEN_GARDEN_NAME", "")
    settings = _GardenSettings()
    assert settings.billing_price_ids == {} and settings.billing_launch_at is None
    assert not settings.billing_enabled and settings.billing_provider is None
    assert settings.billing_api_key is None and settings.billing_webhook_secret is None
    assert settings.billing_signature_tolerance == 5.0
    assert settings.garden_name == ""  # the app's own variable: the app's call, untouched


def test_the_variables_are_read_as_json_and_normalised(environment: pytest.MonkeyPatch) -> None:
    environment.setenv("GARDEN_BILLING_ENABLED", "true")
    environment.setenv("GARDEN_BILLING_PROVIDER", "lemonsqueezy")
    environment.setenv("GARDEN_BILLING_API_KEY", "ls_test_example")
    environment.setenv("GARDEN_BILLING_WEBHOOK_SECRET", "ls_signing_example")
    environment.setenv("GARDEN_BILLING_SIGNATURE_TOLERANCE", "60")
    environment.setenv("GARDEN_BILLING_LAUNCH_AT", "2026-11-01T00:00:00Z")
    environment.setenv("GARDEN_BILLING_PRICE_IDS", '{"Pro": {"chf": {"Year": 12345, "month": ["v_2", "v_1"]}}}')
    settings = _GardenSettings()
    assert settings.billing_enabled and settings.billing_provider is BillingProvider.LEMONSQUEEZY
    assert settings.billing_signature_tolerance == 60.0
    assert settings.billing_launch_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert settings.billing_price_id("pro", "CHF", "year") == "12345"
    assert settings.billing_price_ref("v_1") == PriceRef("pro", BillingCurrency.CHF, BillingInterval.MONTH)
    # An app that reads its own fields gets the same, typed already.
    assert BillingSettings.model_validate(settings, from_attributes=True).billing_price_ids == (
        settings.billing_price_ids
    )


@pytest.mark.parametrize("text", ["{", '{"pro": {"CHF": {"year": ""}}}', "[]"])
def test_price_ids_that_are_no_table_fail_at_start(environment: pytest.MonkeyPatch, text: str) -> None:
    environment.setenv("GARDEN_BILLING_PRICE_IDS", text)
    with pytest.raises(ValidationError, match="billing_price_ids"):
        _GardenSettings()
