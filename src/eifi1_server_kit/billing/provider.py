"""The provider's API behind one port, and what the routes check before they call it.

``docs/billing-harmonization.md`` §14.2, §14.4–§14.6 and §14.8 (with §12.23 and §12.37) in
``Eifi1/ui-kit``. No httpx here: :class:`BillingProviderClient` is the port every app's
billing service calls, :func:`billing_provider_client` answers the deployment's client —
:class:`~eifi1_server_kit.billing.PaddleClient` (the ``billing`` extra) for Paddle with a
key, :class:`NoProviderClient` otherwise — and the rest is pure.

**Four calls, each a hosted page or a change at the provider** (§2.8): a checkout for one
price, a customer-portal session, a cancellation, and undoing a cancellation the period's
end would carry out. No card field, payment method or invoice copy is ever in an app.

**The seam is module-level, not a FastAPI override** (§14.8, keksdose's review): a
deletion's cancellation runs in the service layer, where a request's dependency overrides
don't reach. Each app keeps one function that answers the client, and its tests replace
it with :class:`~eifi1_server_kit.billing.testing.FakeBillingProvider`::

    def billing_client() -> BillingProviderClient:          # the app's seam, patched in tests
        return billing_provider_client(get_settings(), client=shared_http_client)

**The checkout route** (§14.2, §14.6)::

    @router.post("/billing/checkout", response_model=CheckoutAnswer)
    async def checkout(body: CheckoutRequest, payer: Payer = Depends(current_payer)) -> CheckoutAnswer:
        settings.require_billing_enabled()                                  # 404
        sold_plan(PLANS, body, sold=SOLD)                                   # 422 billing_plan_not_sold
        row = await row_of(payer)
        require_new_checkout(row, now())                                    # 409 billing_already_subscribed
        url = await billing_client().checkout_url(
            price_id=settings.billing_price_id(body.plan, body.currency, body.interval),   # 503
            custom_data=checkout_custom_data(payer_ref(payer), app=settings.billing_app),
            customer_id=row.provider_customer_id,
        )                                                                   # 502 billing_provider_unavailable
        return CheckoutAnswer(url=url)

**The way back is configuration, never a request field** (§14.4): the pay page's
``successUrl`` or the hosted checkout's redirect lands on the app's subscription page with
``?checkout=done`` (:func:`checkout_return_url`), which the page reads and drops.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.plans import PlanSpec, normalize_plan
from eifi1_server_kit.billing.settings import BillingProvider, BillingSettings
from eifi1_server_kit.billing.standing import SubscriptionRow, SubscriptionSource, SubscriptionStatus

if TYPE_CHECKING:
    import httpx

    from eifi1_server_kit.billing.schemas import CheckoutRequest

__all__ = [
    "CHECKOUT_RETURN_PARAM",
    "CHECKOUT_RETURN_VALUE",
    "BillingProviderClient",
    "CancelOutcome",
    "NoProviderClient",
    "PortalTarget",
    "ResumeOutcome",
    "billing_provider_client",
    "cancel_for_deletion",
    "checkout_return_url",
    "require_new_checkout",
    "resume_after_withdrawal",
    "sold_plan",
]

logger = logging.getLogger("eifi1_server_kit.billing")

#: Where a portal link opens (§14.5): ``overview`` for "Payment and invoices", ``cancel``
#: for "Cancel subscription" (§12.26), ``payment_method`` for the payment-failed banner.
PortalTarget = Literal["overview", "cancel", "payment_method"]
#: What :func:`cancel_for_deletion` did, for the deletion request's audit detail.
CancelOutcome = Literal["cancelled", "failed"]
#: What :func:`resume_after_withdrawal` did, for the reactivation's audit detail.
ResumeOutcome = Literal["resumed", "failed"]

#: The query parameter that marks the return from a checkout on the app's subscription
#: page (§14.4): ``?checkout=done``. ui-kit's ``isCheckoutReturn`` reads it.
CHECKOUT_RETURN_PARAM = "checkout"
#: Its value.
CHECKOUT_RETURN_VALUE = "done"


class BillingProviderClient(Protocol):
    """The provider's four calls (§14.2), as every app's billing service makes them.
    :class:`~eifi1_server_kit.billing.PaddleClient` is the one implementation;
    :class:`NoProviderClient` refuses, and the tests' fake records
    (:class:`~eifi1_server_kit.billing.testing.FakeBillingProvider`).

    A failure is a :class:`~eifi1_server_kit.billing.BillingError`: 502
    ``billing_provider_unavailable`` ("try again"), or 503 ``billing_not_configured`` for
    the deployment's fault — answered by the contract's error handlers as it is.
    """

    async def checkout_url(
        self,
        *,
        price_id: str,
        custom_data: Mapping[str, str],
        customer_id: str | None = None,
        locale: str | None = None,
    ) -> str:
        """The provider's checkout for one price (quantity 1), carrying ``custom_data``
        (:func:`~eifi1_server_kit.billing.checkout_custom_data`; the client adds its app
        tag) and the payer's customer when known. ``locale`` reaches only a hosted
        checkout."""
        ...

    async def portal_url(
        self,
        *,
        customer_id: str | None,
        subscription_id: str | None = None,
        target: PortalTarget = "overview",
    ) -> str:
        """A fresh portal session's link, never stored. ``customer_id`` ``None`` → 409
        ``billing_not_at_provider``."""
        ...

    async def cancel(self, *, subscription_id: str, immediately: bool = False) -> None:
        """At the period's end by default; ``immediately`` for a paused subscription."""
        ...

    async def remove_scheduled_cancel(self, *, subscription_id: str) -> None:
        """Undo a cancellation at the period's end (§14.8)."""
        ...


class NoProviderClient:
    """No provider client for this deployment — no provider, no key, or an environment the
    settings can't name: every call is 503 ``billing_not_configured``, the deployment's
    fault. Billing switched on can only reach it with a provider the kit has no client for
    (the deprecated Lemon Squeezy, §14.13); with billing off the routes answer 404 first.

    ``reason`` is the refusal's detail, for the log; the page reads the code.
    """

    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason or "No payment provider client is configured"

    def _refuse(self) -> BillingError:
        return BillingError(BillingErrorCode.BILLING_NOT_CONFIGURED, self._reason)

    async def checkout_url(
        self,
        *,
        price_id: str,
        custom_data: Mapping[str, str],
        customer_id: str | None = None,
        locale: str | None = None,
    ) -> str:
        raise self._refuse()

    async def portal_url(
        self,
        *,
        customer_id: str | None,
        subscription_id: str | None = None,
        target: PortalTarget = "overview",
    ) -> str:
        raise self._refuse()

    async def cancel(self, *, subscription_id: str, immediately: bool = False) -> None:
        raise self._refuse()

    async def remove_scheduled_cancel(self, *, subscription_id: str) -> None:
        raise self._refuse()


def billing_provider_client(
    settings: BillingSettings, *, client: httpx.AsyncClient | None = None
) -> BillingProviderClient:
    """The deployment's provider client (§14.2): a
    :class:`~eifi1_server_kit.billing.PaddleClient` for provider ``paddle`` with a key —
    on the API :meth:`~eifi1_server_kit.billing.BillingSettings.billing_api_environment`
    names, tagging every checkout with ``billing_app``, opening it on
    ``billing_checkout_page_url`` or ``billing_hosted_checkout_url`` — and a
    :class:`NoProviderClient` otherwise. It never raises for the settings: an environment
    nobody can name is a :class:`NoProviderClient` that says so, so a deletion's
    cancellation (:func:`cancel_for_deletion`) still goes on. The Paddle client needs the
    ``billing`` extra (httpx); without it this raises :class:`ImportError`.

    ``client`` is an ``httpx.AsyncClient`` the app owns and closes — one for the process,
    opened in its lifespan, so calls reuse connections; without one each call opens and
    closes its own. The switch is not asked: the routes ask it first
    (:meth:`~eifi1_server_kit.billing.BillingSettings.require_billing_enabled`), and a
    deletion's cancellation is owed even after billing was switched off.
    """
    key = settings.billing_api_key
    if settings.billing_provider is not BillingProvider.PADDLE or key is None or not key.get_secret_value().strip():
        return NoProviderClient()
    try:
        environment = settings.billing_api_environment()
    except BillingError as exc:
        return NoProviderClient(str(exc))
    from eifi1_server_kit.billing.paddle import PaddleClient

    return PaddleClient(
        key.get_secret_value(),
        environment=environment,
        app=settings.billing_app,
        checkout_page_url=settings.billing_checkout_page_url,
        hosted_checkout_url=settings.billing_hosted_checkout_url,
        client=client,
    )


def sold_plan(
    catalogue: Mapping[str, PlanSpec], request: CheckoutRequest, *, sold: Collection[str] | None = None
) -> PlanSpec:
    """The plan a checkout asks for (§14.2), or 422 ``billing_plan_not_sold``: a plan the
    catalogue doesn't have, one outside ``sold`` (keksdose's ``SOLD``: the plans on sale,
    where the catalogue keeps retired ones for the rows still on them), or a currency and
    interval the plan has no price for (a free plan has none). kastlan answered 503 here;
    the price id missing from the settings for a combination that IS sold stays 503
    ``billing_not_configured`` (:meth:`~eifi1_server_kit.billing.BillingSettings.billing_price_id`)."""
    code = normalize_plan(request.plan)
    plan = catalogue.get(code)
    on_sale = sold is None or code in {normalize_plan(item) for item in sold}
    if plan is None or not on_sale or plan.price(request.currency, request.interval) is None:
        raise BillingError(
            BillingErrorCode.BILLING_PLAN_NOT_SOLD,
            f"The {code} plan is not sold in {request.currency} per {request.interval}",
        )
    return plan


#: The provider's states in which its subscription still runs.
_RUNNING = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE})


def _provider_subscription_runs(row: SubscriptionRow, now: datetime) -> bool:
    if row.provider_subscription_id is None or row.cancel_at_period_end:
        return False
    if SubscriptionSource(row.source) is SubscriptionSource.PROVIDER:
        return SubscriptionStatus(row.status) in _RUNNING
    # Bought under a grant (§12.11): the webhook wrote the link, the period and the
    # cancellation, but left the grant's status and source — the period says it runs.
    return row.current_period_end is not None and _aware(now) < _aware(row.current_period_end)


def require_new_checkout(row: SubscriptionRow, now: datetime) -> None:
    """Refuse a checkout while a provider subscription runs (§14.6): 409
    ``billing_already_subscribed``. A second checkout would be a second subscription,
    charged twice; plan changes go through the provider's customer portal, and the page
    that gets the 409 points there. Call it in ``POST /billing/checkout`` before the
    transaction is created.

    A subscription runs when the row has the provider's subscription id and it isn't set
    to cancel at its period's end, and:

    * the row's ``source`` is ``provider``: its status is ``active``, ``trialing`` or
      ``past_due``;
    * any other source — a payer who bought while a grant held (§12.11): the webhook
      wrote the subscription's link, period end and cancellation but kept the grant's
      ``comped`` status and source, so the provider's period running past ``now`` is what
      says it runs.

    A subscription already set to cancel at its period's end never blocks: the payer may
    subscribe again, to another plan.
    """
    if _provider_subscription_runs(row, now):
        raise BillingError(BillingErrorCode.BILLING_ALREADY_SUBSCRIBED)


async def cancel_for_deletion(row: SubscriptionRow, client: BillingProviderClient) -> CancelOutcome | None:
    """Cancel the payer's subscription at the provider when the account's deletion is
    REQUESTED (§12.23, §12.37), or the provider keeps charging a deactivated account.
    Lifted from keksdose (``billing_service.cancel_for_deletion``).

    * ``None``: nothing to cancel — no ``provider_subscription_id`` (every payer while
      billing is off), ``canceled``, or ``cancel_at_period_end`` already set (the payer
      cancelled before asking to leave).
    * ``expired`` — Paddle's ``paused``, which Paddle cancels only immediately — is
      cancelled at once, so nothing is left at the provider for an account on its way to
      erasure (a change for keksdose, which skipped it). Anything else at the period's
      end, so a guest's shared item works until then.
    * ``"cancelled"``, or ``"failed"``: it **never raises** (decision 14, leaving never
      depends on paying). A failure is logged at ERROR — "cancel it by hand" — and the
      deletion goes on.

    Put the outcome into the request's audit detail (keksdose's
    ``provider_cancellation``; kastlan per flagged company): a later reactivation undoes
    only a ``"cancelled"`` (:func:`resume_after_withdrawal`). The row itself goes at the
    erasure.
    """
    subscription_id = row.provider_subscription_id
    if subscription_id is None or row.cancel_at_period_end:
        return None
    status = SubscriptionStatus(row.status)
    if status is SubscriptionStatus.CANCELED:
        return None
    try:
        await client.cancel(subscription_id=subscription_id, immediately=status is SubscriptionStatus.EXPIRED)
    except Exception:
        logger.exception(
            "billing: could not cancel the subscription %s for a deletion request — cancel it by hand",
            subscription_id,
        )
        return "failed"
    return "cancelled"


async def resume_after_withdrawal(row: SubscriptionRow, client: BillingProviderClient) -> ResumeOutcome | None:
    """Undo the cancellation a deletion request scheduled, when the account comes back
    (§12.37, §14.8): an operator's reactivation that cancels a pending deletion, or a
    person's own withdrawal where the app has one. The provider's
    :meth:`~BillingProviderClient.remove_scheduled_cancel` — Paddle's ``PATCH
    /subscriptions/{id} {"scheduled_change": null}``: the subscription stays ``active``,
    and the row follows through the ``subscription.updated`` event it sends.

    **Only the cancellation the deletion made:** call it only when the request's audit
    detail recorded ``"cancelled"`` for this row (:func:`cancel_for_deletion`). A payer who
    had cancelled in the portal before asking to leave (the request answered ``None``)
    isn't subscribed again.

    * ``None``: nothing to undo — no ``provider_subscription_id``, ``cancel_at_period_end``
      not set, the subscription ``canceled`` or ``expired``, or its period over. A
      subscription cancelled at once (it was paused) or past its period can't be
      reinstated (Paddle: a cancelled subscription "can't be reinstated"); the payer
      subscribes again through a new checkout.
    * ``"resumed"``, or ``"failed"``: it **never raises**. A failure is logged at ERROR —
      "remove the scheduled cancellation by hand" — and goes into the reactivation's audit
      detail (``provider_resume``).
    """
    subscription_id = row.provider_subscription_id
    if subscription_id is None or not row.cancel_at_period_end:
        return None
    if SubscriptionStatus(row.status) in (SubscriptionStatus.CANCELED, SubscriptionStatus.EXPIRED):
        return None
    if row.current_period_end is not None and datetime.now(UTC) >= _aware(row.current_period_end):
        return None
    try:
        await client.remove_scheduled_cancel(subscription_id=subscription_id)
    except Exception:
        logger.exception(
            "billing: could not resume the subscription %s after a withdrawn deletion — "
            "remove the scheduled cancellation by hand",
            subscription_id,
        )
        return "failed"
    return "resumed"


def checkout_return_url(app_base_url: str, path: str) -> str:
    """Where a checkout returns to (§14.4): the app's subscription page with
    ``?checkout=done`` — ``checkout_return_url("https://kastlan.app", "/admin/billing")``
    is ``"https://kastlan.app/admin/billing?checkout=done"``. For the deploy notes, the
    pay page's ``returnUrl`` and the tests; the request carries no URL (an open redirect,
    and Paddle's transaction takes none). A query or a fragment on ``path`` is kept, and
    an existing ``checkout`` parameter replaced."""
    parts = urlsplit(f"{app_base_url.rstrip('/')}/{path.lstrip('/')}")
    query = [
        (name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != CHECKOUT_RETURN_PARAM
    ]
    query.append((CHECKOUT_RETURN_PARAM, CHECKOUT_RETURN_VALUE))
    return urlunsplit(parts._replace(query=urlencode(query)))
