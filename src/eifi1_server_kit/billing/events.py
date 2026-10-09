"""The providers' webhook events, normalised to one vocabulary.

``docs/billing-harmonization.md`` §5 in ``Eifi1/ui-kit``: "server-kit maps each provider's
events to one vocabulary … The app applies those. Supporting a second provider is a new
mapper, not new app code." A mapper reads the raw body (after its signature passed,
:func:`~eifi1_server_kit.billing.verify_webhook_signature`) and answers a
:class:`NormalisedEvent`, or ``None`` for an event billing doesn't use — answered 2xx and
dropped (§5). A body it cannot read is a :class:`PoisonEventError`: answered 2xx and logged,
because a retry would bring the same bytes.

**The kind says what happened; the snapshot says where the subscription stands.** Both
providers send the whole subscription with every subscription event, so the app writes the
snapshot's fields (:func:`~eifi1_server_kit.billing.row_changes`) and uses the kind only for
its log and its notices. ``payment_failed`` from Lemon Squeezy is the exception: its body is
an invoice, so it carries no status (``status`` ``None``) and changes nothing but the link.

**One Paddle account sells for all three apps** (decision 18, §14.3), and Paddle's
notification destinations can't filter by product or custom data: every app's webhook
receives every app's subscription events. So every checkout carries the app's tag
(:func:`checkout_custom_data`'s ``app``, which the kit's client adds itself), and
:func:`parse_webhook_event` given the app's ``billing_app`` drops another app's event and,
once ``app`` is given, an untagged one too — never matched by the provider's customer id,
since one person may be one Paddle customer across the apps.

.. deprecated:: 0.7.0
   Lemon Squeezy (:func:`map_lemonsqueezy_event`, :data:`LEMONSQUEEZY_EVENT_KINDS`,
   :data:`LEMONSQUEEZY_STATUSES`) is deprecated and goes in 0.8 (decision 25, §14.13).

=========================  ===================================  ================================
kind                       Paddle Billing                       Lemon Squeezy
=========================  ===================================  ================================
``subscription_started``   ``subscription.created``,            ``subscription_created``
                           ``subscription.imported``
``subscription_updated``   ``subscription.updated``,            ``subscription_updated``,
                           ``.activated``, ``.trialing``,       ``_resumed``, ``_paused``,
                           ``.resumed``, ``.paused``            ``_unpaused``
``subscription_canceled``  ``subscription.updated`` with a      ``subscription_cancelled``
                           scheduled ``cancel``
``subscription_expired``   ``subscription.canceled``            ``subscription_expired``
``payment_failed``         ``subscription.past_due``            ``subscription_payment_failed``
=========================  ===================================  ================================

``subscription_canceled`` means the payer cancelled and the subscription runs to its
period's end (``cancel_at_period_end``); ``subscription_expired`` that it has ended. Paddle
has no event of its own for a scheduled cancellation, so its ``subscription.updated`` with
``scheduled_change.action == "cancel"`` is promoted — and repeats if the subscription
changes again before it ends: compare with the row's ``cancel_at_period_end`` to notice the
change once. Transaction events (Paddle's ``transaction.payment_failed`` fires on a declined
checkout card too, with no subscription yet) and Lemon Squeezy's other payment events are
not used: the subscription events carry the state.

Statuses, the provider's word to the row's (§3.2): Paddle ``active``, ``trialing``,
``past_due``, ``canceled``, and ``paused`` → ``expired``; Lemon Squeezy ``on_trial`` →
``trialing``, ``active``, ``past_due``, ``unpaid`` and ``paused`` and ``expired`` →
``expired``, and ``cancelled`` → ``active`` with ``cancel_at_period_end`` (its docs: "still
technically active" until ``ends_at``). An unknown status is poison, never a guess.

Read on 2026-10-08:

* https://developer.paddle.com/webhooks/overview,
  https://developer.paddle.com/webhooks/subscriptions/subscription-updated,
  …/subscription-canceled, …/subscription-past-due, …/subscription-created,
  https://developer.paddle.com/build/subscriptions/provision-access-webhooks ("dedupe on
  ``event_id``", "compare ``occurred_at``"),
  https://developer.paddle.com/build/transactions/custom-data (checkout ``custom_data`` is
  copied to the subscription);
* https://docs.lemonsqueezy.com/help/webhooks/event-types,
  https://docs.lemonsqueezy.com/help/webhooks/webhook-requests (``meta.event_name``,
  ``meta.custom_data``; no event id), https://docs.lemonsqueezy.com/api/subscriptions/the-subscription-object
  (statuses, ``renews_at``, ``ends_at``, ``cancelled``).
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eifi1_server_kit.auth.schemas import UtcDateTime
from eifi1_server_kit.billing.settings import BillingProvider
from eifi1_server_kit.billing.standing import SubscriptionStatus

__all__ = [
    "APP_KEY",
    "LEMONSQUEEZY_EVENT_KINDS",
    "LEMONSQUEEZY_STATUSES",
    "PADDLE_EVENT_KINDS",
    "PADDLE_STATUSES",
    "PAYER_REF_KEY",
    "WEBHOOK_MAPPERS",
    "EventKind",
    "NormalisedEvent",
    "PoisonEventError",
    "checkout_custom_data",
    "map_lemonsqueezy_event",
    "map_paddle_event",
    "parse_webhook_event",
]

logger = logging.getLogger("eifi1_server_kit.billing")


class EventKind(enum.StrEnum):
    """The normalised vocabulary (§5). A ``StrEnum``, so ``str(kind)`` is the word."""

    #: A new subscription: link the payer's row to it.
    SUBSCRIPTION_STARTED = "subscription_started"
    #: Anything else changed: the plan, the period (a renewal), a pause, a resume, a
    #: cancellation withdrawn.
    SUBSCRIPTION_UPDATED = "subscription_updated"
    #: A renewal payment failed; the provider retries (§3.3: still in good standing).
    PAYMENT_FAILED = "payment_failed"
    #: The payer cancelled; it runs until ``current_period_end``.
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    #: The subscription has ended.
    SUBSCRIPTION_EXPIRED = "subscription_expired"


#: The key of the app's payer reference in a checkout's custom data
#: (:func:`checkout_custom_data`).
PAYER_REF_KEY = "payer_ref"
#: The key of the app's tag in a checkout's custom data (§14.3): the settings'
#: ``billing_app``.
APP_KEY = "app"


def checkout_custom_data(payer_ref: str, *, app: str | None = None) -> dict[str, str]:
    """The custom data to put into a checkout, so its webhooks find the payer and the app:
    ``{"payer_ref": "user:42", "app": "keksdose"}`` (or ``"company:7"``, ``"kastlan"``).

    Paddle keeps a checkout's ``custom_data`` on the transaction and copies it to the
    subscription it creates, so every subscription event carries it; Lemon Squeezy sends a
    checkout's ``checkout_data.custom`` as ``meta.custom_data`` with every subscription
    event. The reference is the app's own and opaque to the provider — never an address.
    Flat, because Paddle's dashboard shows nested custom data badly.

    ``app`` is the settings' ``billing_app`` (§14.3): on one Paddle account every app's
    webhook receives every app's events, and the tag is how each keeps only its own
    (:func:`parse_webhook_event`). The kit's client adds its own tag whatever is passed
    here (:class:`~eifi1_server_kit.billing.PaddleClient`), so an app can't forget it.
    """
    if not payer_ref.strip():
        raise ValueError("a payer reference is a non-empty string")
    data = {PAYER_REF_KEY: payer_ref}
    if app is not None:
        if not app.strip():
            raise ValueError("an app tag is a non-empty string")
        data[APP_KEY] = app
    return data


class PoisonEventError(ValueError):
    """An event that can never be applied — unreadable JSON, a field missing or of the wrong
    type, a status nobody knows. Answered 2xx and logged (§5, keksdose's Pub/Sub rule): a
    5xx would make the provider redeliver the same bytes, for days (Paddle retries 60 times
    over 3 days).

    The message names the field, never a value: Lemon Squeezy's attributes carry the
    buyer's name and address, so a log line must never quote the body.
    """


def _custom(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class NormalisedEvent(BaseModel):
    """One provider event in the kit's vocabulary (§5) — what the event store records and
    the app applies.

    A field that is ``None`` is one the event does not carry: leave the row's value. A
    SNAPSHOT (``status`` set) is the subscription's whole state at ``occurred_at``; a notice
    (``status`` ``None``, Lemon Squeezy's ``payment_failed``) only says something happened.
    """

    model_config = ConfigDict(frozen=True)

    provider: BillingProvider
    #: Unique per provider: Paddle's ``evt_…``; for Lemon Squeezy, which sends no event id,
    #: the SHA-256 of the raw body — a redelivery resends the same body.
    event_id: str = Field(min_length=1)
    #: The provider's own event name (``subscription.updated``), for the event store's
    #: ``type`` and the log.
    provider_type: str
    kind: EventKind
    #: When it happened, for the ordering guard (§5): Paddle's ``occurred_at``; Lemon
    #: Squeezy's ``updated_at`` of the object sent.
    occurred_at: UtcDateTime
    #: The provider's customer id (Paddle ``ctm_…``).
    customer_ref: str | None = None
    #: The provider's subscription id (Paddle ``sub_…``).
    subscription_ref: str | None = None
    #: The price the subscription runs on: Paddle's first recurring item's ``pri_…``, Lemon
    #: Squeezy's variant id. The settings say which plan it is.
    plan_price_id: str | None = None
    #: The row's new status; ``None`` on a notice.
    status: SubscriptionStatus | None = None
    #: The end of what the payer has paid for: the current period, or when it ended.
    current_period_end: UtcDateTime | None = None
    #: The payer cancelled for the period's end.
    cancel_at_period_end: bool | None = None
    #: The checkout's custom data, with the app's payer reference
    #: (:func:`checkout_custom_data`).
    custom_data: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_snapshot(self) -> bool:
        """Does it carry the subscription's state (and so take part in the ordering guard)?"""
        return self.status is not None

    @property
    def payer_ref(self) -> str | None:
        """The payer reference the app put into the checkout, or ``None``."""
        value = self.custom_data.get(PAYER_REF_KEY)
        return value if isinstance(value, str) and value else None

    @property
    def app(self) -> str | None:
        """The app's tag the checkout carried (§14.3), or ``None`` for an untagged event."""
        value = self.custom_data.get(APP_KEY)
        return value if isinstance(value, str) and value else None


# --- reading a body ------------------------------------------------------------------------


def _json_object(raw_body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError both are
        raise PoisonEventError("the body is not JSON") from None
    if not isinstance(payload, Mapping):
        raise PoisonEventError("the body is not a JSON object")
    return payload


def _object(parent: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise PoisonEventError(f"{where} is not an object")
    return value


def _text(parent: Mapping[str, Any], key: str, where: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise PoisonEventError(f"{where} is not a non-empty string")
    return value


def _ref(parent: Mapping[str, Any], key: str, where: str) -> str | None:
    """An id that may be a string or a JSON number (Lemon Squeezy's), or absent."""
    value = parent.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int) or value == "":
        raise PoisonEventError(f"{where} is not an id")
    return str(value)


def _instant(parent: Mapping[str, Any], key: str, where: str, *, optional: bool = False) -> datetime | None:
    value = parent.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PoisonEventError(f"{where} is not a date-time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise PoisonEventError(f"{where} is not a date-time") from None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _status(table: Mapping[str, SubscriptionStatus], value: str, where: str) -> SubscriptionStatus:
    if value not in table:
        raise PoisonEventError(f"{where} is a status the kit doesn't know")
    return table[value]


# --- Paddle Billing ----------------------------------------------------------------------

#: Paddle Billing's subscription events and their kinds; any other event is not used.
PADDLE_EVENT_KINDS: Mapping[str, EventKind] = {
    "subscription.created": EventKind.SUBSCRIPTION_STARTED,
    "subscription.imported": EventKind.SUBSCRIPTION_STARTED,
    "subscription.activated": EventKind.SUBSCRIPTION_UPDATED,
    "subscription.trialing": EventKind.SUBSCRIPTION_UPDATED,
    "subscription.updated": EventKind.SUBSCRIPTION_UPDATED,
    "subscription.resumed": EventKind.SUBSCRIPTION_UPDATED,
    "subscription.paused": EventKind.SUBSCRIPTION_UPDATED,
    "subscription.past_due": EventKind.PAYMENT_FAILED,
    "subscription.canceled": EventKind.SUBSCRIPTION_EXPIRED,
}

#: Paddle Billing's subscription statuses and the row's.
PADDLE_STATUSES: Mapping[str, SubscriptionStatus] = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.EXPIRED,
    "canceled": SubscriptionStatus.CANCELED,
}

_STILL_RUNNING = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE})


def _paddle_price(data: Mapping[str, Any]) -> str | None:
    """The first recurring item's price id: one plan per subscription."""
    items = data.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping) or item.get("recurring") is False:
            continue
        price = item.get("price")
        if isinstance(price, Mapping) and isinstance(price.get("id"), str) and price["id"]:
            return str(price["id"])
    return None


def map_paddle_event(raw_body: bytes) -> NormalisedEvent | None:
    """A Paddle Billing notification as a :class:`NormalisedEvent`; ``None`` for an event
    billing doesn't use; :class:`PoisonEventError` for a body it cannot read.

    The envelope is ``{event_id, event_type, occurred_at, notification_id, data}``, and a
    subscription event's ``data`` the whole subscription: ``id``, ``status``,
    ``customer_id``, ``items[].price.id``, ``current_billing_period.ends_at`` (``null``
    once cancelled — ``canceled_at`` then), ``scheduled_change.action``, ``custom_data``.
    """
    payload = _json_object(raw_body)
    event_type = _text(payload, "event_type", "event_type")
    kind = PADDLE_EVENT_KINDS.get(event_type)
    if kind is None:
        return None
    data = _object(payload, "data", "data")
    status = _status(PADDLE_STATUSES, _text(data, "status", "data.status"), "data.status")
    scheduled = data.get("scheduled_change")
    cancelling = isinstance(scheduled, Mapping) and scheduled.get("action") == "cancel"
    period = data.get("current_billing_period")
    if isinstance(period, Mapping):
        period_end = _instant(period, "ends_at", "data.current_billing_period.ends_at")
    else:
        period_end = _instant(data, "canceled_at", "data.canceled_at", optional=True)
    if event_type == "subscription.updated" and cancelling and status in _STILL_RUNNING:
        kind = EventKind.SUBSCRIPTION_CANCELED
    return NormalisedEvent.model_validate(
        {
            "provider": BillingProvider.PADDLE,
            "event_id": _text(payload, "event_id", "event_id"),
            "provider_type": event_type,
            "kind": kind,
            "occurred_at": _instant(payload, "occurred_at", "occurred_at"),
            "customer_ref": _ref(data, "customer_id", "data.customer_id"),
            "subscription_ref": _text(data, "id", "data.id"),
            "plan_price_id": _paddle_price(data),
            "status": status,
            "current_period_end": period_end,
            "cancel_at_period_end": cancelling,
            "custom_data": _custom(data.get("custom_data")),
        }
    )


# --- Lemon Squeezy -----------------------------------------------------------------------

#: Lemon Squeezy's subscription events and their kinds; any other event is not used.
#: Deprecated in 0.7.0, removed in 0.8 (§14.13).
LEMONSQUEEZY_EVENT_KINDS: Mapping[str, EventKind] = {
    "subscription_created": EventKind.SUBSCRIPTION_STARTED,
    "subscription_updated": EventKind.SUBSCRIPTION_UPDATED,
    "subscription_resumed": EventKind.SUBSCRIPTION_UPDATED,
    "subscription_paused": EventKind.SUBSCRIPTION_UPDATED,
    "subscription_unpaused": EventKind.SUBSCRIPTION_UPDATED,
    "subscription_cancelled": EventKind.SUBSCRIPTION_CANCELED,
    "subscription_expired": EventKind.SUBSCRIPTION_EXPIRED,
    "subscription_payment_failed": EventKind.PAYMENT_FAILED,
}

#: Lemon Squeezy's subscription statuses and the row's. ``cancelled`` is still running
#: until ``ends_at``, so it stays ``active`` with ``cancel_at_period_end``. Deprecated in
#: 0.7.0, removed in 0.8 (§14.13).
LEMONSQUEEZY_STATUSES: Mapping[str, SubscriptionStatus] = {
    "on_trial": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.EXPIRED,
    "paused": SubscriptionStatus.EXPIRED,
    "cancelled": SubscriptionStatus.ACTIVE,
    "expired": SubscriptionStatus.EXPIRED,
}


def _check_type(data: Mapping[str, Any], expected: str) -> None:
    kind = data.get("type")
    if kind is not None and kind != expected:
        raise PoisonEventError(f"data.type is not {expected}")


def map_lemonsqueezy_event(raw_body: bytes) -> NormalisedEvent | None:
    """A Lemon Squeezy webhook as a :class:`NormalisedEvent`; ``None`` for an event billing
    doesn't use; :class:`PoisonEventError` for a body it cannot read.

    .. deprecated:: 0.7.0
       Removed in 0.8 with the rest of Lemon Squeezy (§14.13).

    The body is JSON:API: ``{meta: {event_name, custom_data?}, data: {type, id,
    attributes}}``. A subscription's attributes carry ``status``, ``customer_id``,
    ``variant_id``, ``cancelled``, ``renews_at``, ``ends_at`` and ``updated_at``;
    ``subscription_payment_failed`` sends a ``subscription-invoices`` object instead, with
    ``subscription_id`` and ``customer_id`` but no status.

    The event id is the body's SHA-256: Lemon Squeezy documents no event id, and a retry
    (it retries three times, after 5, 25 and 125 seconds) resends the event.
    """
    payload = _json_object(raw_body)
    meta = _object(payload, "meta", "meta")
    name = _text(meta, "event_name", "meta.event_name")
    kind = LEMONSQUEEZY_EVENT_KINDS.get(name)
    if kind is None:
        return None
    data = _object(payload, "data", "data")
    attributes = _object(data, "attributes", "data.attributes")
    event: dict[str, Any] = {
        "provider": BillingProvider.LEMONSQUEEZY,
        "event_id": hashlib.sha256(raw_body).hexdigest(),
        "provider_type": name,
        "kind": kind,
        "occurred_at": _instant(attributes, "updated_at", "data.attributes.updated_at"),
        "customer_ref": _ref(attributes, "customer_id", "data.attributes.customer_id"),
        "custom_data": _custom(meta.get("custom_data")),
    }
    if kind is EventKind.PAYMENT_FAILED:
        _check_type(data, "subscription-invoices")
        event["subscription_ref"] = _ref(attributes, "subscription_id", "data.attributes.subscription_id")
        return NormalisedEvent.model_validate(event)
    _check_type(data, "subscriptions")
    raw_status = _text(attributes, "status", "data.attributes.status")
    ended = raw_status in ("cancelled", "expired")
    event |= {
        "subscription_ref": _ref(data, "id", "data.id"),
        "plan_price_id": _ref(attributes, "variant_id", "data.attributes.variant_id"),
        "status": _status(LEMONSQUEEZY_STATUSES, raw_status, "data.attributes.status"),
        "current_period_end": _instant(
            attributes, "ends_at" if ended else "renews_at", "data.attributes.renews_at/ends_at", optional=True
        ),
        "cancel_at_period_end": attributes.get("cancelled") is True or raw_status == "cancelled",
    }
    return NormalisedEvent.model_validate(event)


#: The mapper per provider: a second provider is a new entry, not new app code (§5).
WEBHOOK_MAPPERS: Mapping[BillingProvider, Callable[[bytes], NormalisedEvent | None]] = {
    BillingProvider.PADDLE: map_paddle_event,
    BillingProvider.LEMONSQUEEZY: map_lemonsqueezy_event,
}


def parse_webhook_event(
    provider: BillingProvider | str, raw_body: bytes, *, app: str | None = None
) -> NormalisedEvent | None:
    """The raw body of ``provider``'s webhook as a :class:`NormalisedEvent` — after its
    signature passed — or ``None`` for an event billing doesn't use (answer 2xx). A body
    that cannot be read is a :class:`PoisonEventError` (answer 2xx and log).

    ``app`` is the settings' ``billing_app`` (§14.3); pass it::

        event = parse_webhook_event("paddle", raw, app=settings.billing_app)

    Given, it keeps this app's events only: **another app's event** (``custom_data.app``
    differs) **and an untagged one are ``None``** — answered 200, never recorded or
    dispatched, and logged (the untagged at WARNING; the other apps' at INFO, since on one
    account they are two thirds of the traffic). An untagged event is never matched by the
    provider's customer id: one person may be one Paddle customer across the apps, and the
    payer references (``user:<id>``) repeat between keksdose and Kurvenschmiede. Every
    checkout from 0.7 on carries the tag, which Paddle copies to the subscription and its
    later events. ``None``: every event, as before 0.7.
    """
    event = WEBHOOK_MAPPERS[BillingProvider(provider)](raw_body)
    if event is None or app is None or event.app == app:
        return event
    if event.app is None:
        logger.warning(
            "billing webhook: dropped the untagged %s event %s (%s): no app in its custom data",
            event.provider,
            event.event_id,
            event.provider_type,
        )
    else:
        logger.info(
            "billing webhook: dropped the %s event %s (%s) of the app %r",
            event.provider,
            event.event_id,
            event.provider_type,
            event.app,
        )
    return None
