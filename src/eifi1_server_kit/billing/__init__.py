"""Billing, plans and payment, as code (``docs/billing-harmonization.md`` §10 in
``Eifi1/ui-kit``; its §12 wins over the sections above it).

Layer 1, like the rest of the kit: pure functions, Pydantic models and ports — no tables, no
routes, no provider SDK and no request to a provider. The subscription row and the event
table, the routes, the provider's API client (checkout, portal, cancellation), the read-only
gate's allow-list and the daily notice job stay in each app. Marcel's decisions (§2): all
three apps charge, through a Merchant of Record — Paddle or Lemon Squeezy, behind one
interface — in CHF and EUR; a lapsed payer is read-only, never locked; a new payer gets a
30-day trial without a card, a beta payer 12 free months.

* :mod:`~eifi1_server_kit.billing.plans` — :class:`PlanSpec`, the catalogue, the limits
  (:func:`check_limit`), currencies, intervals and minor units;
* :mod:`~eifi1_server_kit.billing.errors` — :class:`BillingError` and
  :class:`PlanLimitError`, both answered by
  :func:`~eifi1_server_kit.errors.install_contract_error_handlers`.
"""

from __future__ import annotations

from eifi1_server_kit.billing.errors import (
    BILLING_ERROR_DETAIL,
    BILLING_ERROR_STATUS,
    PLAN_LIMIT_CODE,
    BillingError,
    BillingErrorCode,
    PlanLimitError,
)
from eifi1_server_kit.billing.plans import (
    CURRENCY_EXPONENTS,
    BillingCurrency,
    BillingInterval,
    Currency,
    Interval,
    MinorUnits,
    PlanCode,
    PlanSpec,
    check_limit,
    dimensions_over_limit,
    minor_to_decimal,
    normalize_plan,
    plan_catalogue,
)

__all__ = [
    "BILLING_ERROR_DETAIL",
    "BILLING_ERROR_STATUS",
    "CURRENCY_EXPONENTS",
    "PLAN_LIMIT_CODE",
    "BillingCurrency",
    "BillingError",
    "BillingErrorCode",
    "BillingInterval",
    "Currency",
    "Interval",
    "MinorUnits",
    "PlanCode",
    "PlanLimitError",
    "PlanSpec",
    "check_limit",
    "dimensions_over_limit",
    "minor_to_decimal",
    "normalize_plan",
    "plan_catalogue",
]
