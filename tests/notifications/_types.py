"""The three apps' registries as the contract settles them (§5), for the tests."""

from __future__ import annotations

from eifi1_server_kit.notifications import NotificationType, NotificationTypes

#: keksdose's nine, with its defaults as ``app`` (``push_subscription.py:30-56``); no e-mail.
KEKSDOSE = NotificationTypes(
    NotificationType("recurring_posted", app=True),
    NotificationType("overspend", app=True),
    NotificationType("scan_done", app=False),
    NotificationType("share_events", app=False, quiet=False),
    NotificationType("low_balance", app=False),
    NotificationType("digest", app=False),
    NotificationType("announcements", app=False, quiet=False),
    NotificationType("support_chat", app=True, quiet=False),
    NotificationType("scheduled_due", app=True),
)

#: kastlan's two: the overdue invoices in the app and by mail, the dunnings in the app. No
#: push (§10.2 Settled): every ``push`` is ``None``.
KASTLAN = NotificationTypes(
    NotificationType("invoices_overdue", app=False, email=False),
    NotificationType("dunnings_pending", app=False),
    push_channel=False,
)

#: Kurvenschmiede's three, e-mail only.
KURVENSCHMIEDE = NotificationTypes(
    NotificationType("feedback_answered", app=None, email=True),
    NotificationType("share_granted", app=None, email=False),
    NotificationType("team_added", app=None, email=False),
    push_channel=False,
)
