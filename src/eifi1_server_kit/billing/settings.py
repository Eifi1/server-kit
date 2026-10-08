"""Billing's settings: the switch, the provider, its secrets, the price ids and the launch.

``docs/billing-harmonization.md`` §3.1, §4 and §10 in ``Eifi1/ui-kit``. A mixin like
:class:`~eifi1_server_kit.demo.DemoSettings`, for the app's ``Settings`` to inherit, so each
reads its environment variables there (``KEKSDOSE_BILLING_ENABLED``,
``KASTLAN_BILLING_WEBHOOK_SECRET``).

**Off by default** (§4). While off, ``GET /billing/status`` answers ``{billing_enabled:
false}`` and nothing else of billing exists: every other billing route and the webhook
answer 404 ``billing_disabled`` (:meth:`BillingSettings.require_billing_enabled`), and the
read-only gate is off — everyone is in good standing
(:meth:`BillingSettings.billing_standing`). **Switching on requires the secrets** (§10): a
deployment with the switch on and a secret missing fails at start, not at its first
webhook.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from typing import Annotated, NamedTuple, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.plans import (
    BillingCurrency,
    BillingInterval,
    Currency,
    Interval,
    PlanCode,
    normalize_plan,
)
from eifi1_server_kit.billing.standing import SubscriptionRow, in_good_standing

__all__ = ["BillingProvider", "BillingSettings", "PriceRef"]


class BillingProvider(enum.StrEnum):
    """The Merchants of Record the kit speaks (§2.2, §9). Marcel has not chosen yet, so both
    sit behind one interface: choosing changes the settings, not the app."""

    PADDLE = "paddle"
    LEMONSQUEEZY = "lemonsqueezy"


class PriceRef(NamedTuple):
    """What a provider's price id stands for: one plan, in one currency, per one interval."""

    plan: str
    currency: BillingCurrency
    interval: BillingInterval


def _price_ids(value: object) -> object:
    """One id or a list of them; Lemon Squeezy's variant ids arrive as numbers in JSON."""
    if isinstance(value, str | int) and not isinstance(value, bool):
        value = [value]
    if isinstance(value, list):
        return [str(item) if isinstance(item, int) and not isinstance(item, bool) else item for item in value]
    return value


_PriceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
#: The current price id first, then any retired ones still running subscriptions.
_PriceIds = Annotated[list[_PriceId], BeforeValidator(_price_ids), Field(min_length=1)]


class BillingSettings(BaseModel):
    """Billing's settings (§4, §10), named for ``<APP>_BILLING_*``.

    Meant to be INHERITED by the app's settings::

        class Settings(BaseSettings, DemoSettings, BillingSettings):
            model_config = SettingsConfigDict(env_prefix="KEKSDOSE_")

    An app that keeps its own fields builds one from them instead:
    ``BillingSettings.model_validate(settings, from_attributes=True)``.

    ``billing_price_ids`` is plan → currency → interval → the provider's price id (§3.1;
    Paddle's ``pri_…``, Lemon Squeezy's variant id), JSON in the environment::

        KEKSDOSE_BILLING_PRICE_IDS='{"pro": {"CHF": {"year": "pri_01h…", "month": "pri_01j…"}}}'

    A combination may hold a LIST instead: the current id first — what a new checkout uses
    — then retired ids whose subscriptions still run, so their webhooks still find the plan
    (``["pri_new", "pri_old"]``). An id may stand for one combination only.
    """

    model_config = ConfigDict(from_attributes=True)

    #: Off: billing doesn't exist for the app (§4). On: ``billing_provider``,
    #: ``billing_api_key`` and ``billing_webhook_secret`` must be set.
    billing_enabled: bool = False
    #: The Merchant of Record this deployment sells through (§9).
    billing_provider: BillingProvider | None = None
    #: The provider API key the app's client creates checkouts and portal sessions with
    #: (§4: hosted checkout and portal only). The kit never sends a request; it only
    #: refuses to switch on without it.
    billing_api_key: SecretStr | None = None
    #: The webhook's signing secret (§5): Paddle's endpoint secret key (``pdl_ntfset_…``),
    #: Lemon Squeezy's signing secret.
    billing_webhook_secret: SecretStr | None = None
    #: Plan → currency → interval → price id(s); see the class docstring.
    billing_price_ids: dict[PlanCode, dict[Currency, dict[Interval, _PriceIds]]] = Field(default_factory=dict)
    #: When billing went on for this app (§2.4): the beta's 12 months run from it
    #: (:func:`~eifi1_server_kit.billing.beta_comped_until`), and an invitation created
    #: before it makes a beta payer (:func:`~eifi1_server_kit.billing.is_beta`). Unset until
    #: Marcel names the date.
    billing_launch_at: datetime | None = None

    @model_validator(mode="after")
    def _switching_on_needs_the_secrets(self) -> Self:
        if self.billing_enabled:
            missing = [
                name
                for name, value in (
                    ("billing_provider", self.billing_provider),
                    ("billing_api_key", self.billing_api_key),
                    ("billing_webhook_secret", self.billing_webhook_secret),
                )
                if value is None or (isinstance(value, SecretStr) and not value.get_secret_value().strip())
            ]
            if missing:
                raise ValueError(f"billing_enabled needs {', '.join(missing)}")
        self._price_index()  # an id standing for two combinations is refused here, at start
        return self

    def _price_index(self) -> dict[str, PriceRef]:
        index: dict[str, PriceRef] = {}
        for plan, currencies in self.billing_price_ids.items():
            for currency, intervals in currencies.items():
                for interval, ids in intervals.items():
                    for price_id in ids:
                        ref = PriceRef(plan, currency, interval)
                        if index.setdefault(price_id, ref) != ref:
                            raise ValueError(f"the price id {price_id!r} stands for two plans or periods")
        return index

    def require_billing_enabled(self) -> None:
        """Refuse when the switch is off: 404 ``billing_disabled`` (§2.9). First thing in
        every billing route but ``GET /billing/status`` (which answers a demo user too,
        §12.19), and in the webhook."""
        if not self.billing_enabled:
            raise BillingError(BillingErrorCode.BILLING_DISABLED)

    def billing_price_id(self, plan: str, currency: BillingCurrency | str, interval: BillingInterval | str) -> str:
        """The provider's CURRENT price id for a checkout (§4 ``POST /billing/checkout``).

        Missing — the plan isn't sold that way, or the setting lacks it — is 503
        ``billing_not_configured``: check the plan's own ``prices`` first for a 422 on a
        combination the catalogue doesn't sell.
        """
        key = normalize_plan(plan)
        try:
            return self.billing_price_ids[key][BillingCurrency(currency.strip().upper())][
                BillingInterval(interval.strip().lower())
            ][0]
        except KeyError, ValueError:
            raise BillingError(
                BillingErrorCode.BILLING_NOT_CONFIGURED, f"No price id for {key}, {currency}, {interval}"
            ) from None

    def billing_price_ref(self, price_id: str) -> PriceRef | None:
        """What a provider's price id stands for — a webhook's plan — or ``None`` for an id
        the settings don't name, current or retired."""
        return self._price_index().get(price_id)

    def billing_plan_for_price(self, price_id: str) -> str | None:
        """The plan code a provider's price id stands for, or ``None``: the ``plan_for_price``
        :func:`~eifi1_server_kit.billing.dispatch` takes."""
        ref = self.billing_price_ref(price_id)
        return None if ref is None else ref.plan

    def billing_webhook_key(self) -> str:
        """The webhook secret's value, for the signature check (§5); 503
        ``billing_not_configured`` without one (only reachable with the switch off)."""
        if self.billing_webhook_secret is None:
            raise BillingError(BillingErrorCode.BILLING_NOT_CONFIGURED, "No webhook secret")
        return self.billing_webhook_secret.get_secret_value()

    def billing_standing(
        self, row: SubscriptionRow | None, now: datetime, *, retry_grace: timedelta | None = None
    ) -> bool:
        """:func:`~eifi1_server_kit.billing.in_good_standing`, behind the switch: with
        billing off everyone is in good standing (§4). The standing the read-only gate
        takes (:func:`~eifi1_server_kit.billing.billing_write_allowed`)."""
        return not self.billing_enabled or in_good_standing(row, now, retry_grace=retry_grace)
