"""Deleting one's own account: deactivated at once, erased later — by a job or an operator.

``docs/user-admin-harmonization.md`` §2.1 and §6.4 in ``Eifi1/ui-kit``.

**Stage one, in every app**, at ``POST /auth/me/deletion``: the account is deactivated at
once (``is_active = false``, every session ended), ``deletion_requested_at`` and
``deletion_scheduled_at`` are set (:func:`deletion_schedule`), and the request is logged as
``deletion_request`` with the user as the actor. An admin's reactivation before the date
cancels it: both timestamps cleared, logged as ``deletion_cancel``.

**Stage two depends on the app's mode** (:class:`DeletionMode`, a setting with
``days``):

* ``after_days`` (keksdose, Kurvenschmiede; 30 days): a **Cloud Scheduler → Cloud Run
  Job**, not code in the service — the services scale to zero, so nothing in-process runs
  then. Daily, idempotent, ONE account per transaction (a failure holds back no other),
  writing ``erase`` to ``admin_actions`` in the same transaction. Inside the transaction
  it re-reads the row and asks :func:`deletion_due` — a reactivation since the query
  cleared ``deletion_requested_at``, and the answer is then ``False``.
* ``operator`` (kastlan): no date and no job; the operator erases from the platform
  (``type_email``, logged with no company).

**What may linger** is said in the confirming mail (:func:`deletion_mail_retention_note`).
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta
from typing import TypedDict

from eifi1_server_kit.auth.tokens import _aware

__all__ = [
    "BACKUP_RETENTION_DAYS",
    "DEFAULT_DELETION_DAYS",
    "LOG_RETENTION_DAYS",
    "DeletionMode",
    "RetentionNote",
    "deletion_due",
    "deletion_mail_retention_note",
    "deletion_schedule",
]

#: The window before the erasure (§2.1): the user's time to change their mind, the
#: admins' to hand shared work over (Kurvenschmiede), the guests' to export a shared
#: budget (keksdose).
DEFAULT_DELETION_DAYS = 30
#: How long a copy survives in the database backups and the deleted-files store after
#: the erasure: the shared Cloud SQL instance's 7 daily backups and 7-day point-in-time
#: recovery, and the uploads bucket's 7-day soft delete (§6.4, keksdose's production
#: figures).
BACKUP_RETENTION_DAYS = 7
#: How long the server logs keep a line (Cloud Logging's ``_Default`` bucket, §6.4).
LOG_RETENTION_DAYS = 30


class DeletionMode(enum.StrEnum):
    """Who erases a deleted account, and when — a per-app setting (§6.4)."""

    #: The scheduled job, ``days`` after the request.
    AFTER_DAYS = "after_days"
    #: An operator, by hand: company data is involved (kastlan).
    OPERATOR = "operator"


def _check_days(days: int) -> None:
    if days < 0:
        raise ValueError(f"a deletion window is zero days or more, not {days}")


def deletion_schedule(now: datetime, mode: DeletionMode | str, days: int = DEFAULT_DELETION_DAYS) -> datetime | None:
    """``deletion_scheduled_at`` for a request made at ``now``: ``now + days`` in
    ``after_days`` mode, ``None`` in ``operator`` mode (no date is promised). The
    datetime keeps ``now``'s form, naive or aware, for the app's column."""
    _check_days(days)
    if DeletionMode(mode) is DeletionMode.OPERATOR:
        return None
    return now + timedelta(days=days)


def deletion_due(
    requested_at: datetime | None,
    days: int,
    now: datetime,
    *,
    scheduled_at: datetime | None = None,
) -> bool:
    """Is an account whose deletion was requested at ``requested_at`` due for erasure at
    ``now``, ``days`` later? Due AT the boundary, as a token expires (§6.4).

    ``None`` — no request, or one an admin cancelled — is never due. Pass the row's
    ``scheduled_at`` too, and the erasure waits for BOTH: a window shortened in the
    settings after the request never erases before the date the mail promised. Naive
    datetimes are read as UTC.
    """
    _check_days(days)
    if requested_at is None:
        return False
    due_at = _aware(requested_at) + timedelta(days=days)
    if scheduled_at is not None:
        due_at = max(due_at, _aware(scheduled_at))
    return due_at <= _aware(now)


class RetentionNote(TypedDict):
    """The numbers the deletion mail states (§6.4), as placeholders for
    :meth:`~eifi1_server_kit.mail.MailText.render`."""

    backup_days: int
    log_days: int


def deletion_mail_retention_note(
    *,
    backup_days: int = BACKUP_RETENTION_DAYS,
    log_days: int = LOG_RETENTION_DAYS,
) -> RetentionNote:
    """What the deletion mail says may outlive the erasure (§6.4, §9.5), as
    ``{backup_days, log_days}`` for the mail's placeholders.

    The words are the app's; the contract's English is "copies in database backups and
    deleted files age out within {backup_days} days of the erasure, server logs within
    {log_days}". The defaults are the shared production figures; an app on other
    infrastructure passes its own::

        texts.render(link=link, date=erase_on, **kit.deletion_mail_retention_note())
    """
    for name, value in (("backup_days", backup_days), ("log_days", log_days)):
        if value < 1:
            raise ValueError(f"{name} is a number of days, at least 1: {value}")
    return {"backup_days": backup_days, "log_days": log_days}
