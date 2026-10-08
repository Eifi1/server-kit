"""Applying webhook events (billing contract §5, §12.11): the event store, the ordering
guard, an operator's grant, the payer's subscription, and the answer policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eifi1_server_kit.billing import (
    BillingError,
    BillingErrorCode,
    BillingProvider,
    BillingSettings,
    DispatchOutcome,
    EventKind,
    EventStore,
    NormalisedEvent,
    PoisonEventError,
    SubscriptionSource,
    SubscriptionStatus,
    WebhookAnswer,
    dispatch,
    in_good_standing,
    row_changes,
    webhook_answer,
)
from tests.billing._rows import NOW, Row

DAY = timedelta(days=1)
SETTINGS = BillingSettings(billing_price_ids={"pro": {"CHF": {"year": ["pri_pro_y", "pri_pro_y_2025"]}}})


def _event(**fields: Any) -> NormalisedEvent:
    base: dict[str, Any] = {
        "provider": BillingProvider.PADDLE,
        "event_id": "evt_1",
        "provider_type": "subscription.created",
        "kind": EventKind.SUBSCRIPTION_STARTED,
        "occurred_at": NOW,
        "customer_ref": "ctm_1",
        "subscription_ref": "sub_1",
        "plan_price_id": "pri_pro_y",
        "status": SubscriptionStatus.ACTIVE,
        "current_period_end": NOW + 365 * DAY,
        "cancel_at_period_end": False,
        "custom_data": {"payer_ref": "user:1"},
    }
    return NormalisedEvent.model_validate(base | fields)


@dataclass
class _Store:
    """``billing_events`` in memory: event id → (type, received_at, processed_at)."""

    rows: dict[tuple[str, str], list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def seen(self, provider: BillingProvider, event_id: str) -> bool:
        self.calls.append("seen")
        return (provider, event_id) in self.rows

    async def record(self, event: NormalisedEvent, *, received_at: datetime) -> None:
        self.calls.append("record")
        self.rows[event.provider, event.event_id] = [event.provider_type, received_at, None]

    async def mark_processed(self, provider: BillingProvider, event_id: str, *, processed_at: datetime) -> None:
        self.calls.append("processed")
        self.rows[provider, event_id][2] = processed_at


@dataclass
class _Payers:
    """The app's side: payer rows found by the checkout's payer reference."""

    rows: dict[str, Row] = field(default_factory=dict)
    applied: list[tuple[EventKind, dict[str, object]]] = field(default_factory=list)

    async def load(self, event: NormalisedEvent) -> Row | None:
        return self.rows.get(event.payer_ref or "")

    async def apply(self, event: NormalisedEvent, row: Row, changes: dict[str, object]) -> None:
        self.applied.append((event.kind, changes))
        for name, value in changes.items():
            setattr(row, name, value)


async def _dispatch(event: NormalisedEvent, store: _Store, payers: _Payers, now: datetime = NOW) -> DispatchOutcome:
    port: EventStore = store
    return await dispatch(
        event, port, payers.apply, load=payers.load, plan_for_price=SETTINGS.billing_plan_for_price, now=now
    )


def _trial() -> Row:
    return Row(trial_ends_at=NOW + 10 * DAY)


# --- applying ----------------------------------------------------------------------------


async def test_a_started_subscription_is_recorded_and_applied() -> None:
    store, payers = _Store(), _Payers({"user:1": _trial()})
    assert await _dispatch(_event(), store, payers) is DispatchOutcome.APPLIED
    row = payers.rows["user:1"]
    assert (row.status, row.source, row.plan_code) == (SubscriptionStatus.ACTIVE, SubscriptionSource.PROVIDER, "pro")
    assert (row.provider, row.provider_customer_id, row.provider_subscription_id) == ("paddle", "ctm_1", "sub_1")
    assert (row.current_period_end, row.cancel_at_period_end, row.updated_from_event_at) == (
        NOW + 365 * DAY,
        False,
        NOW,
    )
    assert store.calls == ["seen", "record", "processed"]
    assert store.rows["paddle", "evt_1"] == ["subscription.created", NOW, NOW]
    assert in_good_standing(row, NOW + 100 * DAY)


async def test_a_duplicate_is_answered_without_touching_anything() -> None:
    """§5: a duplicate answers 2xx."""
    store, payers = _Store(), _Payers({"user:1": _trial()})
    await _dispatch(_event(), store, payers)
    assert await _dispatch(_event(status=SubscriptionStatus.EXPIRED), store, payers) is DispatchOutcome.DUPLICATE
    assert payers.rows["user:1"].status is SubscriptionStatus.ACTIVE and len(payers.applied) == 1
    assert store.calls[-1] == "seen"


async def test_an_older_snapshot_does_not_overwrite_a_newer_one() -> None:
    """§5's ordering guard: neither provider guarantees the order of delivery."""
    store, payers = _Store(), _Payers({"user:1": _trial()})
    cancelled = _event(
        event_id="evt_2", kind=EventKind.SUBSCRIPTION_CANCELED, cancel_at_period_end=True, occurred_at=NOW + DAY
    )
    assert await _dispatch(cancelled, store, payers) is DispatchOutcome.APPLIED
    assert await _dispatch(_event(), store, payers) is DispatchOutcome.STALE
    row = payers.rows["user:1"]
    assert row.cancel_at_period_end is True and row.updated_from_event_at == NOW + DAY
    assert store.rows["paddle", "evt_1"][2] == NOW  # recorded and decided
    # The same instant is not older: it applies.
    same = _event(event_id="evt_3", occurred_at=NOW + DAY, cancel_at_period_end=False)
    assert await _dispatch(same, store, payers) is DispatchOutcome.APPLIED and row.cancel_at_period_end is False
    # A naive mark is read as UTC.
    row.updated_from_event_at = datetime(2026, 10, 10)
    assert await _dispatch(_event(event_id="evt_4", occurred_at=NOW + DAY), store, payers) is DispatchOutcome.STALE


async def test_a_notice_links_but_never_moves_the_mark() -> None:
    """Lemon Squeezy's payment_failed carries no state; a late one cannot hide a newer one."""
    store, payers = (
        _Store(),
        _Payers({"user:1": Row(status=SubscriptionStatus.ACTIVE, updated_from_event_at=NOW + DAY)}),
    )
    notice = _event(
        provider=BillingProvider.LEMONSQUEEZY,
        kind=EventKind.PAYMENT_FAILED,
        status=None,
        plan_price_id=None,
        current_period_end=None,
        cancel_at_period_end=None,
    )
    assert await _dispatch(notice, store, payers) is DispatchOutcome.APPLIED
    assert payers.applied == [
        (
            EventKind.PAYMENT_FAILED,
            {
                "provider": BillingProvider.LEMONSQUEEZY,
                "provider_customer_id": "ctm_1",
                "provider_subscription_id": "sub_1",
            },
        )
    ]
    assert payers.rows["user:1"].updated_from_event_at == NOW + DAY


async def test_no_payer_is_recorded_unprocessed() -> None:
    store, payers = _Store(), _Payers()
    assert await _dispatch(_event(custom_data={}), store, payers) is DispatchOutcome.NO_PAYER
    assert store.rows["paddle", "evt_1"][2] is None and store.calls == ["seen", "record"]


async def test_an_unknown_price_is_not_configured_and_retried() -> None:
    store, payers = _Store(), _Payers({"user:1": _trial()})
    with pytest.raises(BillingError) as refused:
        await _dispatch(_event(plan_price_id="pri_unknown"), store, payers)
    assert (refused.value.status_code, refused.value.code) == (503, BillingErrorCode.BILLING_NOT_CONFIGURED)
    assert payers.applied == []
    # A retired price still finds its plan; a snapshot without a price keeps the plan.
    assert (
        await _dispatch(_event(event_id="evt_2", plan_price_id="pri_pro_y_2025"), store, payers)
        is DispatchOutcome.APPLIED
    )
    row = payers.rows["user:1"]
    row.plan_code = "legacy"
    assert await _dispatch(_event(event_id="evt_3", plan_price_id=None, occurred_at=NOW + DAY), store, payers) is (
        DispatchOutcome.APPLIED
    )
    assert row.plan_code == "legacy"


# --- §12.11: an operator's grant beats provider events -----------------------------------


async def test_a_running_grant_keeps_its_status_but_takes_the_link() -> None:
    beta = Row(
        status=SubscriptionStatus.COMPED, source=SubscriptionSource.BETA, plan_code="pro", comped_until=NOW + 5 * DAY
    )
    store, payers = _Store(), _Payers({"user:1": beta})
    expired = _event(status=SubscriptionStatus.CANCELED, plan_price_id="pri_unknown", current_period_end=NOW - DAY)
    assert await _dispatch(expired, store, payers) is DispatchOutcome.GRANT_HOLDS
    assert (beta.status, beta.source, beta.plan_code) == (SubscriptionStatus.COMPED, SubscriptionSource.BETA, "pro")
    assert in_good_standing(beta, NOW + 4 * DAY) and not in_good_standing(beta, NOW + 5 * DAY)
    # The payer subscribes during the grant: from its end on, the provider's period counts.
    started = _event(event_id="evt_2", occurred_at=NOW + DAY, subscription_ref="sub_2")
    assert await _dispatch(started, store, payers) is DispatchOutcome.GRANT_HOLDS
    assert beta.status is SubscriptionStatus.COMPED and beta.provider_subscription_id == "sub_2"
    assert in_good_standing(beta, NOW + 100 * DAY)
    # After the grant, provider events apply again.
    renewed = _event(event_id="evt_3", occurred_at=NOW + 6 * DAY, subscription_ref="sub_2")
    assert await _dispatch(renewed, store, payers, now=NOW + 6 * DAY) is DispatchOutcome.APPLIED
    assert (beta.status, beta.source) == (SubscriptionStatus.ACTIVE, SubscriptionSource.PROVIDER)


async def test_a_grant_without_an_end_always_holds() -> None:
    """§12.8: the operator's admin accounts — comped, manual, no end."""
    admin = Row(status=SubscriptionStatus.COMPED, source=SubscriptionSource.MANUAL)
    store, payers = _Store(), _Payers({"user:1": admin})
    assert await _dispatch(_event(), store, payers, now=NOW + 999 * DAY) is DispatchOutcome.GRANT_HOLDS
    assert admin.status is SubscriptionStatus.COMPED


# --- the payer's subscription --------------------------------------------------------------


async def test_a_replaced_subscriptions_late_end_does_not_lapse_the_new_one() -> None:
    row = Row(status=SubscriptionStatus.ACTIVE, source=SubscriptionSource.PROVIDER, provider_subscription_id="sub_2")
    store, payers = _Store(), _Payers({"user:1": row})
    old_end = _event(kind=EventKind.SUBSCRIPTION_EXPIRED, status=SubscriptionStatus.CANCELED, subscription_ref="sub_1")
    assert await _dispatch(old_end, store, payers) is DispatchOutcome.OTHER_SUBSCRIPTION
    assert row.status is SubscriptionStatus.ACTIVE and row.provider_subscription_id == "sub_2"
    notice = _event(event_id="evt_2", kind=EventKind.PAYMENT_FAILED, status=None, subscription_ref="sub_1")
    assert await _dispatch(notice, store, payers) is DispatchOutcome.OTHER_SUBSCRIPTION
    # A new subscription in good standing takes the row's link.
    again = _event(event_id="evt_3", subscription_ref="sub_3", occurred_at=NOW + DAY)
    assert await _dispatch(again, store, payers) is DispatchOutcome.APPLIED and row.provider_subscription_id == "sub_3"
    # An event without a subscription id is the row's own.
    bare = _event(
        event_id="evt_4", subscription_ref=None, occurred_at=NOW + 2 * DAY, status=SubscriptionStatus.PAST_DUE
    )
    assert await _dispatch(bare, store, payers) is DispatchOutcome.APPLIED
    status: object = row.status  # mypy narrowed it to ACTIVE above; apply changed it
    assert status is SubscriptionStatus.PAST_DUE


async def test_dispatch_defaults_to_the_current_time() -> None:
    store, payers = _Store(), _Payers({"user:1": _trial()})
    port: EventStore = store
    outcome = await dispatch(
        _event(), port, payers.apply, load=payers.load, plan_for_price=SETTINGS.billing_plan_for_price
    )
    assert outcome is DispatchOutcome.APPLIED
    received = store.rows["paddle", "evt_1"][1]
    assert isinstance(received, datetime) and received.tzinfo is UTC and received > NOW - 3650 * DAY


def test_row_changes_names_the_contracts_columns() -> None:
    """§3.2's columns, and nothing a grant set while it holds."""
    assert set(row_changes(_event(), plan_code="pro")) == {
        "provider",
        "provider_customer_id",
        "provider_subscription_id",
        "current_period_end",
        "cancel_at_period_end",
        "updated_from_event_at",
        "status",
        "source",
        "plan_code",
    }
    held = row_changes(_event(customer_ref=None, current_period_end=None, cancel_at_period_end=None), grant_holds=True)
    assert held == {
        "provider": BillingProvider.PADDLE,
        "provider_subscription_id": "sub_1",
        "updated_from_event_at": NOW,
    }


# --- the answer policy -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "answer"),
    [
        (None, WebhookAnswer(200, log=False)),  # an event billing doesn't use
        (DispatchOutcome.APPLIED, WebhookAnswer(200, log=False)),
        (DispatchOutcome.DUPLICATE, WebhookAnswer(200, log=False)),
        (DispatchOutcome.STALE, WebhookAnswer(200, log=False)),
        (DispatchOutcome.GRANT_HOLDS, WebhookAnswer(200, log=False)),
        (DispatchOutcome.OTHER_SUBSCRIPTION, WebhookAnswer(200, log=False)),
        (DispatchOutcome.NO_PAYER, WebhookAnswer(200, log=True)),
        (PoisonEventError("data.status"), WebhookAnswer(200, log=True)),  # poison: 2xx and logged
        (BillingError(BillingErrorCode.INVALID_SIGNATURE), WebhookAnswer(400, log=True)),
        (BillingError(BillingErrorCode.BILLING_DISABLED), WebhookAnswer(404, log=False)),
        (BillingError(BillingErrorCode.BILLING_NOT_CONFIGURED), WebhookAnswer(503, log=True)),
        (ConnectionError("database"), WebhookAnswer(500, log=True)),  # transient: retried
    ],
)
def test_the_answer_policy(result: DispatchOutcome | BaseException | None, answer: WebhookAnswer) -> None:
    """§5: duplicate or unknown → 2xx, poison → 2xx and logged, transient → 5xx."""
    assert webhook_answer(result) == answer
