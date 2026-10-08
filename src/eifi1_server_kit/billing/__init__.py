"""Billing, plans and payment, as code (``docs/billing-harmonization.md`` §10 in
``Eifi1/ui-kit``; its §12 wins over the sections above it).

Layer 1, like the rest of the kit: pure functions, Pydantic models and ports — no tables, no
routes, no provider SDK and no request to a provider. The subscription row and the event
table, the routes, the provider's API client (checkout, portal, cancellation), the read-only
gate's allow-list and the daily notice job stay in each app. Marcel's decisions (§2): all
three apps charge, through a Merchant of Record — Paddle or Lemon Squeezy, behind one
interface — in CHF and EUR; a lapsed payer is read-only, never locked; a new payer gets a
30-day trial without a card, a beta payer 12 free months.

* :mod:`~eifi1_server_kit.billing.settings` — :class:`BillingSettings`, the switch (off by
  default), the provider, its secrets and the price ids;
* :mod:`~eifi1_server_kit.billing.plans` — :class:`PlanSpec`, the catalogue, the limits
  (:func:`check_limit`), currencies, intervals and minor units;
* :mod:`~eifi1_server_kit.billing.standing` — the row's statuses and sources,
  :func:`in_good_standing`, and the trial's and the beta's dates
  (:func:`effective_comped_until` for a beta row stored before the launch date was known);
* :mod:`~eifi1_server_kit.billing.gate` — the read-only gate:
  :func:`billing_write_allowed` (the demo's shape, the standing as an input) and
  :func:`refuse_billing_read_only` for an app's own choke points;
* :mod:`~eifi1_server_kit.billing.signatures` — each provider's webhook signature,
  checked with ``hmac`` (no SDK);
* :mod:`~eifi1_server_kit.billing.events` — the normalised event vocabulary,
  :class:`NormalisedEvent`, and a mapper per provider;
* :mod:`~eifi1_server_kit.billing.webhooks` — the event-store port, the pure
  :func:`dispatch` with the ordering guard and the grant's precedence, and the answer
  policy;
* :mod:`~eifi1_server_kit.billing.schemas` — the wire shapes: status, overview, the plans
  (:class:`PlanOut`, :func:`plans_out`), checkout, the operator's plan change, and a sync
  reply's refused changes;
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
from eifi1_server_kit.billing.events import (
    LEMONSQUEEZY_EVENT_KINDS,
    LEMONSQUEEZY_STATUSES,
    PADDLE_EVENT_KINDS,
    PADDLE_STATUSES,
    PAYER_REF_KEY,
    WEBHOOK_MAPPERS,
    EventKind,
    NormalisedEvent,
    PoisonEventError,
    checkout_custom_data,
    map_lemonsqueezy_event,
    map_paddle_event,
    parse_webhook_event,
)
from eifi1_server_kit.billing.gate import billing_write_allowed, refuse_billing_read_only
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
from eifi1_server_kit.billing.schemas import (
    BillingOverview,
    BillingStatus,
    CheckoutAnswer,
    CheckoutRequest,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanOut,
    SyncRefusal,
    plans_out,
)
from eifi1_server_kit.billing.settings import BillingProvider, BillingSettings, PriceRef
from eifi1_server_kit.billing.signatures import (
    LEMONSQUEEZY_SIGNATURE_HEADER,
    PADDLE_SIGNATURE_HEADER,
    PADDLE_SIGNATURE_TOLERANCE,
    SIGNATURE_HEADERS,
    verify_lemonsqueezy_signature,
    verify_paddle_signature,
    verify_webhook_signature,
)
from eifi1_server_kit.billing.standing import (
    BETA_FREE_MONTHS,
    TRIAL_LENGTH,
    SubscriptionRow,
    SubscriptionSource,
    SubscriptionStatus,
    beta_comped_until,
    effective_comped_until,
    grant_holds,
    in_good_standing,
    is_beta,
    trial_ends_at,
)
from eifi1_server_kit.billing.webhooks import (
    DispatchOutcome,
    EventStore,
    WebhookAnswer,
    dispatch,
    row_changes,
    webhook_answer,
)

__all__ = [
    "BETA_FREE_MONTHS",
    "BILLING_ERROR_DETAIL",
    "BILLING_ERROR_STATUS",
    "CURRENCY_EXPONENTS",
    "LEMONSQUEEZY_EVENT_KINDS",
    "LEMONSQUEEZY_SIGNATURE_HEADER",
    "LEMONSQUEEZY_STATUSES",
    "PADDLE_EVENT_KINDS",
    "PADDLE_SIGNATURE_HEADER",
    "PADDLE_SIGNATURE_TOLERANCE",
    "PADDLE_STATUSES",
    "PAYER_REF_KEY",
    "PLAN_LIMIT_CODE",
    "SIGNATURE_HEADERS",
    "TRIAL_LENGTH",
    "WEBHOOK_MAPPERS",
    "BillingCurrency",
    "BillingError",
    "BillingErrorCode",
    "BillingInterval",
    "BillingOverview",
    "BillingProvider",
    "BillingSettings",
    "BillingStatus",
    "CheckoutAnswer",
    "CheckoutRequest",
    "Currency",
    "DispatchOutcome",
    "EventKind",
    "EventStore",
    "Interval",
    "MinorUnits",
    "NormalisedEvent",
    "PlanChangeRequest",
    "PlanChangeResponse",
    "PlanCode",
    "PlanLimitError",
    "PlanOut",
    "PlanSpec",
    "PoisonEventError",
    "PriceRef",
    "SubscriptionRow",
    "SubscriptionSource",
    "SubscriptionStatus",
    "SyncRefusal",
    "WebhookAnswer",
    "beta_comped_until",
    "billing_write_allowed",
    "check_limit",
    "checkout_custom_data",
    "dimensions_over_limit",
    "dispatch",
    "effective_comped_until",
    "grant_holds",
    "in_good_standing",
    "is_beta",
    "map_lemonsqueezy_event",
    "map_paddle_event",
    "minor_to_decimal",
    "normalize_plan",
    "parse_webhook_event",
    "plan_catalogue",
    "plans_out",
    "refuse_billing_read_only",
    "row_changes",
    "trial_ends_at",
    "verify_lemonsqueezy_signature",
    "verify_paddle_signature",
    "verify_webhook_signature",
    "webhook_answer",
]
