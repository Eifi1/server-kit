"""Good standing and the row's dates (billing contract §3.2, §3.3, §12.7–§12.11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eifi1_server_kit.billing import (
    BETA_FREE_MONTHS,
    TRIAL_LENGTH,
    SubscriptionSource,
    SubscriptionStatus,
    beta_comped_until,
    grant_holds,
    in_good_standing,
    is_beta,
    trial_ends_at,
)
from tests.billing._rows import NOW, Row, _satisfies_the_protocol

DAY = timedelta(days=1)
TRIALING, ACTIVE, PAST_DUE, CANCELED, EXPIRED, COMPED = (
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.CANCELED,
    SubscriptionStatus.EXPIRED,
    SubscriptionStatus.COMPED,
)


def test_the_vocabularies_are_the_contracts() -> None:
    """§3.2's status and source columns."""
    assert [str(status) for status in SubscriptionStatus] == [
        "trialing",
        "active",
        "past_due",
        "canceled",
        "expired",
        "comped",
    ]
    assert [str(source) for source in SubscriptionSource] == ["trial", "provider", "manual", "beta"]
    assert _satisfies_the_protocol(Row()).status is TRIALING


def test_no_row_is_always_in_good_standing() -> None:
    """§12.7: demo users, the demo's system account, ownerless items."""
    assert in_good_standing(None, NOW)


def test_a_trial_counts_until_it_ends_and_while_it_has_not_started() -> None:
    assert in_good_standing(Row(trial_ends_at=NOW + DAY), NOW)
    assert not in_good_standing(Row(trial_ends_at=NOW), NOW)  # out AT the end
    # §12.9: a pure guest's row waits for their first owned item.
    assert in_good_standing(Row(trial_ends_at=None), NOW)
    # Naive columns read as UTC (kastlan's).
    assert in_good_standing(Row(trial_ends_at=datetime(2026, 10, 9)), NOW)
    assert not in_good_standing(Row(trial_ends_at=datetime(2026, 10, 7)), NOW.replace(tzinfo=None))


def test_a_paid_subscription_counts_until_a_cancelled_period_ends() -> None:
    provider = SubscriptionSource.PROVIDER
    assert in_good_standing(Row(status=ACTIVE, source=provider), NOW)
    assert in_good_standing(Row(status="active", source="provider", current_period_end=NOW - DAY), NOW)
    cancelled = Row(status=ACTIVE, source=provider, cancel_at_period_end=True, current_period_end=NOW + DAY)
    assert in_good_standing(cancelled, NOW)
    # The provider's final event may be late; a cancelled period cannot renew.
    assert not in_good_standing(cancelled, NOW + DAY)
    assert in_good_standing(Row(status=ACTIVE, source=provider, cancel_at_period_end=True), NOW)
    assert in_good_standing(Row(status=ACTIVE, source=provider, cancel_at_period_end=None), NOW)
    # A provider's own trial reads as a paid subscription, not as the app's cardless trial.
    assert in_good_standing(Row(status=TRIALING, source=provider, trial_ends_at=NOW - DAY), NOW)


def test_a_grant_counts_until_it_ends_and_then_the_providers_period() -> None:
    """§3.3, §12.8 (the operator's admins: no end), §12.11 (provider state after the grant)."""
    assert in_good_standing(Row(status=COMPED, source=SubscriptionSource.MANUAL), NOW)
    beta = Row(status=COMPED, source=SubscriptionSource.BETA, comped_until=NOW + DAY)
    assert in_good_standing(beta, NOW) and grant_holds(beta, NOW)
    assert not in_good_standing(beta, NOW + DAY) and not grant_holds(beta, NOW + DAY)
    subscribed = Row(
        status=COMPED, source=SubscriptionSource.BETA, comped_until=NOW, current_period_end=NOW + 300 * DAY
    )
    assert not grant_holds(subscribed, NOW) and in_good_standing(subscribed, NOW)
    assert not in_good_standing(subscribed, NOW + 300 * DAY)
    assert not grant_holds(Row(status=ACTIVE), NOW)


def test_past_due_counts_while_the_provider_retries() -> None:
    late = Row(status=PAST_DUE, source=SubscriptionSource.PROVIDER, current_period_end=NOW - 10 * DAY)
    assert in_good_standing(late, NOW)
    grace = timedelta(days=14)
    assert in_good_standing(late, NOW, retry_grace=grace)
    assert not in_good_standing(late, NOW + 4 * DAY, retry_grace=grace)
    assert in_good_standing(Row(status=PAST_DUE, current_period_end=None), NOW, retry_grace=grace)


@pytest.mark.parametrize("status", [CANCELED, EXPIRED, "canceled", "expired"])
def test_an_ended_subscription_is_read_only(status: SubscriptionStatus | str) -> None:
    assert not in_good_standing(Row(status=status, current_period_end=NOW + DAY, comped_until=NOW + DAY), NOW)


def test_an_unknown_status_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        in_good_standing(Row(status="grandfathered"), NOW)


def test_the_trial_is_30_days() -> None:
    """§2.7."""
    assert TRIAL_LENGTH.days == 30
    assert trial_ends_at(NOW) == datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert trial_ends_at(datetime(2026, 10, 8)).tzinfo is None


@pytest.mark.parametrize(
    ("launch", "until"),
    [
        (datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 11, 1, tzinfo=UTC)),
        (datetime(2027, 3, 31, 9, 30), datetime(2028, 3, 31, 9, 30)),
        (datetime(2028, 2, 29, tzinfo=UTC), datetime(2029, 2, 28, tzinfo=UTC)),
    ],
)
def test_the_beta_is_free_for_12_calendar_months(launch: datetime, until: datetime) -> None:
    """§2.4: from the day billing goes on."""
    assert BETA_FREE_MONTHS == 12
    assert beta_comped_until(launch) == until


def test_beta_follows_the_invitations_date_not_the_registrations() -> None:
    """§12.10: last week's invitees get 12 months, not 30 days."""
    launch = datetime(2026, 11, 1, tzinfo=UTC)
    assert is_beta(launch - DAY, launch)
    assert not is_beta(launch, launch)
    assert not is_beta(None, launch)
    assert is_beta(datetime(2026, 10, 31), launch)
