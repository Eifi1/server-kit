"""Trial and grant end notices: which payer is owed which notice, and when.

``docs/billing-harmonization.md`` §12.22 and §14.7 (decision 23) in ``Eifi1/ui-kit``. A
cardless trial never reaches the provider, so nobody but the app tells its payer that the
trial or a free grant is about to end — 7 days before (:data:`NOTICE_AHEAD`), and once it
has ended. The kit only DECIDES: :func:`billing_notice_due` answers per row with the kind,
the end and the days left, and knows no recipient, no address and no words. The app turns
``(payer, kind, ends_at, days_left)`` into its own mail, in every locale it has, through
its :class:`~eifi1_server_kit.mail.Mailer` with ``kind=str(notice.kind)``.

**The app's job**, wherever it runs (a token route behind ``X-Jobs-Token`` for a Cloud
Scheduler HTTP job, keksdose's Cloud Run Job, a local CLI)::

    async def billing_notices(session, now: datetime) -> int:
        if not settings.billing_enabled:
            return 0                                             # off: nothing, and a 200
        sent = 0
        rows = await bypass_rls(session).scalars(
            select(Subscription).where(Subscription.status.in_(NOTICE_STATUSES))
        )
        for row in rows:                                         # few rows: filter in Python
            payer = await payer_of(session, row)
            if payer.deactivated or payer.deletion_requested_at is not None:
                continue                                         # a deactivated account is sent nothing
            notice = billing_notice_due(row, now, launch=settings.billing_launch_at, sent=row.billing_notice_sent)
            if notice is None:
                continue
            if await mailer.send(payer.email, notice_mail(payer.locale, notice), kind=str(notice.kind)):
                row.billing_notice_sent = notice.key             # the marker: once per notice
                sent += 1
            await session.commit()                               # per row
        return sent

:func:`billing_notices_owed` is the same decision over the app's candidates, the switch and
the skipped accounts included, for an app that gathers them first.

**The marker** is a column on each app's subscription row, ``billing_notice_sent
VARCHAR(64) NULL``, holding the last notice's :attr:`BillingNotice.key`: the job is then
idempotent across missed runs, Cloud Scheduler's retries and double runs, and a moved end
(an operator's longer grant) is a new key, so a new notice.

An English reference text, for the app's own ``MailText`` per kind (the words are the
app's, ``mail.py``'s rule):

* ``trial_ending`` — "Your trial ends on {date}. Choose a plan to keep creating and
  changing; everything you have stays yours to read and export either way."
* ``trial_ended`` — "Your trial ended on {date}. Your account is read-only until you choose
  a plan: you can still sign in, read and export everything."
* ``grant_ending`` — "Your free access ends on {date} ({days} days). Choose a plan to keep
  creating and changing; nothing is deleted either way."
* ``grant_ended`` — "Your free access ended on {date}. Your account is read-only until you
  choose a plan: you can still sign in, read and export everything."
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.billing.settings import BillingSettings
from eifi1_server_kit.billing.standing import (
    SubscriptionRow,
    SubscriptionSource,
    SubscriptionStatus,
    effective_comped_until,
)

__all__ = [
    "NOTICE_AHEAD",
    "NOTICE_LATE_LIMIT",
    "NOTICE_STATUSES",
    "BillingNotice",
    "BillingNoticeKind",
    "NoticeCandidate",
    "OwedNotice",
    "billing_notice_due",
    "billing_notices_owed",
]

#: How long before the end the ``…_ending`` notice is due (§12.22).
NOTICE_AHEAD = timedelta(days=7)
#: An ``…_ended`` notice no later than this after the end: a first run, or a job that was
#: down for a while, doesn't mail every long-ended trial.
NOTICE_LATE_LIMIT = timedelta(days=3)
#: The statuses a notice can be owed in, for the app's candidate query (``status IN …``).
NOTICE_STATUSES = frozenset({SubscriptionStatus.TRIALING, SubscriptionStatus.COMPED})

_DAY = timedelta(days=1)


class BillingNoticeKind(enum.StrEnum):
    """Which notice (§12.22). A ``StrEnum``: ``str(kind)`` is the word, the ``kind`` the
    app's :class:`~eifi1_server_kit.mail.Mailer` logs."""

    #: The cardless trial ends within :data:`NOTICE_AHEAD`.
    TRIAL_ENDING = "trial_ending"
    #: The cardless trial has ended: the payer is read-only (§2.7).
    TRIAL_ENDED = "trial_ended"
    #: A free grant — the beta's 12 months or an operator's — ends within
    #: :data:`NOTICE_AHEAD`.
    GRANT_ENDING = "grant_ending"
    #: A free grant has ended, and no paid period follows it.
    GRANT_ENDED = "grant_ended"


class BillingNotice(NamedTuple):
    """The notice a row is owed: its kind, the end it speaks of, and the days left."""

    kind: BillingNoticeKind
    #: The trial's or the grant's end, aware, in UTC.
    ends_at: datetime
    #: Whole days from now to ``ends_at``, rounded down; 0 from the end on.
    days_left: int

    @property
    def key(self) -> str:
        """What the row's ``billing_notice_sent`` stores once the mail went out:
        ``"trial_ending:2026-11-07T09:00Z"`` — the kind and the end to the minute, at most
        30 characters."""
        return f"{self.kind}:{_aware(self.ends_at).astimezone(UTC):%Y-%m-%dT%H:%MZ}"


def _due(
    end: datetime,
    ending: BillingNoticeKind,
    ended: BillingNoticeKind,
    now: datetime,
    ahead: timedelta,
    late_limit: timedelta,
) -> BillingNotice | None:
    if now < end - ahead or now > end + late_limit:
        return None
    if now < end:
        return BillingNotice(ending, end, (end - now) // _DAY)
    return BillingNotice(ended, end, 0)


def billing_notice_due(
    row: SubscriptionRow,
    now: datetime,
    *,
    launch: datetime | None,
    sent: str | None,
    ahead: timedelta = NOTICE_AHEAD,
    late_limit: timedelta = NOTICE_LATE_LIMIT,
) -> BillingNotice | None:
    """The notice ``row`` is owed at ``now``, or ``None`` (§12.22, §14.7).

    * **The trial** is the cardless one: ``trialing``, ``source`` not ``provider``,
      ``trial_ends_at`` set. A provider's own trial is the provider's to mail, and a pure
      guest's row with an empty ``trial_ends_at`` (§12.9) has nothing to end.
    * **The grant** is a ``comped`` row's, ending at
      :func:`~eifi1_server_kit.billing.effective_comped_until`: a beta row stored without
      an end ends at ``launch`` (the settings' ``billing_launch_at``) plus 12 months, and
      without a launch date it has no end yet — so no notice. A grant without an end (the
      operator's own accounts) is owed none.
    * **No grant notice while the provider's paid period runs past the grant's end**
      (``current_period_end`` later than it): that payer already pays (§12.11).
    * ``…_ending`` from ``ahead`` before the end; ``…_ended`` from the end until
      ``late_limit`` after it. A missed ``…_ending`` is skipped, never sent after the end.
    * ``None`` when ``sent`` — the row's ``billing_notice_sent`` — is this notice's
      :attr:`~BillingNotice.key`: it went out already.

    Any other status is owed nothing. Naive datetimes are read as UTC; ``ends_at`` is
    aware. Pure: the switch, the skipped accounts, the mail and the marker are the app's
    (:func:`billing_notices_owed` does the first two).
    """
    moment = _aware(now)
    status = SubscriptionStatus(row.status)
    if status is SubscriptionStatus.TRIALING:
        if SubscriptionSource(row.source) is SubscriptionSource.PROVIDER or row.trial_ends_at is None:
            return None
        notice = _due(
            _aware(row.trial_ends_at),
            BillingNoticeKind.TRIAL_ENDING,
            BillingNoticeKind.TRIAL_ENDED,
            moment,
            ahead,
            late_limit,
        )
    elif status is SubscriptionStatus.COMPED:
        until = effective_comped_until(row, launch)
        if until is None:
            return None
        end = _aware(until)
        if row.current_period_end is not None and _aware(row.current_period_end) > end:
            return None
        notice = _due(end, BillingNoticeKind.GRANT_ENDING, BillingNoticeKind.GRANT_ENDED, moment, ahead, late_limit)
    else:
        return None
    if notice is None or notice.key == sent:
        return None
    return notice


class NoticeCandidate[PayerT](NamedTuple):
    """One payer the app's notice job considers (§14.7): the app's own ``payer`` (a user,
    a company, or whatever it mails from), the payer's subscription ``row``, and whether
    the account is ``deactivated`` or has a ``deletion_requested`` — both are sent nothing
    (keksdose's and Kurvenschmiede's rule; kastlan's company flagged for deletion, or
    without an active admin). ``sent`` is the row's ``billing_notice_sent``."""

    payer: PayerT
    row: SubscriptionRow
    sent: str | None = None
    deactivated: bool = False
    deletion_requested: bool = False


class OwedNotice[PayerT](NamedTuple):
    """A notice owed to a payer: :class:`NoticeCandidate`'s ``payer`` and the
    :class:`BillingNotice`'s fields — the ``(payer, kind, ends_at, days_left)`` the app
    turns into its mail."""

    payer: PayerT
    kind: BillingNoticeKind
    #: The trial's or the grant's end, aware, in UTC.
    ends_at: datetime
    #: Whole days from now to ``ends_at``, rounded down; 0 from the end on.
    days_left: int

    @property
    def key(self) -> str:
        """The marker to store once the mail went out (:attr:`BillingNotice.key`)."""
        return BillingNotice(self.kind, self.ends_at, self.days_left).key


def billing_notices_owed[PayerT](
    candidates: Iterable[NoticeCandidate[PayerT]],
    now: datetime,
    *,
    settings: BillingSettings,
    ahead: timedelta = NOTICE_AHEAD,
    late_limit: timedelta = NOTICE_LATE_LIMIT,
) -> list[OwedNotice[PayerT]]:
    """The notices owed at ``now`` among ``candidates``, in their order (§14.7): nothing
    while billing is off (``settings.billing_enabled``), no deactivated account and none
    with a deletion request, and for the rest :func:`billing_notice_due` with the
    settings' ``billing_launch_at`` and each candidate's ``sent`` marker. Pure: the app
    sends each mail and stores :attr:`OwedNotice.key` once it went out."""
    if not settings.billing_enabled:
        return []
    owed: list[OwedNotice[PayerT]] = []
    for candidate in candidates:
        if candidate.deactivated or candidate.deletion_requested:
            continue
        notice = billing_notice_due(
            candidate.row,
            now,
            launch=settings.billing_launch_at,
            sent=candidate.sent,
            ahead=ahead,
            late_limit=late_limit,
        )
        if notice is not None:
            owed.append(OwedNotice(candidate.payer, *notice))
    return owed
