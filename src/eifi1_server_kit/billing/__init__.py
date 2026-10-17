"""Billing, plans and payment, as code (``docs/billing-harmonization.md`` §10 and §14 in
``Eifi1/ui-kit``; since ui-kit 0.34 the contract's body carries every settled amendment).

Layer 1, like the rest of the kit: pure functions, Pydantic models and ports — no tables and
no routes, and one request each for checkout, portal, cancel, undoing a cancel, a plan
switch's preview and the switch, and the shared customer (§15), through
:mod:`~eifi1_server_kit.billing.paddle`, only with the ``billing`` extra
(``eifi1-server-kit[billing]``, httpx; imported lazily, so importing this package never
needs it). The subscription row and the event table, the routes, the read-only gate's
allow-list, the notice job's query, mail and trigger, and the provider seam stay in each
app. Marcel's decisions (§2): all three apps charge, through a Merchant of Record —
Paddle (decision 16), one account for the three apps, each checkout tagged with its app
(decision 18) — in CHF and EUR; a lapsed payer is read-only, never locked; a new payer gets
a 30-day trial without a card, a beta payer 12 free months. Lemon Squeezy, deprecated in
0.7, went in 0.8 (decision 25).

* :mod:`~eifi1_server_kit.billing.settings` — :class:`BillingSettings`, the switch (off by
  default), the provider, its secrets, the price ids, the app's tag, Paddle's environment
  and where the checkout opens;
* :mod:`~eifi1_server_kit.billing.plans` — :class:`PlanSpec`, the catalogue, the limits
  (:func:`check_limit`), currencies, intervals and minor units;
* :mod:`~eifi1_server_kit.billing.standing` — the row's statuses and sources,
  :func:`in_good_standing`, and the trial's and the beta's dates
  (:func:`effective_comped_until` for a beta row stored before the launch date was known);
* :mod:`~eifi1_server_kit.billing.gate` — the read-only gate:
  :func:`billing_write_allowed` (the demo's shape, the standing as an input) and
  :func:`refuse_billing_read_only` for an app's own choke points;
* :mod:`~eifi1_server_kit.billing.signatures` — the provider's webhook signature,
  checked with ``hmac`` (no SDK);
* :mod:`~eifi1_server_kit.billing.events` — the normalised event vocabulary,
  :class:`NormalisedEvent`, a mapper per provider, and the app's tag that drops another
  app's events (:func:`parse_webhook_event`'s ``app``);
* :mod:`~eifi1_server_kit.billing.provider` — the provider's port
  (:class:`BillingProviderClient`), the deployment's client
  (:func:`billing_provider_client`), and what a route checks first (:func:`sold_plan`,
  :func:`require_new_checkout`; for a plan switch :func:`require_plan_switch` and
  :func:`switch_price`; the rule both refuse by, :func:`provider_subscription_runs`); a deletion's cancellation and its undoing
  (:func:`cancel_for_deletion`, :func:`resume_after_withdrawal`); the portal for the buyer
  only (:func:`portal_access`, :func:`require_portal_access`) and the in-app cancel and its
  undoing (:func:`cancel_at_period_end`, :func:`resume_scheduled_cancel`); the checkout's way back
  (:func:`checkout_return_url`);
* :mod:`~eifi1_server_kit.billing.paddle` — :class:`PaddleClient`, Paddle Billing's API
  (the ``billing`` extra), :class:`PaddleError` (the 4xx codes that are the
  deployment's fault, :data:`PADDLE_CONFIGURATION_CODES`), and the buyer's language as Paddle's
  checkout names it (:func:`paddle_locale`);
* :mod:`~eifi1_server_kit.billing.notices` — which payer is owed a trial or grant end
  notice (:func:`billing_notice_due`);
* :mod:`~eifi1_server_kit.billing.webhooks` — the event-store port, the pure
  :func:`dispatch` with the ordering guard and the grant's precedence, and the answer
  policy;
* :mod:`~eifi1_server_kit.billing.schemas` — the wire shapes: status, overview, the
  standing (:class:`BillingStanding`), the plans
  (:class:`PlanOut` with :class:`PlanPrices`, :func:`plans_out`), checkout, the operator's plans
  (:class:`AdminPlan`, :func:`admin_plans_out`) and plan change, the payer's plan switch
  (:class:`PlanSwitchRequest`, :class:`PlanSwitchPreview`, :func:`plan_switch_preview`) and
  what the payer pays (:class:`PaidPrice`, the overview's ``paid``), and a sync reply's
  refused changes;
* :mod:`~eifi1_server_kit.billing.errors` — :class:`BillingError` and
  :class:`PlanLimitError`, both answered by
  :func:`~eifi1_server_kit.errors.install_contract_error_handlers`.

Not re-exported: :mod:`eifi1_server_kit.billing.testing`, the signed fixture events and the
provider fakes for the apps' tests and local CLIs (§14.9).
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
    APP_KEY,
    BUYER_KEY,
    PADDLE_EVENT_KINDS,
    PADDLE_STATUSES,
    PAYER_REF_KEY,
    WEBHOOK_MAPPERS,
    EventKind,
    NormalisedEvent,
    PoisonEventError,
    checkout_custom_data,
    map_paddle_event,
    parse_webhook_event,
)
from eifi1_server_kit.billing.gate import billing_write_allowed, refuse_billing_read_only
from eifi1_server_kit.billing.notices import (
    NOTICE_AHEAD,
    NOTICE_LATE_LIMIT,
    NOTICE_STATUSES,
    BillingNotice,
    BillingNoticeKind,
    NoticeCandidate,
    OwedNotice,
    billing_notice_due,
    billing_notices_owed,
)
from eifi1_server_kit.billing.paddle import (
    PADDLE_API_BASES,
    PADDLE_API_VERSION,
    PADDLE_CONFIGURATION_CODES,
    PADDLE_TIMEOUT_SECONDS,
    PaddleClient,
    PaddleError,
    paddle_environment_of,
    paddle_locale,
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
from eifi1_server_kit.billing.provider import (
    CHECKOUT_RETURN_PARAM,
    CHECKOUT_RETURN_VALUE,
    BillingProviderClient,
    CancelOutcome,
    NoProviderClient,
    PortalAccess,
    PortalTarget,
    ResumeOutcome,
    billing_provider_client,
    cancel_at_period_end,
    cancel_for_deletion,
    checkout_return_url,
    portal_access,
    provider_subscription_runs,
    require_new_checkout,
    require_plan_switch,
    require_portal_access,
    resume_after_withdrawal,
    resume_scheduled_cancel,
    sold_plan,
    switch_price,
)
from eifi1_server_kit.billing.schemas import (
    AdminPlan,
    BillingOverview,
    BillingStanding,
    BillingStatus,
    CheckoutAnswer,
    CheckoutRequest,
    PaidPrice,
    PlanChangeRequest,
    PlanChangeResponse,
    PlanIntervalPrices,
    PlanOut,
    PlanPrices,
    PlanSwitchPreview,
    PlanSwitchRequest,
    PortalRequest,
    ProviderSwitchPreview,
    SyncRefusal,
    admin_plans_out,
    plan_switch_preview,
    plans_out,
)
from eifi1_server_kit.billing.settings import BillingProvider, BillingSettings, PaddleEnvironment, PriceRef
from eifi1_server_kit.billing.signatures import (
    PADDLE_SIGNATURE_HEADER,
    PADDLE_SIGNATURE_TOLERANCE,
    SIGNATURE_HEADERS,
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
    DuplicateEventError,
    EventStore,
    WebhookAnswer,
    dispatch,
    row_changes,
    webhook_answer,
)

__all__ = [
    "APP_KEY",
    "BETA_FREE_MONTHS",
    "BILLING_ERROR_DETAIL",
    "BILLING_ERROR_STATUS",
    "BUYER_KEY",
    "CHECKOUT_RETURN_PARAM",
    "CHECKOUT_RETURN_VALUE",
    "CURRENCY_EXPONENTS",
    "NOTICE_AHEAD",
    "NOTICE_LATE_LIMIT",
    "NOTICE_STATUSES",
    "PADDLE_API_BASES",
    "PADDLE_API_VERSION",
    "PADDLE_CONFIGURATION_CODES",
    "PADDLE_EVENT_KINDS",
    "PADDLE_SIGNATURE_HEADER",
    "PADDLE_SIGNATURE_TOLERANCE",
    "PADDLE_STATUSES",
    "PADDLE_TIMEOUT_SECONDS",
    "PAYER_REF_KEY",
    "PLAN_LIMIT_CODE",
    "SIGNATURE_HEADERS",
    "TRIAL_LENGTH",
    "WEBHOOK_MAPPERS",
    "AdminPlan",
    "BillingCurrency",
    "BillingError",
    "BillingErrorCode",
    "BillingInterval",
    "BillingNotice",
    "BillingNoticeKind",
    "BillingOverview",
    "BillingProvider",
    "BillingProviderClient",
    "BillingSettings",
    "BillingStanding",
    "BillingStatus",
    "CancelOutcome",
    "CheckoutAnswer",
    "CheckoutRequest",
    "Currency",
    "DispatchOutcome",
    "DuplicateEventError",
    "EventKind",
    "EventStore",
    "Interval",
    "MinorUnits",
    "NoProviderClient",
    "NormalisedEvent",
    "NoticeCandidate",
    "OwedNotice",
    "PaddleClient",
    "PaddleEnvironment",
    "PaddleError",
    "PaidPrice",
    "PlanChangeRequest",
    "PlanChangeResponse",
    "PlanCode",
    "PlanIntervalPrices",
    "PlanLimitError",
    "PlanOut",
    "PlanPrices",
    "PlanSpec",
    "PlanSwitchPreview",
    "PlanSwitchRequest",
    "PoisonEventError",
    "PortalAccess",
    "PortalRequest",
    "PortalTarget",
    "PriceRef",
    "ProviderSwitchPreview",
    "ResumeOutcome",
    "SubscriptionRow",
    "SubscriptionSource",
    "SubscriptionStatus",
    "SyncRefusal",
    "WebhookAnswer",
    "admin_plans_out",
    "beta_comped_until",
    "billing_notice_due",
    "billing_notices_owed",
    "billing_provider_client",
    "billing_write_allowed",
    "cancel_at_period_end",
    "cancel_for_deletion",
    "check_limit",
    "checkout_custom_data",
    "checkout_return_url",
    "dimensions_over_limit",
    "dispatch",
    "effective_comped_until",
    "grant_holds",
    "in_good_standing",
    "is_beta",
    "map_paddle_event",
    "minor_to_decimal",
    "normalize_plan",
    "paddle_environment_of",
    "paddle_locale",
    "parse_webhook_event",
    "plan_catalogue",
    "plan_switch_preview",
    "plans_out",
    "portal_access",
    "provider_subscription_runs",
    "refuse_billing_read_only",
    "require_new_checkout",
    "require_plan_switch",
    "require_portal_access",
    "resume_after_withdrawal",
    "resume_scheduled_cancel",
    "row_changes",
    "sold_plan",
    "switch_price",
    "trial_ends_at",
    "verify_paddle_signature",
    "verify_webhook_signature",
    "webhook_answer",
]
