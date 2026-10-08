"""A subscription row as an app's ORM would hand it over (billing §3.2) — for the tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from eifi1_server_kit.billing import SubscriptionRow, SubscriptionSource, SubscriptionStatus

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@dataclass
class Row:
    """Every column of §3.2; ``payer`` is the app's own key (a user, a company)."""

    payer: str = "user:1"
    plan_code: str = "free"
    status: SubscriptionStatus | str = SubscriptionStatus.TRIALING
    source: SubscriptionSource | str = SubscriptionSource.TRIAL
    trial_ends_at: datetime | None = None
    comped_until: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool | None = False
    provider: str | None = None
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None
    updated_from_event_at: datetime | None = None


def _satisfies_the_protocol(row: Row) -> SubscriptionRow:
    """mypy checks that the dataclass is a :class:`SubscriptionRow`."""
    return row
