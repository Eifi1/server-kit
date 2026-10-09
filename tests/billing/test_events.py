"""The normalised webhook events (billing contract §5): each provider's events, mapped from
payloads shaped as their docs show them. Synthetic people only."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from eifi1_server_kit.billing import (
    APP_KEY,
    LEMONSQUEEZY_EVENT_KINDS,
    LEMONSQUEEZY_STATUSES,
    PADDLE_EVENT_KINDS,
    PADDLE_STATUSES,
    PAYER_REF_KEY,
    WEBHOOK_MAPPERS,
    BillingProvider,
    EventKind,
    NormalisedEvent,
    PoisonEventError,
    SubscriptionStatus,
    checkout_custom_data,
    map_lemonsqueezy_event,
    map_paddle_event,
    parse_webhook_event,
)


def _paddle(event_type: str = "subscription.updated", **data: Any) -> dict[str, Any]:
    """A Paddle Billing notification (developer.paddle.com/webhooks/subscriptions/…)."""
    subscription: dict[str, Any] = {
        "id": "sub_01h04vsc0qhwtsbsxh3422wjs4",
        "status": "active",
        "customer_id": "ctm_01h04vsbhqc62t8hmd4z3b578c",
        "currency_code": "CHF",
        "items": [
            {"status": "active", "recurring": False, "price": {"id": "pri_one_off"}},
            {"status": "active", "recurring": True, "price": {"id": "pri_pro_chf_y", "product_id": "pro_01"}},
        ],
        "current_billing_period": {"starts_at": "2026-10-08T09:00:00Z", "ends_at": "2027-10-08T09:00:00.000000Z"},
        "scheduled_change": None,
        "canceled_at": None,
        "custom_data": {"payer_ref": "user:1"},
    }
    return {
        "event_id": "evt_01h04vsc6t5zf4vp4cby9jq6bf",
        "event_type": event_type,
        "occurred_at": "2026-10-08T09:00:01.123456Z",
        "notification_id": "ntf_01h04vsc7g3v5w0dbmz0gq8ghv",
        "data": {**subscription, **data},
    }


def _lemon(event_name: str = "subscription_updated", **attributes: Any) -> dict[str, Any]:
    """A Lemon Squeezy webhook (docs.lemonsqueezy.com/help/webhooks/example-payloads)."""
    return {
        "meta": {"event_name": event_name, "custom_data": {"payer_ref": "user:1"}},
        "data": {
            "type": "subscriptions",
            "id": "1",
            "attributes": {
                "store_id": 1,
                "customer_id": 7,
                "order_id": 1,
                "product_id": 1,
                "variant_id": 12345,
                "user_name": "Ada Example",
                "user_email": "ada@example.com",
                "status": "active",
                "cancelled": False,
                "trial_ends_at": None,
                "renews_at": "2027-10-08T00:00:00.000000Z",
                "ends_at": None,
                "created_at": "2026-10-08T09:00:00.000000Z",
                "updated_at": "2026-10-08T09:00:01.000000Z",
                "test_mode": True,
                **attributes,
            },
        },
    }


def _bytes(payload: object) -> bytes:
    return json.dumps(payload).encode()


# --- the vocabulary ----------------------------------------------------------------------


def test_the_vocabulary_is_the_contracts() -> None:
    """§5's five kinds."""
    assert [str(kind) for kind in EventKind] == [
        "subscription_started",
        "subscription_updated",
        "payment_failed",
        "subscription_canceled",
        "subscription_expired",
    ]
    assert set(WEBHOOK_MAPPERS) == set(BillingProvider)
    assert set(PADDLE_STATUSES) == {"active", "trialing", "past_due", "paused", "canceled"}
    assert set(LEMONSQUEEZY_STATUSES) == {"on_trial", "active", "past_due", "unpaid", "paused", "cancelled", "expired"}


def test_the_checkout_carries_the_payer_reference() -> None:
    assert checkout_custom_data("company:7") == {PAYER_REF_KEY: "company:7"} == {"payer_ref": "company:7"}
    with pytest.raises(ValueError, match="non-empty"):
        checkout_custom_data("  ")


def test_the_checkout_carries_the_apps_tag() -> None:
    """§14.3: one Paddle account, three apps; the tag says whose checkout it was."""
    assert checkout_custom_data("user:42", app="keksdose") == {"payer_ref": "user:42", APP_KEY: "keksdose"}
    assert APP_KEY == "app"
    with pytest.raises(ValueError, match="app tag"):
        checkout_custom_data("user:42", app=" ")


# --- Paddle ------------------------------------------------------------------------------


def test_a_paddle_subscription_update_is_a_snapshot() -> None:
    event = map_paddle_event(_bytes(_paddle()))
    assert event == NormalisedEvent(
        provider=BillingProvider.PADDLE,
        event_id="evt_01h04vsc6t5zf4vp4cby9jq6bf",
        provider_type="subscription.updated",
        kind=EventKind.SUBSCRIPTION_UPDATED,
        occurred_at=datetime(2026, 10, 8, 9, 0, 1, 123456, tzinfo=UTC),
        customer_ref="ctm_01h04vsbhqc62t8hmd4z3b578c",
        subscription_ref="sub_01h04vsc0qhwtsbsxh3422wjs4",
        plan_price_id="pri_pro_chf_y",  # the first RECURRING item
        status=SubscriptionStatus.ACTIVE,
        current_period_end=datetime(2027, 10, 8, 9, 0, tzinfo=UTC),
        cancel_at_period_end=False,
        custom_data={"payer_ref": "user:1"},
    )
    assert event.is_snapshot and event.payer_ref == "user:1"


@pytest.mark.parametrize(
    ("event_type", "status", "kind", "row_status"),
    [
        ("subscription.created", "active", EventKind.SUBSCRIPTION_STARTED, SubscriptionStatus.ACTIVE),
        ("subscription.imported", "active", EventKind.SUBSCRIPTION_STARTED, SubscriptionStatus.ACTIVE),
        ("subscription.activated", "active", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.ACTIVE),
        ("subscription.trialing", "trialing", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.TRIALING),
        ("subscription.resumed", "active", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.ACTIVE),
        ("subscription.paused", "paused", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.EXPIRED),
        ("subscription.past_due", "past_due", EventKind.PAYMENT_FAILED, SubscriptionStatus.PAST_DUE),
    ],
)
def test_each_paddle_subscription_event_has_its_kind(
    event_type: str, status: str, kind: EventKind, row_status: SubscriptionStatus
) -> None:
    assert set(PADDLE_EVENT_KINDS) >= {event_type}
    event = map_paddle_event(_bytes(_paddle(event_type, status=status)))
    assert event is not None and (event.kind, event.status) == (kind, row_status)


def test_a_scheduled_paddle_cancellation_is_subscription_canceled() -> None:
    """Paddle has no event of its own for it: ``subscription.updated`` with a scheduled
    ``cancel`` (developer.paddle.com/webhooks/subscriptions/subscription-canceled)."""
    scheduled = {"action": "cancel", "effective_at": "2027-10-08T09:00:00Z", "resume_at": None}
    event = map_paddle_event(_bytes(_paddle(scheduled_change=scheduled)))
    assert event is not None and event.kind is EventKind.SUBSCRIPTION_CANCELED
    assert event.cancel_at_period_end and event.status is SubscriptionStatus.ACTIVE
    # A scheduled pause is not a cancellation; nor is a cancel on an event of another name.
    paused = map_paddle_event(_bytes(_paddle(scheduled_change={**scheduled, "action": "pause"})))
    assert paused is not None and paused.kind is EventKind.SUBSCRIPTION_UPDATED and not paused.cancel_at_period_end
    trialing = map_paddle_event(_bytes(_paddle("subscription.trialing", status="trialing", scheduled_change=scheduled)))
    assert trialing is not None and trialing.kind is EventKind.SUBSCRIPTION_UPDATED and trialing.cancel_at_period_end
    ended = map_paddle_event(_bytes(_paddle(status="canceled", scheduled_change=scheduled)))
    assert ended is not None and ended.kind is EventKind.SUBSCRIPTION_UPDATED


def test_a_canceled_paddle_subscription_has_expired_at_its_cancellation() -> None:
    """``current_billing_period`` is null once cancelled; ``canceled_at`` says when it ended."""
    event = map_paddle_event(
        _bytes(
            _paddle(
                "subscription.canceled",
                status="canceled",
                current_billing_period=None,
                canceled_at="2026-12-01T00:00:00Z",
            )
        )
    )
    assert event is not None
    assert (event.kind, event.status) == (EventKind.SUBSCRIPTION_EXPIRED, SubscriptionStatus.CANCELED)
    assert event.current_period_end == datetime(2026, 12, 1, tzinfo=UTC)
    bare = map_paddle_event(_bytes(_paddle(current_billing_period=None)))
    assert bare is not None and bare.current_period_end is None


def test_a_paddle_event_without_extras_still_maps() -> None:
    event = map_paddle_event(_bytes(_paddle(items="none", custom_data=None, customer_id=None)))
    assert event is not None
    assert (event.plan_price_id, event.custom_data, event.payer_ref, event.customer_ref) == (None, {}, None, None)
    odd_items = [None, {"recurring": True, "price": {"id": ""}}, {"price": "pri_x"}, {"price": {"id": "pri_ok"}}]
    assert getattr(map_paddle_event(_bytes(_paddle(items=odd_items))), "plan_price_id", None) == "pri_ok"
    assert getattr(map_paddle_event(_bytes(_paddle(items=[]))), "plan_price_id", "x") is None


@pytest.mark.parametrize(
    "event_type", ["transaction.payment_failed", "transaction.completed", "customer.updated", "subscription.unknown"]
)
def test_other_paddle_events_are_not_used(event_type: str) -> None:
    """A declined checkout card fires ``transaction.payment_failed`` with no subscription yet."""
    assert map_paddle_event(_bytes({"event_type": event_type, "data": "anything"})) is None


@pytest.mark.parametrize(
    ("payload", "where"),
    [
        (b"not json", "not JSON"),
        (b"\xff\xfe", "not JSON"),
        (b"[1, 2]", "not a JSON object"),
        (_bytes({"data": {}}), "event_type"),
        (_bytes({**_paddle(), "data": []}), "data is not an object"),
        (_bytes(_paddle(status="incomplete")), "data.status is a status the kit doesn't know"),
        (_bytes(_paddle(status=None)), "data.status"),
        (_bytes(_paddle(id="")), "data.id"),
        (_bytes(_paddle(customer_id=True)), "data.customer_id is not an id"),
        (_bytes(_paddle(current_billing_period={"ends_at": "next year"})), "ends_at is not a date-time"),
        (_bytes(_paddle(current_billing_period={"ends_at": 1_791_460_800})), "ends_at is not a date-time"),
        (_bytes({**_paddle(), "occurred_at": None}), "occurred_at"),
        (_bytes({**_paddle(), "event_id": ""}), "event_id"),
    ],
)
def test_an_unreadable_paddle_body_is_poison(payload: bytes, where: str) -> None:
    with pytest.raises(PoisonEventError, match=where):
        map_paddle_event(payload)


# --- Lemon Squeezy -----------------------------------------------------------------------


def test_a_lemon_squeezy_subscription_update_is_a_snapshot() -> None:
    raw = _bytes(_lemon())
    event = map_lemonsqueezy_event(raw)
    assert event == NormalisedEvent(
        provider=BillingProvider.LEMONSQUEEZY,
        event_id=hashlib.sha256(raw).hexdigest(),  # no event id in the docs: the body's digest
        provider_type="subscription_updated",
        kind=EventKind.SUBSCRIPTION_UPDATED,
        occurred_at=datetime(2026, 10, 8, 9, 0, 1, tzinfo=UTC),
        customer_ref="7",
        subscription_ref="1",
        plan_price_id="12345",  # the variant
        status=SubscriptionStatus.ACTIVE,
        current_period_end=datetime(2027, 10, 8, tzinfo=UTC),
        cancel_at_period_end=False,
        custom_data={"payer_ref": "user:1"},
    )
    assert "ada@example.com" not in event.model_dump_json()  # the buyer's address never rides along


@pytest.mark.parametrize(
    ("event_name", "status", "kind", "row_status"),
    [
        ("subscription_created", "on_trial", EventKind.SUBSCRIPTION_STARTED, SubscriptionStatus.TRIALING),
        ("subscription_resumed", "active", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.ACTIVE),
        ("subscription_paused", "paused", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.EXPIRED),
        ("subscription_unpaused", "active", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.ACTIVE),
        ("subscription_updated", "past_due", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.PAST_DUE),
        ("subscription_updated", "unpaid", EventKind.SUBSCRIPTION_UPDATED, SubscriptionStatus.EXPIRED),
    ],
)
def test_each_lemon_squeezy_subscription_event_has_its_kind(
    event_name: str, status: str, kind: EventKind, row_status: SubscriptionStatus
) -> None:
    assert event_name in LEMONSQUEEZY_EVENT_KINDS
    event = map_lemonsqueezy_event(_bytes(_lemon(event_name, status=status)))
    assert event is not None and (event.kind, event.status) == (kind, row_status)


def test_a_cancelled_lemon_squeezy_subscription_runs_until_it_ends() -> None:
    """ "Still technically active" until ``ends_at`` (the subscription object's docs)."""
    event = map_lemonsqueezy_event(
        _bytes(_lemon("subscription_cancelled", status="cancelled", cancelled=True, ends_at="2027-10-08T00:00:00Z"))
    )
    assert event is not None
    assert (event.kind, event.status, event.cancel_at_period_end) == (
        EventKind.SUBSCRIPTION_CANCELED,
        SubscriptionStatus.ACTIVE,
        True,
    )
    assert event.current_period_end == datetime(2027, 10, 8, tzinfo=UTC)
    expired = map_lemonsqueezy_event(
        _bytes(_lemon("subscription_expired", status="expired", cancelled=True, ends_at="2027-10-08T00:00:00Z"))
    )
    assert expired is not None and (expired.kind, expired.status) == (
        EventKind.SUBSCRIPTION_EXPIRED,
        SubscriptionStatus.EXPIRED,
    )
    no_end = map_lemonsqueezy_event(_bytes(_lemon(renews_at=None, custom_data=None)))
    assert no_end is not None and no_end.current_period_end is None


def test_a_failed_lemon_squeezy_payment_is_a_notice() -> None:
    """Its body is a subscription invoice: no status, so it changes no state."""
    payload: dict[str, Any] = {
        "meta": {"event_name": "subscription_payment_failed"},
        "data": {
            "type": "subscription-invoices",
            "id": "9",
            "attributes": {
                "subscription_id": 1,
                "customer_id": 7,
                "status": "pending",
                "updated_at": "2026-11-08T00:00:00Z",
            },
        },
    }
    event = map_lemonsqueezy_event(_bytes(payload))
    assert event is not None and event.kind is EventKind.PAYMENT_FAILED
    assert not event.is_snapshot and event.status is None and event.custom_data == {}
    assert (event.subscription_ref, event.customer_ref, event.plan_price_id) == ("1", "7", None)
    with pytest.raises(PoisonEventError, match=r"data\.type is not subscription-invoices"):
        map_lemonsqueezy_event(_bytes({**payload, "data": {**payload["data"], "type": "subscriptions"}}))


@pytest.mark.parametrize(
    "event_name",
    ["order_created", "subscription_payment_success", "subscription_payment_recovered", "license_key_created"],
)
def test_other_lemon_squeezy_events_are_not_used(event_name: str) -> None:
    assert map_lemonsqueezy_event(_bytes({"meta": {"event_name": event_name}})) is None


@pytest.mark.parametrize(
    ("payload", "where"),
    [
        (_bytes({"data": {}}), "meta is not an object"),
        (_bytes({"meta": {}}), "meta.event_name"),
        (_bytes({"meta": {"event_name": "subscription_updated"}}), "data is not an object"),
        (_bytes({**_lemon(), "data": {"type": "subscriptions"}}), "data.attributes"),
        (_bytes({**_lemon(), "data": {**_lemon()["data"], "type": "orders"}}), "data.type is not subscriptions"),
        (_bytes(_lemon(status="refunded")), "a status the kit doesn't know"),
        (_bytes(_lemon(updated_at=None)), "updated_at"),
        (_bytes(_lemon(variant_id=[1])), "variant_id is not an id"),
        (_bytes(_lemon(customer_id="")), "customer_id is not an id"),
    ],
)
def test_an_unreadable_lemon_squeezy_body_is_poison(payload: bytes, where: str) -> None:
    with pytest.raises(PoisonEventError, match=where):
        map_lemonsqueezy_event(payload)


def test_parse_webhook_event_picks_the_mapper() -> None:
    assert getattr(parse_webhook_event("paddle", _bytes(_paddle())), "provider", None) is BillingProvider.PADDLE
    assert getattr(parse_webhook_event(BillingProvider.LEMONSQUEEZY, _bytes(_lemon())), "provider", None) is (
        BillingProvider.LEMONSQUEEZY
    )
    with pytest.raises(ValueError):
        parse_webhook_event("stripe", b"{}")


def test_a_naive_time_is_read_as_utc() -> None:
    event = map_paddle_event(_bytes({**_paddle(), "occurred_at": "2026-10-08T09:00:01"}))
    assert event is not None and event.occurred_at == datetime(2026, 10, 8, 9, 0, 1, tzinfo=UTC)


def test_a_payer_reference_must_be_a_non_empty_string() -> None:
    for custom in ({"payer_ref": ""}, {"payer_ref": 42}, {"other": "user:1"}):
        event = map_paddle_event(_bytes(_paddle(custom_data=custom)))
        assert event is not None and event.payer_ref is None


# --- the app's tag (§14.3) ---------------------------------------------------------------


def _tagged(app: object) -> bytes:
    custom: dict[str, object] = {"payer_ref": "user:42"}
    if app is not None:
        custom["app"] = app
    return _bytes(_paddle(custom_data=custom))


def test_an_event_reads_the_apps_tag() -> None:
    event = map_paddle_event(_tagged("keksdose"))
    assert event is not None and event.app == "keksdose" and event.payer_ref == "user:42"
    for tag in (None, "", 7):
        untagged = map_paddle_event(_tagged(tag))
        assert untagged is not None and untagged.app is None


def test_the_apps_own_events_pass(caplog: pytest.LogCaptureFixture) -> None:
    event = parse_webhook_event("paddle", _tagged("keksdose"), app="keksdose")
    assert event is not None and event.app == "keksdose"
    assert caplog.records == []


def test_another_apps_event_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Kurvenschmiede's user 42 is not keksdose's user 42: answered 200, never dispatched."""
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.billing"):
        assert parse_webhook_event("paddle", _tagged("kurvenschmiede"), app="keksdose") is None
    (record,) = caplog.records
    assert record.levelno == logging.INFO and "'kurvenschmiede'" in record.getMessage()
    assert "evt_01h04vsc6t5zf4vp4cby9jq6bf" in record.getMessage() and "user:42" not in record.getMessage()


def test_an_untagged_event_is_dropped_once_the_app_is_given(caplog: pytest.LogCaptureFixture) -> None:
    """Kurvenschmiede's review: never matched by the provider's customer id, since one
    person may be one Paddle customer across the apps."""
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.billing"):
        assert parse_webhook_event("paddle", _tagged(None), app="keksdose") is None
    (record,) = caplog.records
    assert record.levelno == logging.WARNING and "untagged" in record.getMessage()


def test_without_an_app_every_event_passes_as_before() -> None:
    for tag in ("kurvenschmiede", None):
        assert parse_webhook_event("paddle", _tagged(tag)) is not None
    # An event billing doesn't use stays None, tagged or not.
    assert parse_webhook_event("paddle", _bytes(_paddle("transaction.completed")), app="keksdose") is None
