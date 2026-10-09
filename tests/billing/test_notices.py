"""Trial and grant end notices (billing contract §12.22, §14.7): which payer is owed which
notice, when, and once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from eifi1_server_kit.billing import (
    NOTICE_AHEAD,
    NOTICE_LATE_LIMIT,
    NOTICE_STATUSES,
    BillingNotice,
    BillingNoticeKind,
    BillingSettings,
    NoticeCandidate,
    OwedNotice,
    SubscriptionSource,
    SubscriptionStatus,
    billing_notice_due,
    billing_notices_owed,
)
from tests.billing._rows import Row

DAY = timedelta(days=1)
END = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
LAUNCH = datetime(2025, 11, 7, 9, 0, tzinfo=UTC)  # a beta row without an end ends at END
ON: dict[str, Any] = {
    "billing_enabled": True,
    "billing_provider": "paddle",
    "billing_api_key": "pdl_sdbx_apikey_example",
    "billing_webhook_secret": "pdl_ntfset_example",
    "billing_app": "garden",
}


def _trial(**fields: Any) -> Row:
    return Row(**({"trial_ends_at": END} | fields))


def _grant(**fields: Any) -> Row:
    base: dict[str, Any] = {"status": "comped", "source": "manual", "comped_until": END}
    return Row(**(base | fields))


def _due(row: Row, now: datetime, *, sent: str | None = None, launch: datetime | None = None) -> BillingNotice | None:
    return billing_notice_due(row, now, launch=launch, sent=sent)


def test_the_constants_are_the_contracts() -> None:
    assert (timedelta(days=7), timedelta(days=3)) == (NOTICE_AHEAD, NOTICE_LATE_LIMIT)
    assert {SubscriptionStatus.TRIALING, SubscriptionStatus.COMPED} == NOTICE_STATUSES
    assert [str(kind) for kind in BillingNoticeKind] == ["trial_ending", "trial_ended", "grant_ending", "grant_ended"]


def test_a_trial_is_told_a_week_ahead_and_at_its_end() -> None:
    assert _due(_trial(), END - 7 * DAY - timedelta(minutes=1)) is None
    assert _due(_trial(), END - 7 * DAY) == BillingNotice(BillingNoticeKind.TRIAL_ENDING, END, 7)
    assert _due(_trial(), END - timedelta(days=6, hours=21)) == BillingNotice(BillingNoticeKind.TRIAL_ENDING, END, 6)
    assert _due(_trial(), END - timedelta(minutes=1)) == BillingNotice(BillingNoticeKind.TRIAL_ENDING, END, 0)
    assert _due(_trial(), END) == BillingNotice(BillingNoticeKind.TRIAL_ENDED, END, 0)
    assert _due(_trial(), END + 3 * DAY) == BillingNotice(BillingNoticeKind.TRIAL_ENDED, END, 0)


def test_a_long_ended_trial_is_not_mailed() -> None:
    """A first run, or a job that was down, doesn't mail every long-ended trial."""
    assert _due(_trial(), END + 3 * DAY + timedelta(seconds=1)) is None


def test_the_marker_makes_a_notice_once() -> None:
    """Idempotent across missed runs, Cloud Scheduler's retries and double runs."""
    ending = _due(_trial(), END - 2 * DAY)
    assert ending is not None and ending.key == "trial_ending:2026-11-07T09:00Z"
    assert _due(_trial(), END - DAY, sent=ending.key) is None
    ended = _due(_trial(), END + DAY, sent=ending.key)
    assert ended is not None and ended.key == "trial_ended:2026-11-07T09:00Z"
    assert _due(_trial(), END + 2 * DAY, sent=ended.key) is None
    # A moved end is a new notice.
    later = _trial(trial_ends_at=END + 2 * DAY)
    assert _due(later, END + DAY, sent=ended.key) == BillingNotice(BillingNoticeKind.TRIAL_ENDING, END + 2 * DAY, 1)
    assert len(BillingNotice(BillingNoticeKind.GRANT_ENDING, END, 0).key) <= 64  # VARCHAR(64)


def test_a_naive_end_is_read_as_utc_and_answered_aware() -> None:
    """kastlan's columns are naive."""
    notice = _due(_trial(trial_ends_at=datetime(2026, 11, 7, 9, 0)), datetime(2026, 11, 5, 9, 0))
    assert notice == BillingNotice(BillingNoticeKind.TRIAL_ENDING, END, 2)
    assert notice is not None and notice.ends_at.tzinfo is UTC
    in_zurich = BillingNotice(BillingNoticeKind.TRIAL_ENDED, END.astimezone(timezone(timedelta(hours=1))), 0)
    assert in_zurich.key == "trial_ended:2026-11-07T09:00Z"  # the key is in UTC whatever the zone


@pytest.mark.parametrize(
    "row",
    [
        _trial(source="provider"),  # the provider's own trial is the provider's to mail
        _trial(trial_ends_at=None),  # a pure guest waits for their first owned item (§12.9)
        _trial(status="active", source="provider"),
        _trial(status="past_due", source="provider"),
        _trial(status="expired"),
        _trial(status="canceled"),
    ],
)
def test_no_trial_notice_but_for_the_cardless_trial(row: Row) -> None:
    assert _due(row, END - DAY) is None


def test_a_grant_is_told_a_week_ahead_and_at_its_end() -> None:
    assert _due(_grant(), END - 3 * DAY) == BillingNotice(BillingNoticeKind.GRANT_ENDING, END, 3)
    assert _due(_grant(), END + DAY) == BillingNotice(BillingNoticeKind.GRANT_ENDED, END, 0)
    assert _due(_grant(), END + 4 * DAY) is None


def test_a_beta_rows_end_is_the_launch_plus_twelve_months() -> None:
    """§3.2: kastlan's ``due_notices`` read the stored end only, wrong for a beta row
    stored without one."""
    beta = _grant(source=SubscriptionSource.BETA, comped_until=None)
    assert _due(beta, END - DAY, launch=LAUNCH) == BillingNotice(BillingNoticeKind.GRANT_ENDING, END, 1)
    assert _due(beta, END - DAY) is None  # no launch date yet: no end yet


def test_a_grant_without_an_end_is_owed_nothing() -> None:
    """The operator's own accounts (§12.8)."""
    assert _due(_grant(comped_until=None), END) is None


def test_no_grant_notice_while_the_payer_already_pays_past_it() -> None:
    """§12.11: a payer who bought under the grant goes on paying when it ends."""
    assert _due(_grant(current_period_end=END + 300 * DAY), END - DAY) is None
    assert _due(_grant(current_period_end=END - 30 * DAY), END - DAY) is not None  # an old period
    assert _due(_grant(current_period_end=END), END - DAY) is not None


# --- over the app's candidates ---------------------------------------------------------------


def test_the_payers_owed_a_notice() -> None:
    """§14.7: (payer, kind, ends_at, days_left) for the app's mail; the marker to store."""
    candidates = [
        NoticeCandidate("ada", _trial()),
        NoticeCandidate("bea", _trial(), sent="trial_ending:2026-11-07T09:00Z"),  # told already
        NoticeCandidate("cem", _trial(), deactivated=True),  # a deactivated account is sent nothing
        NoticeCandidate("dan", _trial(), deletion_requested=True),  # leaving: nothing either
        NoticeCandidate("eva", _grant(source="beta", comped_until=None)),  # the launch's end
        NoticeCandidate("fay", Row(status="active", source="provider")),
    ]
    owed = billing_notices_owed(candidates, END - 2 * DAY, settings=BillingSettings(billing_launch_at=LAUNCH, **ON))
    assert owed == [
        OwedNotice("ada", BillingNoticeKind.TRIAL_ENDING, END, 2),
        OwedNotice("eva", BillingNoticeKind.GRANT_ENDING, END, 2),
    ]
    assert [notice.key for notice in owed] == ["trial_ending:2026-11-07T09:00Z", "grant_ending:2026-11-07T09:00Z"]
    assert (owed[0].payer, owed[0].kind, owed[0].ends_at, owed[0].days_left) == ("ada", "trial_ending", END, 2)


def test_nothing_is_owed_while_billing_is_off() -> None:
    """§14.7: the job returns at once, and its route answers 200 with nothing sent."""
    assert billing_notices_owed([NoticeCandidate(1, _trial())], END, settings=BillingSettings()) == []


def test_the_window_can_be_the_apps() -> None:
    owed = billing_notices_owed(
        [NoticeCandidate(1, _trial())], END - 10 * DAY, settings=BillingSettings(**ON), ahead=14 * DAY
    )
    assert owed == [OwedNotice(1, BillingNoticeKind.TRIAL_ENDING, END, 10)]
    late = billing_notices_owed(
        [NoticeCandidate(1, _trial())], END + 5 * DAY, settings=BillingSettings(**ON), late_limit=7 * DAY
    )
    assert [notice.kind for notice in late] == [BillingNoticeKind.TRIAL_ENDED]
