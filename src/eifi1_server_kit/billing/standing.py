"""The subscription row's vocabulary, good standing, and the dates a row starts with.

``docs/billing-harmonization.md`` §3.2, §3.3 and §12.7–§12.11 in ``Eifi1/ui-kit``. The row
itself is the app's table — one per payer, created WITH the payer (§3.2, §12.12) — and this
module reads it through :class:`SubscriptionRow`, a protocol any ORM row satisfies.

**Good standing decides whether the payer's data may change** (§3.3). Out of it, the
payer's data is read-only — never deleted, hidden or locked; a later plan opens it all
again. Whose standing counts is the app's question, answered per write (§12.1): keksdose's
budget OWNER's, Kurvenschmiede's row owner's (the creator's for a create), kastlan's acting
company's.
"""

from __future__ import annotations

import calendar
import enum
from datetime import datetime, timedelta
from typing import Protocol

from eifi1_server_kit.auth.tokens import _aware

__all__ = [
    "BETA_FREE_MONTHS",
    "TRIAL_LENGTH",
    "SubscriptionRow",
    "SubscriptionSource",
    "SubscriptionStatus",
    "beta_comped_until",
    "grant_holds",
    "in_good_standing",
    "is_beta",
    "trial_ends_at",
]

#: A new payer's trial, without a card (§2.7).
TRIAL_LENGTH = timedelta(days=30)
#: A beta payer's free months from the day billing goes on (§2.4).
BETA_FREE_MONTHS = 12


class SubscriptionStatus(enum.StrEnum):
    """The row's ``status`` (§3.2). A ``StrEnum``, so ``str(status)`` is the stored value;
    the kit's ``SubscriptionStatusChip`` reads the same words."""

    #: The cardless trial (§2.7), until ``trial_ends_at`` — or a provider's own trial.
    TRIALING = "trialing"
    #: Paid, and renewing — or cancelled for the period's end (``cancel_at_period_end``).
    ACTIVE = "active"
    #: A renewal failed; the provider is retrying. In good standing while it does (§3.3).
    PAST_DUE = "past_due"
    #: The provider ended the subscription (Paddle's ``canceled``). Not in good standing.
    CANCELED = "canceled"
    #: It ran out: Lemon Squeezy's ``expired`` and ``unpaid``, a paused collection, or a
    #: trial or grant the app's daily job wrote off. Not in good standing.
    EXPIRED = "expired"
    #: Free, by an operator's grant or the beta's 12 months, until ``comped_until`` — or for
    #: good without one (the operator's own admin accounts, §12.8).
    COMPED = "comped"


class SubscriptionSource(enum.StrEnum):
    """The row's ``source`` (§3.2): where its current state came from."""

    #: The cardless trial every new payer starts with (§3.2).
    TRIAL = "trial"
    #: A provider's webhook (§5).
    PROVIDER = "provider"
    #: An operator's grant (§6), with or without an end.
    MANUAL = "manual"
    #: The beta's 12 free months (§2.4).
    BETA = "beta"


class SubscriptionRow(Protocol):
    """What the kit reads of the app's subscription row (§3.2). An ORM row satisfies it as
    it is; the provider's ids and ``plan_code`` are the app's columns too, read where the
    kit needs them.

    Naive datetimes are read as UTC, as everywhere in the kit (kastlan's columns are naive).
    """

    @property
    def plan_code(self) -> str: ...

    @property
    def status(self) -> SubscriptionStatus | str: ...

    @property
    def source(self) -> SubscriptionSource | str: ...

    @property
    def trial_ends_at(self) -> datetime | None: ...

    @property
    def comped_until(self) -> datetime | None: ...

    @property
    def current_period_end(self) -> datetime | None: ...

    @property
    def cancel_at_period_end(self) -> bool | None: ...

    @property
    def provider_subscription_id(self) -> str | None: ...

    @property
    def updated_from_event_at(self) -> datetime | None: ...


def grant_holds(row: SubscriptionRow, now: datetime) -> bool:
    """Is a free grant running — ``comped`` before ``comped_until``, or without one?

    While it is, a provider event does not overwrite the row (§12.11): an operator's grant
    — or the beta's — beats the provider, and the provider's state applies from
    ``comped_until`` on (:func:`in_good_standing`).
    """
    if SubscriptionStatus(row.status) is not SubscriptionStatus.COMPED:
        return False
    return row.comped_until is None or _aware(now) < _aware(row.comped_until)


def _paid_until(row: SubscriptionRow, moment: datetime) -> bool:
    """The provider's paid period still runs at ``moment``."""
    return row.current_period_end is not None and moment < _aware(row.current_period_end)


def in_good_standing(row: SubscriptionRow | None, now: datetime, *, retry_grace: timedelta | None = None) -> bool:
    """May the payer's data change at ``now``? §3.3, with §12.7 and §12.11.

    * **No row: always** (§12.7) — demo users, the demo's system account, ownerless items
      such as keksdose's preview budget. They are never gated and never counted. Every real
      payer has a row (§12.12), so ``None`` means one of these, never "forgotten".
    * ``active`` — yes; but a subscription set to cancel at its period's end is out once
      that end has passed, even before the provider's final event arrives (it may be late;
      a cancelled period cannot renew).
    * ``trialing`` — the cardless trial (``source`` ``trial``) before ``trial_ends_at``, or
      while it is empty: a pure guest's row waits for their first owned item to start it
      (§12.9). A provider's own trial counts as ``active``.
    * ``comped`` — before ``comped_until``, or without one; after it, while the provider's
      paid period runs (``current_period_end``): a payer who subscribed during the grant
      — whose provider events left the grant in place (§12.11) — is in good standing from
      the grant's end on, as the provider says.
    * ``past_due`` — yes: the provider retries, and moves it on when it gives up (Paddle to
      ``canceled`` or ``paused``, Lemon Squeezy after 4 retries over 2 weeks to ``unpaid``).
      With ``retry_grace``, also no later than ``current_period_end`` plus the grace — a
      backstop for a provider event that never arrived.
    * ``canceled``, ``expired`` — no.

    Billing switched off means everyone is in good standing (§4):
    :meth:`~eifi1_server_kit.billing.BillingSettings.billing_standing` asks the switch
    first. An unknown ``status`` is a :class:`ValueError`.
    """
    if row is None:
        return True
    moment = _aware(now)
    status = SubscriptionStatus(row.status)
    if status is SubscriptionStatus.TRIALING and SubscriptionSource(row.source) is not SubscriptionSource.PROVIDER:
        return row.trial_ends_at is None or moment < _aware(row.trial_ends_at)
    if status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        return not row.cancel_at_period_end or row.current_period_end is None or _paid_until(row, moment)
    if status is SubscriptionStatus.COMPED:
        return grant_holds(row, moment) or _paid_until(row, moment)
    if status is SubscriptionStatus.PAST_DUE:
        if retry_grace is None or row.current_period_end is None:
            return True
        return moment < _aware(row.current_period_end) + retry_grace
    return False


def trial_ends_at(now: datetime) -> datetime:
    """When a trial started at ``now`` ends: 30 days on (§2.7, :data:`TRIAL_LENGTH`).

    Set where the payer is created (§12.12) — except for keksdose's pure guest, whose row is
    created with an empty ``trial_ends_at`` and given one at their first OWNED item
    (§12.9). Naive in, naive out.
    """
    return now + TRIAL_LENGTH


def _add_months(moment: datetime, months: int) -> datetime:
    """``moment`` plus calendar months, the day clamped to the month's last."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def beta_comped_until(launch: datetime) -> datetime:
    """When a beta payer's free months end: 12 calendar months after the launch (§2.4,
    :data:`BETA_FREE_MONTHS`) — the same date for every beta payer of the app.

    Calendar months, not 365 days: launched on 1 March, free until 1 March. A launch on
    29 February ends on 28 February. Naive in, naive out.
    """
    return _add_months(launch, BETA_FREE_MONTHS)


def is_beta(invitation_created_at: datetime | None, launch: datetime) -> bool:
    """Does a payer registering now count as beta (§12.10)? Yes when their invitation was
    CREATED before the launch, even if it is accepted after — the last week's invitees get
    12 months, not 30 days. No invitation (open registration after launch): no.

    The payers who exist when billing goes on are beta by the migration (§3.2), which needs
    no question.
    """
    return invitation_created_at is not None and _aware(invitation_created_at) < _aware(launch)
