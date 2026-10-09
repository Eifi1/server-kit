"""Applying a webhook event: the event store, the guards, and what to answer the provider.

``docs/billing-harmonization.md`` §5 and §12.11, §12.20 in ``Eifi1/ui-kit``. The webhook
route is the app's; what it does in between is this::

    @router.post("/webhooks/paddle")
    async def paddle_webhook(request: Request, session=Depends(system_session)) -> Response:
        settings.require_billing_enabled()                       # 404 billing_disabled
        raw = await request.body()
        settings.verify_billing_webhook("paddle", raw, request.headers)   # 400, with the tolerance
        try:
            event = parse_webhook_event("paddle", raw, app=settings.billing_app)  # None: not used, not ours
            result = None if event is None else await dispatch(
                event, EventTable(session), apply, load=find_payer_row,
                plan_for_price=settings.billing_plan_for_price, launch=settings.billing_launch_at,
            )
            await session.commit()                               # recorded and applied in ONE transaction
        except Exception as exc:
            await session.rollback()
            result = exc
        answer = webhook_answer(result)
        if answer.log:
            logger.warning("billing webhook %s: %r", answer.status_code, result)   # never the body
        return Response(status_code=answer.status_code)

**The event table sits outside tenant RLS**, or the handler uses the bypass explicitly
(§12.20): a webhook writes rows for any payer. kastlan's ``billing_stripe_events`` becomes
``billing_events(provider, event_id UNIQUE, type, occurred_at, received_at, processed_at)``
(§5). Two deliveries of one event racing each other: both pass ``seen``, and the loser's
``record`` violates the unique key. Its store raises :class:`DuplicateEventError`, and
:func:`dispatch` answers :attr:`DispatchOutcome.DUPLICATE` — a 200, as for an event seen
before. A store that lets the database error through instead rolls back and answers 500,
and the provider's retry finds the event seen.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.events import NormalisedEvent, PoisonEventError
from eifi1_server_kit.billing.settings import BillingProvider
from eifi1_server_kit.billing.standing import SubscriptionRow, SubscriptionSource, SubscriptionStatus, grant_holds

__all__ = [
    "DispatchOutcome",
    "DuplicateEventError",
    "EventStore",
    "WebhookAnswer",
    "dispatch",
    "row_changes",
    "webhook_answer",
]


class DuplicateEventError(Exception):
    """Raised by :meth:`EventStore.record` when the event is in the table already: the
    insert hit the unique ``(provider, event_id)`` — a second delivery that raced the first
    past :meth:`~EventStore.seen` (§5). :func:`dispatch` answers it as
    :attr:`DispatchOutcome.DUPLICATE`, and :func:`webhook_answer` as a 200 wherever it is
    raised.

    It replaces the savepoint recipe as the kit's way: a nested transaction is what lets
    the outer one go on after the failed insert, and SQLite's driver in the apps' tests
    can't nest one, so each app worked around it (keksdose's 0.32 report). The store
    catches the database's unique violation, undoes the failed insert — a savepoint where
    the driver nests them, else a rollback of the session: nothing :func:`dispatch` did
    before ``record`` needs keeping, since ``seen`` only reads — and raises this.
    """


class EventStore(Protocol):
    """The app's ``billing_events`` table (§5) — the port :func:`dispatch` records through.
    Every method runs in the request's one transaction."""

    async def seen(self, provider: BillingProvider, event_id: str) -> bool:
        """Is ``(provider, event_id)`` in the table already — applied or not?"""
        ...

    async def record(self, event: NormalisedEvent, *, received_at: datetime) -> None:
        """Insert the event: ``provider``, ``event_id``, ``type`` (``event.provider_type``),
        ``occurred_at`` and ``received_at``; ``processed_at`` empty.

        Flush it here, so a unique violation surfaces here and not at the commit, and raise
        :class:`DuplicateEventError` for one: the event is in the table already.
        """
        ...

    async def mark_processed(self, provider: BillingProvider, event_id: str, *, processed_at: datetime) -> None:
        """Set ``processed_at``: the event was decided — applied, or deliberately skipped."""
        ...


class DispatchOutcome(enum.StrEnum):
    """What :func:`dispatch` did with an event. Every outcome answers 2xx
    (:func:`webhook_answer`)."""

    #: Written onto the payer's row.
    APPLIED = "applied"
    #: Seen before (§5) — by ``seen``, or by ``record`` raising
    #: :class:`DuplicateEventError`: nothing written, nothing recorded again.
    DUPLICATE = "duplicate"
    #: Older than the row's ``updated_from_event_at`` — the ordering guard (§5): recorded,
    #: not applied. Neither provider guarantees the order of delivery.
    STALE = "stale"
    #: A free grant runs (§12.11): recorded; only the provider's link and dates written,
    #: the status, source and plan left as the grant set them.
    GRANT_HOLDS = "grant_holds"
    #: About a subscription other than the row's, and it would not keep the payer in good
    #: standing — a late event of a subscription the payer replaced: recorded, not applied.
    OTHER_SUBSCRIPTION = "other_subscription"
    #: No payer's row found for it: recorded, NOT marked processed, and logged — every payer
    #: has a row (§12.12), so this is a test event, a deleted payer, or a lost reference.
    NO_PAYER = "no_payer"


def row_changes(
    event: NormalisedEvent, *, grant_holds: bool = False, plan_code: str | None = None
) -> dict[str, object]:
    """The columns of the payer's row (§3.2) that ``event`` sets, by name — what
    :func:`dispatch` hands the app's ``apply``::

        for name, value in changes.items():
            setattr(row, name, value)

    * Always ``provider``, and the provider's ``provider_customer_id`` and
      ``provider_subscription_id`` where the event names them — the link the portal and a
      deletion's cancellation need (§12.23).
    * A snapshot also sets ``current_period_end`` and ``cancel_at_period_end`` where it
      carries them, and ``updated_from_event_at`` (the ordering guard's mark).
    * Unless a grant holds, a snapshot then sets ``status``, ``source`` (``provider``) and,
      given ``plan_code``, the plan. While a grant holds those stay as the grant set them,
      and :func:`~eifi1_server_kit.billing.in_good_standing` reads the provider's paid
      period once the grant ends (§12.11).

    Datetimes are aware, in UTC: an app with naive columns (kastlan) strips the zone.
    ``status``, ``source`` and ``provider`` are the kit's ``StrEnum`` members
    (:class:`~eifi1_server_kit.billing.SubscriptionStatus`,
    :class:`~eifi1_server_kit.billing.SubscriptionSource`,
    :class:`~eifi1_server_kit.billing.BillingProvider`), not plain strings: a ``str``
    subclass, so they compare equal to their string values and a string column stores
    them as those values — an app that writes ``.value`` instead sees no difference.
    """
    changes: dict[str, object] = {"provider": event.provider}
    if event.customer_ref is not None:
        changes["provider_customer_id"] = event.customer_ref
    if event.subscription_ref is not None:
        changes["provider_subscription_id"] = event.subscription_ref
    if not event.is_snapshot:
        return changes
    if event.current_period_end is not None:
        changes["current_period_end"] = event.current_period_end
    if event.cancel_at_period_end is not None:
        changes["cancel_at_period_end"] = event.cancel_at_period_end
    changes["updated_from_event_at"] = event.occurred_at
    if grant_holds:
        return changes
    changes["status"] = event.status
    changes["source"] = SubscriptionSource.PROVIDER
    if plan_code is not None:
        changes["plan_code"] = plan_code
    return changes


_KEEPS_STANDING = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE})


def _foreign(event: NormalisedEvent, row: SubscriptionRow) -> bool:
    """About another subscription than the row's, and not one that would take its place."""
    if row.provider_subscription_id is None or event.subscription_ref is None:
        return False
    if event.subscription_ref == row.provider_subscription_id:
        return False
    return event.status not in _KEEPS_STANDING


async def dispatch[RowT: SubscriptionRow](
    event: NormalisedEvent,
    store: EventStore,
    apply: Callable[[NormalisedEvent, RowT, dict[str, object]], Awaitable[None]],
    *,
    load: Callable[[NormalisedEvent], Awaitable[RowT | None]],
    plan_for_price: Callable[[str], str | None],
    now: datetime | None = None,
    launch: datetime | None = None,
) -> DispatchOutcome:
    """Record ``event`` and apply it to the payer's row, through the app's ports (§5).

    Pure but for its callbacks, which it awaits in order and never concurrently:

    1. **seen** → :attr:`DispatchOutcome.DUPLICATE`; nothing else happens;
    2. **record** it (``received_at`` = ``now``); a :class:`DuplicateEventError` from it —
       a delivery that raced this one past ``seen`` — is
       :attr:`~DispatchOutcome.DUPLICATE` too, and nothing else happens;
    3. **load** the payer's row — by ``event.payer_ref`` first (the checkout's custom data,
       :func:`~eifi1_server_kit.billing.checkout_custom_data`), else by the provider's
       subscription or customer id. None → :attr:`~DispatchOutcome.NO_PAYER`;
    4. **the ordering guard** (§5): a snapshot older than the row's
       ``updated_from_event_at`` → :attr:`~DispatchOutcome.STALE`. A notice never moves
       the mark, so a late notice cannot hide a newer state;
    5. **another subscription** that would not keep the payer in good standing →
       :attr:`~DispatchOutcome.OTHER_SUBSCRIPTION`; one that would (the payer subscribed
       again) takes the row's link;
    6. **a free grant** (§12.11, :func:`~eifi1_server_kit.billing.grant_holds`) → apply only
       the link and the dates, :attr:`~DispatchOutcome.GRANT_HOLDS`. Pass ``launch`` — the
       settings' ``billing_launch_at`` — so a beta row stored without an end stops
       holding at the launch plus 12 months (§3.2); without it such a row holds for good,
       as in 0.6.0;
    7. otherwise **apply** :func:`row_changes`, with the plan from ``plan_for_price``
       (:meth:`~eifi1_server_kit.billing.BillingSettings.billing_plan_for_price`). A price
       the settings don't know is 503 ``billing_not_configured``: nothing is kept, and the
       provider's retry applies the event once the setting is there;
    8. **mark processed**, except for ``NO_PAYER``.

    ``apply(event, row, changes)`` writes the changes — and may send the app's notice for
    the kind. It raises :class:`~eifi1_server_kit.billing.PoisonEventError` for an event it
    can never apply (answered 2xx and logged); any other exception rolls the transaction
    back and answers 5xx, so the provider retries (:func:`webhook_answer`).
    """
    moment = datetime.now(UTC) if now is None else _aware(now)
    if await store.seen(event.provider, event.event_id):
        return DispatchOutcome.DUPLICATE
    try:
        await store.record(event, received_at=moment)
    except DuplicateEventError:
        return DispatchOutcome.DUPLICATE
    row = await load(event)
    if row is None:
        return DispatchOutcome.NO_PAYER
    if (
        event.is_snapshot
        and row.updated_from_event_at is not None
        and event.occurred_at < _aware(row.updated_from_event_at)
    ):
        outcome = DispatchOutcome.STALE
    elif _foreign(event, row):
        outcome = DispatchOutcome.OTHER_SUBSCRIPTION
    else:
        holds = event.is_snapshot and grant_holds(row, moment, launch=launch)
        plan_code: str | None = None
        if event.is_snapshot and not holds and event.plan_price_id is not None:
            plan_code = plan_for_price(event.plan_price_id)
            if plan_code is None:
                raise BillingError(
                    BillingErrorCode.BILLING_NOT_CONFIGURED,
                    f"No plan for the {event.provider} price {event.plan_price_id}",
                )
        await apply(event, row, row_changes(event, grant_holds=holds, plan_code=plan_code))
        outcome = DispatchOutcome.GRANT_HOLDS if holds else DispatchOutcome.APPLIED
    await store.mark_processed(event.provider, event.event_id, processed_at=moment)
    return outcome


class WebhookAnswer(NamedTuple):
    """The status to answer the provider with, and whether the case is worth a log line."""

    status_code: int
    log: bool


def webhook_answer(result: DispatchOutcome | BaseException | None) -> WebhookAnswer:
    """The answer policy (§5): what the webhook route answers for what happened.

    * ``None`` — an event billing doesn't use — and every :class:`DispatchOutcome`: 200.
      Paddle and Lemon Squeezy both ask for a 200; anything else is retried. So is a
      :class:`DuplicateEventError` that reaches the route (a store that raises it at the
      commit): the event is in the table, a retry would only find it there.
    * :attr:`DispatchOutcome.NO_PAYER` and a
      :class:`~eifi1_server_kit.billing.PoisonEventError`: 200 and logged — a retry would
      bring the same bytes (keksdose's Pub/Sub rule).
    * A :class:`~eifi1_server_kit.billing.BillingError`: its own status — 400 for a bad
      signature, 404 with billing off, 503 for a missing setting (retried) — logged, but
      for the 404.
    * Anything else is transient: 500 and logged, so the provider retries (Paddle 60 times
      over 3 days, Lemon Squeezy 3 times within minutes).

    Log the result's type and message, never the body: it carries the buyer's details.
    """
    if result is None:
        return WebhookAnswer(200, log=False)
    if isinstance(result, DispatchOutcome):
        return WebhookAnswer(200, log=result is DispatchOutcome.NO_PAYER)
    if isinstance(result, DuplicateEventError):
        return WebhookAnswer(200, log=False)
    if isinstance(result, PoisonEventError):
        return WebhookAnswer(200, log=True)
    if isinstance(result, BillingError):
        return WebhookAnswer(result.status_code, log=result.code is not BillingErrorCode.BILLING_DISABLED)
    return WebhookAnswer(500, log=True)
