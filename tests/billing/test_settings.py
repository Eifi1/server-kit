"""Billing's settings (billing contract §3.1, §4, §10, §14.2–§14.4): off by default, on only
with the secrets (and with Paddle, the app's tag and an environment that agrees with the
key), the price ids both ways, the standing behind the switch, the checkout's pages, and
the webhook's check with the deployment's tolerance."""

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
    PaddleEnvironment,
    PriceRef,
    SubscriptionSource,
    SubscriptionStatus,
)
from eifi1_server_kit.billing.testing import sign_paddle
from tests.billing._rows import NOW, Row

SECRETS: dict[str, Any] = {
    "billing_provider": "paddle",
    "billing_api_key": "pdl_live_apikey_example",
    "billing_webhook_secret": "pdl_ntfset_example",
    "billing_app": "garden",
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


# --- Paddle: the app's tag, the environment, the pages (§14.2–§14.4) --------------------------


def test_switching_on_with_paddle_needs_the_apps_tag() -> None:
    """§14.3: every checkout carries the app, so the webhook can drop the others' events."""
    with pytest.raises(ValidationError, match="needs billing_app"):
        BillingSettings(billing_enabled=True, **{**SECRETS, "billing_app": None})
    with pytest.raises(ValidationError, match="needs billing_app"):
        BillingSettings(billing_enabled=True, **{**SECRETS, "billing_app": "   "})  # blank is unset
    for tag in ("Garden", "1garden", "gar den", "g" * 65):
        with pytest.raises(ValidationError, match="billing_app"):
            BillingSettings(billing_app=tag)
    assert BillingSettings(billing_app=" kurven-schmiede_2 ").billing_app == "kurven-schmiede_2"


def test_the_environment_is_read_from_the_keys_prefix() -> None:
    """Decision 24: the key says sandbox or live; the API's address is never a setting."""
    live = BillingSettings(billing_enabled=True, **SECRETS)
    assert live.billing_environment is None and live.billing_api_environment() is PaddleEnvironment.LIVE
    sandbox = BillingSettings(billing_enabled=True, **{**SECRETS, "billing_api_key": "pdl_sdbx_apikey_example"})
    assert sandbox.billing_api_environment() is PaddleEnvironment.SANDBOX
    named = BillingSettings(billing_enabled=True, billing_environment="live", **SECRETS)
    assert named.billing_api_environment() is PaddleEnvironment.LIVE


def test_a_legacy_key_needs_the_environment_named() -> None:
    """A key from before 2025-05-06 has no prefix."""
    legacy = {**SECRETS, "billing_api_key": "a" * 50}
    with pytest.raises(ValidationError, match="says neither sandbox nor live"):
        BillingSettings(billing_enabled=True, **legacy)
    assert (
        BillingSettings(billing_enabled=True, billing_environment="sandbox", **legacy).billing_api_environment()
        is PaddleEnvironment.SANDBOX
    )


def test_a_key_of_the_other_environment_fails_at_start() -> None:
    """A sandbox key works only against the sandbox: refused at start, not at the first checkout."""
    with pytest.raises(ValidationError, match="is a live key, but billing_environment is sandbox"):
        BillingSettings(billing_enabled=True, billing_environment="sandbox", **SECRETS)


def test_with_billing_off_an_unknown_environment_is_not_configured() -> None:
    """Switching on checks it; off, asking for it is the deployment's 503."""
    for settings in (
        BillingSettings(),
        BillingSettings(billing_provider="paddle", billing_api_key="legacy"),
        BillingSettings(billing_api_key="pdl_live_apikey_example", billing_environment="sandbox"),
    ):
        with pytest.raises(BillingError) as refused:
            settings.billing_api_environment()
        assert (refused.value.status_code, refused.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)
    assert BillingSettings(billing_environment="sandbox").billing_api_environment() is PaddleEnvironment.SANDBOX


def test_lemon_squeezy_needs_neither_tag_nor_environment() -> None:
    """Deprecated (§14.13), and nothing of §14 applies to it."""
    on = BillingSettings(
        billing_enabled=True,
        billing_provider="lemonsqueezy",
        billing_api_key="ls_example",
        billing_webhook_secret="ls_signing_example",
    )
    assert on.billing_app is None and on.billing_environment is None


@pytest.mark.parametrize(
    "url",
    ["https://pay.garden.example/", "http://localhost:8090/", "http://127.0.0.1/pay", "http://[::1]:5173/"],
)
def test_a_checkout_page_is_https_or_local(url: str) -> None:
    settings = BillingSettings(billing_checkout_page_url=f" {url} ", billing_hosted_checkout_url=url)
    assert settings.billing_checkout_page_url == url and settings.billing_hosted_checkout_url == url


@pytest.mark.parametrize(
    "url", ["http://pay.garden.example/", "ftp://pay.garden.example/", "https://", "pay.garden.example", "/pay"]
)
def test_a_checkout_page_elsewhere_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="https:// address"):
        BillingSettings(billing_checkout_page_url=url)
    with pytest.raises(ValidationError, match="https:// address"):
        BillingSettings(billing_hosted_checkout_url=url)


def test_the_webhook_is_checked_with_the_deployments_tolerance() -> None:
    """kastlan and Kurvenschmiede left ``tolerance=`` out, so Paddle's five seconds applied."""
    body = b'{"event_id":"evt_1"}'
    headers = sign_paddle(body, "pdl_ntfset_example", now=1_791_460_800)
    wide = BillingSettings(billing_signature_tolerance=60, billing_webhook_secret="pdl_ntfset_example")
    wide.verify_billing_webhook("paddle", body, headers, now=1_791_460_830)
    narrow = BillingSettings(billing_webhook_secret="pdl_ntfset_example")
    with pytest.raises(BillingError, match="outside the tolerance") as refused:
        narrow.verify_billing_webhook(BillingProvider.PADDLE, body, headers, now=1_791_460_830)
    assert refused.value.status_code == 400
    with pytest.raises(BillingError, match="No webhook secret"):
        BillingSettings().verify_billing_webhook("paddle", body, headers)


def test_the_new_variables_keep_their_names(environment: pytest.MonkeyPatch) -> None:
    """§14.2: ``<APP>_BILLING_APP``, ``_ENVIRONMENT``, ``_CHECKOUT_PAGE_URL`` and kastlan's
    ``_HOSTED_CHECKOUT_URL``."""
    environment.setenv("GARDEN_BILLING_APP", "garden")
    environment.setenv("GARDEN_BILLING_ENVIRONMENT", "sandbox")
    environment.setenv("GARDEN_BILLING_CHECKOUT_PAGE_URL", "https://pay.garden.example/")
    environment.setenv("GARDEN_BILLING_HOSTED_CHECKOUT_URL", "https://pay.paddle.io/checkout/hsc_01abc")
    settings = _GardenSettings()
    assert (settings.billing_app, settings.billing_environment) == ("garden", PaddleEnvironment.SANDBOX)
    assert settings.billing_checkout_page_url == "https://pay.garden.example/"
    assert settings.billing_hosted_checkout_url == "https://pay.paddle.io/checkout/hsc_01abc"
