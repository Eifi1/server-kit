"""Billing's wire shapes (``docs/billing-harmonization.md`` §3.1, §4, §6 and §12.5 in
``Eifi1/ui-kit``).

Like the other kit schemas, meant to be used as they are or SUBCLASSED: the app adds its
fields and narrows a type. The request bodies refuse unknown fields (``extra="forbid"``): a
``price`` or a ``status`` sent by a client is a 422, never silently dropped — the price
comes from the settings, the status from the provider. Money is never on these shapes as a
float: a plan's prices are integer minor units (§4), formatted by the kit's
``formatMoney``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Self, TypedDict

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, with_config

from eifi1_server_kit.auth.schemas import UtcDateTime
from eifi1_server_kit.billing.errors import BILLING_ERROR_DETAIL, BillingErrorCode
from eifi1_server_kit.billing.plans import (
    BillingCurrency,
    Currency,
    Interval,
    MinorUnits,
    PlanCode,
    PlanSpec,
    dimensions_over_limit,
    normalize_plan,
)
from eifi1_server_kit.billing.standing import (
    SubscriptionRow,
    SubscriptionSource,
    SubscriptionStatus,
    effective_comped_until,
)
from eifi1_server_kit.billing.standing import in_good_standing as _in_good_standing

__all__ = [
    "BillingOverview",
    "BillingStatus",
    "CheckoutAnswer",
    "CheckoutRequest",
    "PlanChangeRequest",
    "PlanChangeResponse",
    "PlanIntervalPrices",
    "PlanOut",
    "PlanPrices",
    "SyncRefusal",
    "plans_out",
]


class BillingStatus(BaseModel):
    """``GET /billing/status`` → ``{billing_enabled}`` (§4): the one billing route that
    answers while billing is off — and to a demo user (§12.19), so the shell can hide the
    billing page, its nav entry, banners and search entries."""

    billing_enabled: bool


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
    ) -> Self:
        """The overview of ``row``, whose plan is ``plan`` (the catalogue's entry for
        ``row.plan_code`` — another plan is a :class:`ValueError`), with the payer's
        ``usage`` and ``currency``, its standing as of ``now``.

        Pass ``launch=settings.billing_launch_at``: a beta row stored before the launch
        date was known has no ``comped_until``, and both the standing and the
        ``comped_until`` shown read it as the launch plus 12 months (§3.2)."""
        if normalize_plan(row.plan_code) != plan.code:
            raise ValueError(f"the row's plan is {row.plan_code!r}, not {plan.code!r}")
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
    ordered = sorted(catalogue.values(), key=lambda plan: (plan.sort, plan.code))
    return [PlanOut.from_spec(plan) for plan in ordered]


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
    """

    model_config = ConfigDict(extra="forbid")

    plan: PlanCode
    comped_until: UtcDateTime | None = None
    acknowledged: bool = False


class PlanChangeResponse(BaseModel):
    """What a plan change did (§6): ``{previous_plan, plan, limits, over_limit}``.

    ``previous_plan`` is the point of the shape: "the plan was changed" without the value
    it replaced is no trail (keksdose). ``over_limit`` — a downgrade below what the payer
    has — is not an error and nothing is cleaned up: every item stays open and writable,
    only the next create is refused (§3.4), so the panel says it rather than let an admin
    discover it. Build it with :meth:`of`.
    """

    previous_plan: PlanCode
    plan: PlanCode
    limits: dict[str, int | None]
    over_limit: bool

    @classmethod
    def of(cls, previous_plan: str, plan: PlanSpec, usage: Mapping[str, int]) -> Self:
        """The answer for a change from ``previous_plan`` to ``plan``, with the payer's
        ``usage`` (:func:`~eifi1_server_kit.billing.dimensions_over_limit`)."""
        return cls(
            previous_plan=previous_plan,
            plan=plan.code,
            limits=dict(plan.limits),
            over_limit=bool(dimensions_over_limit(plan, usage)),
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
