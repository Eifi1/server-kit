"""Billing's wire shapes (``docs/billing-harmonization.md`` §3.1, §4, §6, §12.5, §14.5,
§14.12, §15.2.10 and §16 in ``Eifi1/ui-kit``).

Like the other kit schemas, meant to be used as they are or SUBCLASSED: the app adds its
fields and narrows a type. The request bodies refuse unknown fields (``extra="forbid"``): a
``price`` or a ``status`` sent by a client is a 422, never silently dropped — the price
comes from the settings, the status from the provider. Money is never on these shapes as a
float: a plan's prices are integer minor units (§4), formatted by the kit's
``formatMoney``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Self, TypedDict

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, with_config

from eifi1_server_kit.auth.schemas import UtcDateTime
from eifi1_server_kit.billing.errors import BILLING_ERROR_DETAIL, BillingErrorCode
from eifi1_server_kit.billing.plans import (
    BillingCurrency,
    BillingInterval,
    Currency,
    Interval,
    MinorUnits,
    PlanCode,
    PlanSpec,
    dimensions_over_limit,
    normalize_plan,
)
from eifi1_server_kit.billing.provider import PortalAccess, PortalTarget, portal_access, provider_subscription_runs
from eifi1_server_kit.billing.settings import PriceRef
from eifi1_server_kit.billing.standing import (
    SubscriptionRow,
    SubscriptionSource,
    SubscriptionStatus,
    effective_comped_until,
)
from eifi1_server_kit.billing.standing import in_good_standing as _in_good_standing

__all__ = [
    "AdminPlan",
    "BillingOverview",
    "BillingStanding",
    "BillingStatus",
    "CheckoutAnswer",
    "CheckoutRequest",
    "PaidPrice",
    "PlanChangeRequest",
    "PlanChangeResponse",
    "PlanIntervalPrices",
    "PlanOut",
    "PlanPrices",
    "PlanSwitchPreview",
    "PlanSwitchRequest",
    "PortalRequest",
    "ProviderSwitchPreview",
    "SyncRefusal",
    "admin_plans_out",
    "plan_switch_preview",
    "plans_out",
]


class BillingStatus(BaseModel):
    """``GET /billing/status`` → ``{billing_enabled}`` (§4): the one billing route that
    answers while billing is off — and to a demo user (§12.19), so the shell can hide the
    billing page, its nav entry, banners and search entries."""

    billing_enabled: bool


class PaidPrice(BaseModel):
    """What the payer pays at the provider (§15.2.10): ``{plan, interval, currency}`` — the
    row's ``provider_price_id`` as the settings name it
    (:meth:`~eifi1_server_kit.billing.BillingSettings.billing_price_ref`). The overview's
    ``paid``. Under a grant the row keeps the grant's plan, so this is what shows that a
    plan switch landed, and ui-kit's ``checkoutFingerprint`` reads its plan and interval."""

    plan: PlanCode
    interval: Interval
    currency: Currency


class BillingOverview(BaseModel):
    """``GET /billing/overview`` (§4), for the payer (kastlan: a company admin): where the
    subscription stands, what the plan allows and what is used.

    Only ever the payer's own: a guest in someone else's budget reads the item's ``locked:
    "billing" | null`` instead, never the owner's status (§12.6). Build it with
    :meth:`from_row`.
    """

    #: The plan's code (lowercase, §3.1).
    plan: str
    status: SubscriptionStatus
    source: SubscriptionSource
    #: :func:`~eifi1_server_kit.billing.in_good_standing` now: false = read-only (§3.3).
    in_good_standing: bool
    trial_ends_at: UtcDateTime | None = None
    #: When the free grant ends (:func:`~eifi1_server_kit.billing.effective_comped_until`):
    #: a beta row stored without an end shows the launch plus 12 months, the banner's
    #: "free until …" (§3.2); ``None`` is no end.
    comped_until: UtcDateTime | None = None
    current_period_end: UtcDateTime | None = None
    cancel_at_period_end: bool = False
    #: The plan's limits, dimension → number, ``None`` for unlimited.
    limits: dict[str, int | None]
    #: What the payer has, per dimension (§12.15).
    usage: dict[str, int]
    #: The payer's currency (§4, §12.18): the subscription's, else the account's stored
    #: currency (keksdose ``reporting_currency``), else the locale's.
    currency: BillingCurrency
    #: The provider has a customer for this payer (§14.5): the row's
    #: ``provider_customer_id`` is set, whatever the status or the source. False: the page
    #: shows a caption in place of "Payment and invoices" and "Cancel subscription", and
    #: never asks for the portal (which would answer 409 ``billing_not_at_provider``).
    #: True: both show — a payer who bought under a grant keeps the way to the invoices and
    #: to "Cancel" (§12.26).
    at_provider: bool = False
    #: The price the row says the payer pays (§15.2.10), from its ``provider_price_id``;
    #: ``None`` with no price yet (no provider snapshot), one the settings don't name, or
    #: without ``price_ref``. It stays after the subscription ended: ``status`` says that.
    paid: PaidPrice | None = None
    #: A provider subscription runs (§15.2.10): a plan pick goes to the plan switch, not to
    #: the checkout. :func:`~eifi1_server_kit.billing.provider_subscription_runs` as of
    #: ``now`` — the rule :func:`~eifi1_server_kit.billing.require_new_checkout` and
    #: :func:`~eifi1_server_kit.billing.require_plan_switch` refuse by, so the page and the
    #: two routes never disagree. False by default: an app's own overview built without
    #: :meth:`from_row` keeps building.
    switching: bool = False
    #: Who may open the portal (§16.2, §16.3): ``open``, or ``buyer_only`` — the row stores
    #: a buyer (kastlan's ``provider_payer_user_id``) and the viewer isn't them, so "Payment
    #: and invoices" becomes a caption naming :attr:`buyer_name` and "Cancel subscription"
    #: cancels in the app (``POST /billing/cancel``). :func:`~eifi1_server_kit.billing.portal_access`.
    portal: PortalAccess = "open"
    #: The buyer's display name for that caption, only with ``buyer_only`` (§16.3, §16.7
    #: G): the app looks it up — kastlan: first and last name — and passes ``None`` once
    #: the buyer is gone or no longer a member; the caption then names "the admin who
    #: bought the subscription".
    buyer_name: str | None = None

    @classmethod
    def from_row(
        cls,
        row: SubscriptionRow,
        *,
        plan: PlanSpec,
        usage: Mapping[str, int],
        currency: BillingCurrency | str,
        now: datetime,
        retry_grace: timedelta | None = None,
        launch: datetime | None = None,
        price_ref: Callable[[str], PriceRef | None] | None = None,
        viewer_is_buyer: bool | None = None,
        buyer_name: str | None = None,
    ) -> Self:
        """The overview of ``row``, whose plan is ``plan`` (the catalogue's entry for
        ``row.plan_code`` — another plan is a :class:`ValueError`), with the payer's
        ``usage`` and ``currency``, its standing as of ``now``.

        Pass ``launch=settings.billing_launch_at``: a beta row stored before the launch
        date was known has no ``comped_until``, and both the standing and the
        ``comped_until`` shown read it as the launch plus 12 months (§3.2).

        Pass ``price_ref=settings.billing_price_ref`` for ``paid`` (§15.2.10): the row's
        ``provider_price_id`` as a plan, interval and currency.

        ``switching`` is :func:`~eifi1_server_kit.billing.provider_subscription_runs`
        ``(row, now)``.

        Pass ``viewer_is_buyer`` where the row stores a buyer (kastlan: ``row.
        provider_payer_user_id == user.id``) and the buyer's ``buyer_name`` (§16.3):
        ``portal`` is :func:`~eifi1_server_kit.billing.portal_access`, and ``buyer_name``
        is kept only with ``buyer_only``. A person-payer app passes neither: ``open``."""
        if normalize_plan(row.plan_code) != plan.code:
            raise ValueError(f"the row's plan is {row.plan_code!r}, not {plan.code!r}")
        ref = price_ref(row.provider_price_id) if price_ref is not None and row.provider_price_id else None
        return cls(
            plan=plan.code,
            status=SubscriptionStatus(row.status),
            source=SubscriptionSource(row.source),
            in_good_standing=_in_good_standing(row, now, retry_grace=retry_grace, launch=launch),
            trial_ends_at=row.trial_ends_at,
            comped_until=effective_comped_until(row, launch),
            current_period_end=row.current_period_end,
            cancel_at_period_end=bool(row.cancel_at_period_end),
            limits=dict(plan.limits),
            usage=dict(usage),
            currency=BillingCurrency(str(currency).strip().upper()),
            at_provider=row.provider_customer_id is not None,
            paid=None if ref is None else PaidPrice(plan=ref.plan, interval=ref.interval, currency=ref.currency),
            switching=provider_subscription_runs(row, now),
            **_portal_fields(row, viewer_is_buyer, buyer_name),
        )


def _portal_fields(row: SubscriptionRow, viewer_is_buyer: bool | None, buyer_name: str | None) -> _PortalFields:
    access = portal_access(row, viewer_is_buyer=viewer_is_buyer)
    return {"portal": access, "buyer_name": buyer_name if access == "buyer_only" else None}


class _PortalFields(TypedDict):
    portal: PortalAccess
    buyer_name: str | None


class BillingStanding(BaseModel):
    """The payer's standing as every member's write lock and banners read it (§3.3,
    §12.5, §16.7 A) — ui-kit's ``BillingStanding`` — with the portal's ``portal`` and
    ``buyer_name`` as the overview has them. For an app whose ``GET /billing/status``
    carries the standing beside ``billing_enabled`` (kastlan's ``standing``), where the
    payment-failed banner reads it: with ``buyer_only`` the banner names the buyer and
    shows no button, so no request ends in the 403. Never the provider's details: those
    are the overview's. Build it with :meth:`from_row`::

        class AppBillingStatus(BillingStatus):
            standing: BillingStanding | None = None          # null while off, and for a demo
    """

    status: SubscriptionStatus
    #: :func:`~eifi1_server_kit.billing.in_good_standing` now: false = read-only (§3.3).
    in_good_standing: bool
    trial_ends_at: UtcDateTime | None = None
    #: :func:`~eifi1_server_kit.billing.effective_comped_until`, as on the overview.
    comped_until: UtcDateTime | None = None
    #: As :attr:`BillingOverview.portal`.
    portal: PortalAccess = "open"
    #: As :attr:`BillingOverview.buyer_name`: only with ``buyer_only``.
    buyer_name: str | None = None

    @classmethod
    def from_row(
        cls,
        row: SubscriptionRow,
        *,
        now: datetime,
        retry_grace: timedelta | None = None,
        launch: datetime | None = None,
        viewer_is_buyer: bool | None = None,
        buyer_name: str | None = None,
    ) -> Self:
        """The standing of ``row`` as of ``now``, with :meth:`BillingOverview.from_row`'s
        ``retry_grace``, ``launch``, ``viewer_is_buyer`` and ``buyer_name``."""
        return cls(
            status=SubscriptionStatus(row.status),
            in_good_standing=_in_good_standing(row, now, retry_grace=retry_grace, launch=launch),
            trial_ends_at=row.trial_ends_at,
            comped_until=effective_comped_until(row, launch),
            **_portal_fields(row, viewer_is_buyer, buyer_name),
        )


@with_config(ConfigDict(extra="forbid"))
class PlanIntervalPrices(TypedDict, total=False):
    """One currency's prices on the wire: ``{month?, year?}``, GROSS minor units (§4,
    §12.17). A period the plan isn't sold for is ABSENT, never ``0`` or ``null``.

    A ``TypedDict`` with a key per :class:`~eifi1_server_kit.billing.BillingInterval`, not
    a ``dict`` keyed by the enum: OpenAPI's ``propertyNames`` is dropped by
    openapi-typescript (7.13), which then generates ``{[key: string]: number}`` — not
    assignable to ui-kit's ``PlanIntervalPrices`` without a cast (keksdose's 0.32 report).
    Named keys generate ``{month?: number; year?: number}``, ui-kit's type as it is.
    """

    month: MinorUnits
    year: MinorUnits


@with_config(ConfigDict(extra="forbid"))
class PlanPrices(TypedDict, total=False):
    """A plan's prices on the wire: ``{CHF?, EUR?}``, each a :class:`PlanIntervalPrices`
    — ui-kit's ``PlanPrices``, which ``BillingPlan.prices`` takes as it is (§3.1, §4). A
    currency the plan isn't sold in is ABSENT; a plan sold in none is ``{}``, which the
    page reads as free. A key per
    :class:`~eifi1_server_kit.billing.BillingCurrency`, for the reason
    :class:`PlanIntervalPrices` gives; any other key is refused.
    """

    CHF: PlanIntervalPrices
    EUR: PlanIntervalPrices


class PlanOut(BaseModel):
    """One plan of ``GET /billing/plans`` (§4): ``{code, prices, limits, sort}``, the
    catalogue's :class:`~eifi1_server_kit.billing.PlanSpec` as JSON can carry it. Build the
    list with :func:`plans_out`.

    **``prices`` is nested, currency → interval → GROSS minor units** (§4, §12.17)::

        {"code": "standard",
         "prices": {"CHF": {"month": 7900, "year": 79000}, "EUR": {"month": 7900, "year": 79000}},
         "limits": {"units": 150, "seats": 5, "storage": 26843545600},
         "sort": 1}

    ``PlanSpec.prices`` is keyed by ``(currency, interval)``, and a tuple is no JSON key, so
    each app invented its own list and converted it in the page (kastlan's ``[{currency,
    interval, amount}]``, its 0.32 report). This is ui-kit's ``PlanPrices``
    (:class:`PlanPrices`, with named keys so the generated TypeScript is that type too), so
    ``BillingPlan.prices`` takes it as it is. A combination the plan doesn't sell is
    ABSENT, never ``0``: the page reads ``0`` — and a plan with no prices at all, ``{}`` —
    as free. ``limits`` is the plan's, ``null`` for unlimited (kastlan's ``storage`` in
    bytes, §13.1); ``sort`` the catalogue's order, cheapest first, which decides the page's
    upgrade and downgrade.

    **No name, description or feature lines**: those are the app's i18n, never the
    provider's or the server's (§3.1). The page maps ``code`` to its own words and builds
    ui-kit's ``BillingPlan`` from both.

    A RETIRED PRICE needs nothing here: its id stays findable in the settings for the
    subscriptions still on it (:class:`~eifi1_server_kit.billing.BillingSettings`), and
    this shape carries amounts, not price ids. A plan sold no more stays in the catalogue
    for the rows on it — its limits still gate creation (§3.4) — and the page marks it
    ``disabled`` or the route leaves it out; it is the app's word, not the catalogue's.
    """

    code: PlanCode
    prices: PlanPrices
    limits: dict[str, int | None]
    sort: int

    @classmethod
    def from_spec(cls, plan: PlanSpec) -> Self:
        """``plan`` on the wire: its prices nested, CHF before EUR and month before year
        whatever order the spec lists them in (:class:`PlanPrices`' order), so the JSON is
        the same on every run."""
        prices: dict[str, dict[str, int]] = {}
        for (currency, interval), amount in plan.prices.items():
            prices.setdefault(currency.value, {})[interval.value] = amount
        return cls(code=plan.code, prices=prices, limits=dict(plan.limits), sort=plan.sort)


def plans_out(catalogue: Mapping[str, PlanSpec]) -> list[PlanOut]:
    """The answer to ``GET /billing/plans`` (§4): every plan of ``catalogue`` as
    :class:`PlanOut`, in ``sort`` order (then by code, as
    :func:`~eifi1_server_kit.billing.plan_catalogue` orders them — sorted again here, so a
    mapping built by hand comes out the same)::

        @router.get("/billing/plans", response_model=list[PlanOut])
        def billing_plans(payer: Payer = Depends(current_payer)) -> list[PlanOut]:
            settings.require_billing_enabled()  # 404 billing_disabled
            return plans_out(PLANS)

    Every currency the plans are sold in: the page picks the payer's — the overview's
    ``currency`` (§12.18) — and offers the switch where a plan has more than one.
    """
    return [PlanOut.from_spec(plan) for plan in _in_order(catalogue)]


def _in_order(catalogue: Mapping[str, PlanSpec]) -> list[PlanSpec]:
    return sorted(catalogue.values(), key=lambda plan: (plan.sort, plan.code))


class AdminPlan(BaseModel):
    """One plan of ``GET /admin/plans`` (§14.12): ``{code, limits, sort}`` — the
    catalogue's code, its limits (``null`` for unlimited) and its order, what the
    operator's plan picker and the roster's plan filter are built from, with the names
    from the app's i18n. Lifted from keksdose (``schemas/admin.py``). Build the list with
    :func:`admin_plans_out`.

    **No prices**: they are the payer's (``GET /billing/plans``, :class:`PlanOut`), and
    this answers whatever the billing switch says — an operator grants plans with billing
    off too. Every plan of the catalogue, the ones not sold any more included: the
    accounts on them keep them.
    """

    code: PlanCode
    limits: dict[str, int | None]
    sort: int

    @classmethod
    def from_spec(cls, plan: PlanSpec) -> Self:
        """``plan`` for the operator: its code, limits and order."""
        return cls(code=plan.code, limits=dict(plan.limits), sort=plan.sort)


def admin_plans_out(catalogue: Mapping[str, PlanSpec]) -> list[AdminPlan]:
    """The answer to ``GET /admin/plans`` (§14.12): every plan of ``catalogue`` as
    :class:`AdminPlan`, in :func:`plans_out`'s order (``sort``, then the code)::

        @router.get("/admin/plans", response_model=list[AdminPlan])
        async def list_plans() -> list[AdminPlan]:     # admins only: the router's gate
            return admin_plans_out(PLANS)
    """
    return [AdminPlan.from_spec(plan) for plan in _in_order(catalogue)]


class CheckoutRequest(BaseModel):
    """``POST /billing/checkout {plan, interval, currency}`` (§4): the plan, how often it
    is paid, and in which currency (CHF or EUR, §2.6). Check the plan against the
    catalogue and its ``prices`` for the combination (a 422 for one it doesn't sell), then
    take the provider's price id from the settings
    (:meth:`~eifi1_server_kit.billing.BillingSettings.billing_price_id`) and put the payer's
    reference into the checkout (:func:`~eifi1_server_kit.billing.checkout_custom_data`).
    """

    model_config = ConfigDict(extra="forbid")

    plan: PlanCode
    interval: Interval
    currency: Currency


class PlanSwitchRequest(BaseModel):
    """``POST /billing/plan/preview`` and ``POST /billing/plan`` ``{plan, interval}``
    (§15.2.10): the plan to switch to and how often it is paid. No currency: a switch keeps
    the subscription's (another currency is a new checkout after the old one ends). The
    price comes from :func:`~eifi1_server_kit.billing.switch_price`::

        @router.post("/billing/plan/preview", response_model=PlanSwitchPreview)
        async def preview(body: PlanSwitchRequest, payer: Payer = Depends(current_payer)) -> PlanSwitchPreview:
            settings.require_billing_enabled()                          # 404
            row = await row_of(payer)
            require_plan_switch(row, now())                             # 409 billing_not_subscribed
            price_id = switch_price(PLANS, row, body, settings, sold=SOLD)   # 422, 409 billing_plan_unchanged
            answer = await billing_client().preview_plan_change(
                subscription_id=row.provider_subscription_id, price_id=price_id
            )                                                           # 502
            return plan_switch_preview(answer, PLANS[body.plan], await usage_of(payer), interval=body.interval)

    ``POST /billing/plan`` is the same with ``change_plan``; the row follows through the
    ``subscription.updated`` Paddle sends.
    """

    model_config = ConfigDict(extra="forbid")

    plan: PlanCode
    interval: Interval


class ProviderSwitchPreview(BaseModel):
    """The provider's figures for a plan switch (§15.2.10), what
    :meth:`~eifi1_server_kit.billing.BillingProviderClient.preview_plan_change` and
    ``change_plan`` answer: Paddle's ``PATCH /subscriptions/{id}/preview``, prorated
    immediately.

    * ``currency`` — the subscription's.
    * ``now_minor`` — charged now, minor units; NEGATIVE for a credit (a downgrade's unused
      part, which Paddle keeps on the customer's balance and takes off the next charges).
    * ``next_minor`` — the next charge, ``None`` where the provider names none.
    * ``next_billed_at`` — when, ``None`` likewise.
    """

    currency: Currency
    now_minor: int
    next_minor: MinorUnits | None = None
    next_billed_at: UtcDateTime | None = None


class PlanSwitchPreview(ProviderSwitchPreview):
    """The answer to ``POST /billing/plan/preview`` and ``POST /billing/plan``
    (§15.2.10): the provider's figures plus ``{plan, interval, over_limit, limits,
    usage}`` — what ui-kit's ``PlanSwitchConfirm`` shows before the payer agrees. A
    downgrade over the new limits is allowed (§3.4: a limit gates creation only), so
    ``over_limit`` names the dimensions over it, in the plan's order, for the lines.
    Build it with :func:`plan_switch_preview`."""

    plan: PlanCode
    interval: Interval
    over_limit: list[str]
    limits: dict[str, int | None]
    usage: dict[str, int]


def plan_switch_preview(
    answer: ProviderSwitchPreview, plan: PlanSpec, usage: Mapping[str, int], *, interval: BillingInterval | str
) -> PlanSwitchPreview:
    """The route's answer (§15.2.10): the provider's ``answer`` for a switch to ``plan``
    per ``interval``, with the payer's ``usage`` and the dimensions it exceeds on the new
    plan (:func:`~eifi1_server_kit.billing.dimensions_over_limit`)."""
    return PlanSwitchPreview.model_validate(
        {
            **answer.model_dump(),
            "plan": plan.code,
            "interval": interval,
            "over_limit": dimensions_over_limit(plan, usage),
            "limits": dict(plan.limits),
            "usage": dict(usage),
        }
    )


class PortalRequest(BaseModel):
    """``POST /billing/portal {target?}`` (§14.5), an optional body: where the portal link
    opens — ``overview`` ("Payment and invoices", the default), ``cancel`` ("Cancel
    subscription", the subscription's cancel link, §12.26) or ``payment_method`` (the
    payment-failed banner). The link is never stored. Every target is for the buyer only
    where the row stores one (§16.2): :func:`~eifi1_server_kit.billing.require_portal_access`
    first, a no-op for a person-payer app::

        @router.post("/billing/portal", response_model=CheckoutAnswer)
        async def portal(body: PortalRequest | None = None, viewer=Depends(current_user)) -> CheckoutAnswer:
            settings.require_billing_enabled()
            row = await row_of(payer_of(viewer))
            require_portal_access(row, viewer_ref=str(viewer.id))   # 403 billing_portal_buyer_only
            url = await billing_client().portal_url(
                customer_id=row.provider_customer_id,           # None: 409 billing_not_at_provider
                subscription_id=row.provider_subscription_id,
                target=(body or PortalRequest()).target,
            )
            return CheckoutAnswer(url=url)
    """

    model_config = ConfigDict(extra="forbid")

    target: PortalTarget = "overview"


def _web_address(value: str) -> str:
    if not value.startswith(("https://", "http://")):
        raise ValueError("the provider's page is an http(s) address")
    return value


class CheckoutAnswer(BaseModel):
    """``{url}``: the provider's hosted page (§2.8) — the answer to ``POST
    /billing/checkout`` (the checkout) and ``POST /billing/portal`` (payment method,
    invoices, cancellation). No card field and no invoice copy is ever in an app. After the
    checkout the page shows "payment processing" and re-polls the overview: the webhook may
    land after the person is back (§12.21)."""

    url: Annotated[str, AfterValidator(_web_address)]


class PlanChangeRequest(BaseModel):
    """``POST /admin/…/{id}/plan {plan, comped_until?, acknowledged}`` (§4, §6): an
    operator's plan or grant — :attr:`~eifi1_server_kit.user_admin.AdminAction.PLAN` at the
    ``acknowledge`` level, checked with
    :func:`~eifi1_server_kit.user_admin.require_confirmation`. No ``confirm_email``: the
    level is ``acknowledge`` for every account, and offering the field would suggest a
    stronger gate (keksdose's reasoning).

    The app writes it as an operator's grant (§3.2): ``comped``, ``source`` ``manual``,
    until ``comped_until`` or without an end; a ``comped_until`` of now ends a grant. Log it
    as ``detail {from, to, comped_until, counts}`` (§6). An admin transfer or an erasure
    hand-over never checks a limit, and neither does this (§12.15).

    **A running beta grant keeps its beta** (keksdose's ``kept_beta``, its 0.32 report):
    given no ``comped_until``, a change on a row whose ``source`` is ``beta`` and whose
    grant holds (:func:`~eifi1_server_kit.billing.grant_holds` with the settings' launch)
    moves ``plan_code`` only — ``status``, ``source`` and ``comped_until`` stay, so the
    beta still ends at the launch plus 12 months
    (:func:`~eifi1_server_kit.billing.effective_comped_until`). Before the launch every
    payer is beta; writing such a move as ``manual`` without an end would make the payer
    free for good, which nobody decided. A ``comped_until`` turns it into an operator's
    grant until then; any other row takes the grant as above. The kit writes no row, so
    the rule is the app's (keksdose's ``set_plan``), which answers it as
    :class:`PlanChangeResponse`'s ``kept_beta``.
    """

    model_config = ConfigDict(extra="forbid")

    plan: PlanCode
    comped_until: UtcDateTime | None = None
    acknowledged: bool = False


class PlanChangeResponse(BaseModel):
    """What a plan change did (§6, §14.12): ``{previous_plan, plan, limits, over_limit,
    usage, kept_beta, comped_until}`` — what ui-kit's ``usePlanChangeResult`` turns into
    the operator's result lines.

    ``previous_plan`` is the point of the shape: "the plan was changed" without the value
    it replaced is no trail (keksdose); ``None`` when the account had no subscription
    (the line then reads "Plan set to …"). ``over_limit`` — a downgrade below what the
    payer has — is not an error and nothing is cleaned up: every item stays open and
    writable, only the next create is refused (§3.4), so the panel says it rather than let
    an admin discover it, with one line per dimension over the new limit from ``usage``
    and ``limits``. ``kept_beta``: a running beta kept its end and only the plan moved
    (§12.34, :class:`PlanChangeRequest`). ``comped_until``: the grant's end as written.
    Build it with :meth:`of`.
    """

    previous_plan: PlanCode | None
    plan: PlanCode
    limits: dict[str, int | None]
    over_limit: bool
    #: What the payer has, per dimension (§12.15): the figures for the over-limit lines.
    usage: dict[str, int]
    #: A running beta grant kept its beta (§12.34): only the plan changed.
    kept_beta: bool = False
    #: The grant's end as the change wrote it; ``None``: no end, or no grant. Required
    #: though it may be ``None`` (no default): it is always sent, so the generated
    #: TypeScript marks it required (keksdose's 0.33 report). :meth:`of` fills it.
    comped_until: UtcDateTime | None

    @classmethod
    def of(
        cls,
        previous_plan: str | None,
        plan: PlanSpec,
        usage: Mapping[str, int],
        *,
        kept_beta: bool = False,
        comped_until: datetime | None = None,
    ) -> Self:
        """The answer for a change from ``previous_plan`` (``None``: there was no
        subscription) to ``plan``, with the payer's ``usage``
        (:func:`~eifi1_server_kit.billing.dimensions_over_limit`), whether a running beta
        was kept, and the grant's end."""
        return cls(
            previous_plan=previous_plan,
            plan=plan.code,
            limits=dict(plan.limits),
            over_limit=bool(dimensions_over_limit(plan, usage)),
            usage=dict(usage),
            kept_beta=kept_beta,
            comped_until=comped_until,
        )


def _id_text(value: object) -> object:
    return str(value) if isinstance(value, uuid.UUID) else value


_ChangeId = Annotated[str, BeforeValidator(_id_text), Field(min_length=1, max_length=200)]


class SyncRefusal(BaseModel):
    """The changes a sync reply refused (§12.5): ``{code, detail, change_ids}``, on the
    app's sync answer beside the updates it still sends.

    **A 402 is not a refusal of one change.** Sync endpoints stay on the gate's allow-list;
    inside them, a lapsed payer's changes are refused as ``billing_read_only`` and not
    applied, and the reply still carries the updates — keksdose's E2EE gate is the
    precedent. The client keeps the refused changes queued, as "N changes waiting for a
    plan", and sends them after payment::

        class SyncResponse(BaseModel):
            server_knowledge: int
            changes: list[SyncChange]
            refused: SyncRefusal | None = None

    ``change_ids`` are the client-minted ids of the refused changes (keksdose's and
    kastlan's ``SyncChange.id``; a UUID is written as its string). ``code`` is another
    kit code where a change is refused for another reason (``plan_limit`` for a create past
    the limit, after the read-only check, §12.3).
    """

    code: str = BillingErrorCode.BILLING_READ_ONLY.value
    detail: str = BILLING_ERROR_DETAIL[BillingErrorCode.BILLING_READ_ONLY]
    change_ids: list[_ChangeId] = Field(min_length=1)
