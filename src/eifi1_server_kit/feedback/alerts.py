"""The admins' alert for a new report, a rework and a new crash (feedback §11, 0.16).

``docs/feedback-harmonization.md`` §11 in ``Eifi1/ui-kit``, settled 2026-10-19. One
notification type, :data:`FEEDBACK_RECEIVED`, which every app declares in its registry, on
by default in every channel it offers and ``quiet``::

    NotificationType(FEEDBACK_RECEIVED, app=True, email=True, bundle=timedelta(minutes=15))  # keksdose
    NotificationType(FEEDBACK_RECEIVED, app=None, email=True)                                # kastlan, KS

:class:`~eifi1_server_kit.feedback.FeedbackService` calls the app's
:class:`FeedbackAlerts` port after the write, in the request's transaction — ``create``
after the report, ``report_crash`` only when it ADDS a row (never for a folded repeat),
``rework`` after the report moved and only when it lands OPEN (an admin's own rework lands
in REWORK and tells nobody). A reply, a status change, a comment, a vote and a loop's
write fire nothing; a loop's service is built with ``alerts=None`` (Kurvenschmiede's
``feedback_loop``), and keksdose's and kastlan's loops write past the service. A demo
cannot file or rework and its crash is not stored, so it fires nothing.

**The app delivers after the commit** — kastlan in a background task, Kurvenschmiede
through ``send_after_commit``, keksdose through its notification plan (inbox row and push
in the transaction, the mail after the commit, a crash's mail queued) — to
:func:`admin_alert_recipients` of its admins: keksdose every active ADMIN, kastlan the
ADMINs of the report's COMPANY (:attr:`FeedbackAlert.company_id`; the type's
receivability is "ADMIN in some company"), Kurvenschmiede every active, non-demo ADMIN.
Never a demo or deactivated account, never the actor, never the platform superuser for
being one. **Mail by kind** (:func:`mails_at_once`): a report and a rework are mailed at
once; a crash only inside the queue's 15-minute bundle where the app runs one.

**The words**: no title, no body, no reporter — "New feedback report #123 (Bug)", "Report
#123 was sent back for rework", "New crash report #123" — linking ``/feedback?row=123``;
kastlan names the company. **No recipient in logs**: the alert's path logs the type, the
report id and the recipient's user id, never an address.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from eifi1_server_kit.feedback.enums import FeedbackCategory

__all__ = [
    "FEEDBACK_RECEIVED",
    "FeedbackAlert",
    "FeedbackAlertKind",
    "FeedbackAlerts",
    "admin_alert_recipients",
    "mails_at_once",
]

#: The admins' notification type (§11.2 Settled 1), declared by every app.
FEEDBACK_RECEIVED = "feedback_received"

#: What happened: a new report, a report sent back that landed OPEN, a new crash row.
FeedbackAlertKind = Literal["report", "rework", "crash"]


@dataclass(frozen=True, slots=True)
class FeedbackAlert:
    """One alert, as :meth:`FeedbackAlerts.filed` receives it: ids and the category, no
    sealed content and no address."""

    kind: FeedbackAlertKind
    feedback_id: int
    #: The report's category, for the words ("New feedback report #123 (Bug)").
    category: FeedbackCategory | str | None
    #: Who filed or reworked it: never told (:func:`admin_alert_recipients`).
    actor_id: int
    #: The report's company where rows carry one (kastlan's ``company_id``); ``None`` elsewhere.
    company_id: int | None = None


class FeedbackAlerts(Protocol):
    """The app's delivery of :data:`FEEDBACK_RECEIVED` (§11.2 rule 8)."""

    async def filed(self, alert: FeedbackAlert, row: Any) -> None:
        """Called after the write, in the request's transaction, with the report's row.
        Resolve the recipients and hand the delivery to the commit: an inbox row and a
        push in the transaction, the mail after it (or queued). Never log an address."""


def admin_alert_recipients(admin_ids: Iterable[int], *, actor_id: int | None) -> list[int]:
    """The admins to tell, in the given order, once each, the actor left out: an admin
    who files or reworks is not told of their own report; the others are. ``admin_ids``
    are the app's active, non-demo admins (of the report's company in kastlan)."""
    return [admin_id for admin_id in dict.fromkeys(admin_ids) if admin_id != actor_id]


def mails_at_once(alert: FeedbackAlert) -> bool:
    """Whether the alert's mail goes right after the commit (§11.2 Settled 3): a report
    and a rework do; a crash does not — it is in the app (an inbox row and a push, or the
    attention badge) and mailed only inside the queue's bundle where the app runs one."""
    return alert.kind != "crash"
