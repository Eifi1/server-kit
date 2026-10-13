"""Who gets what, for one recipient of one type (§3.3).

In this order:

1. **Nobody inactive, nobody who acted:** a deactivated account (a deletion request
   deactivates at once, user-admin §2.1), a demo, the event's own author or actor
   (keksdose's live #342): nothing. The writer passes the actor, so this holds for every
   writer.
2. **``app`` on:** an inbox row where the app keeps one, then a push to each device —
   unless the type is ``quiet`` and the account is inside its quiet hours. A held push is
   dropped; the inbox keeps the row.
3. **``email`` on, and the address verified:** the mail. Unverified: no mail, and the card
   says so (decision 8).
4. Both off: nothing, not even an inbox row.

**A service notice** (§2.8) skips the type's switch and keeps step 1: keksdose's deadline
on a guest's budget is a push and an inbox row whatever ``share_events`` says
(``push_notifications.py:752-786``). It goes on the app channel only — a service notice
carries no unsubscribe, so it is never a notification mail; an app that sends one by mail
sends it as its own account mail. The test push skips this plan altogether.

Lifted from keksdose ``domain/services/push_notifications.py:366-407``
(``_deliver_to_user``) and the per-type gate its callers apply before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from eifi1_server_kit.notifications.registry import NotificationType
from eifi1_server_kit.notifications.resource import TypeSwitches

__all__ = ["Delivery", "plan_delivery"]


@dataclass(frozen=True, slots=True)
class Delivery:
    """What to do for one recipient: write an inbox row, push to their devices, send the
    mail. :attr:`NONE` is nothing at all."""

    inbox: bool
    push: bool
    email: bool

    #: Nothing: no inbox row, no push, no mail.
    NONE: ClassVar[Delivery]


Delivery.NONE = Delivery(inbox=False, push=False, email=False)


def plan_delivery(
    notification_type: NotificationType,
    switches: TypeSwitches,
    *,
    active: bool,
    is_actor: bool,
    quiet_now: bool,
    email_verified: bool,
    keeps_inbox: bool,
    service: bool = False,
) -> Delivery:
    """The :class:`Delivery` for one recipient (§3.3).

    ``switches`` is the recipient's, resolved (:func:`~.resource.resolve_switches`).
    ``active`` is false for a deactivated account and a demo; ``is_actor`` for the
    event's own author. ``quiet_now`` is :func:`~.timing.in_quiet_hours` for the
    recipient, and drops only the push, only of a ``quiet`` type. ``keeps_inbox``: the
    app keeps an inbox (keksdose). ``service``: a service notice, on the app channel
    whatever the switch says, never by mail.
    """
    if not active or is_actor:
        return Delivery.NONE
    offers_app = notification_type.app is not None
    app = offers_app and (service or switches.app is True)
    email = not service and notification_type.email is not None and switches.email is True and email_verified
    push = app and not (notification_type.quiet and quiet_now)
    return Delivery(inbox=app and keeps_inbox, push=push, email=email)
