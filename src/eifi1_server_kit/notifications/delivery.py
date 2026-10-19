"""Who gets what, for one recipient of one type (§3.3).

In this order:

1. **Nobody inactive, nobody who acted:** a deactivated account (a deletion request
   deactivates at once, user-admin §2.1), a demo, the event's own author or actor
   (keksdose's live #342): nothing. The writer passes the actor, so this holds for every
   writer.
2. **``app`` on:** an inbox row where the app keeps one. **``push`` on** (0.16, §10.2:
   independent of ``app``): a push to each device that doesn't mute the type
   (:func:`device_receives`) — unless the type is ``quiet`` and the account is inside its
   quiet hours. A held push waits for the window's end where the app runs the queue
   (:meth:`~.queue.QueueService.hold_push`), else it is dropped; the inbox keeps the row.
   A push with no inbox row carries ``data.inbox: false`` (:class:`~.push.PushData`).
3. **``email`` on, and the address verified:** the mail. Unverified: no mail, and the card
   says so (decision 8).
4. All off: nothing, not even an inbox row.

**A service notice** (§2.8) skips the type's switch and keeps step 1: keksdose's deadline
on a guest's budget is a push and an inbox row whatever ``share_events`` says
(``push_notifications.py:752-786``). It goes on the app channel only — a service notice
carries no unsubscribe, so it is never a notification mail; an app that sends one by mail
sends it as its own account mail. The test push skips this plan altogether.

Lifted from keksdose ``domain/services/push_notifications.py:366-407``
(``_deliver_to_user``) and the per-type gate its callers apply before it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from eifi1_server_kit.notifications.errors import NotificationChannelError, NotificationTypeError
from eifi1_server_kit.notifications.registry import NotificationType, NotificationTypes
from eifi1_server_kit.notifications.resource import TypeSwitches

__all__ = ["Delivery", "apply_muted_types", "device_receives", "plan_delivery", "read_muted_types"]


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
    recipient, and holds only the push, only of a ``quiet`` type. ``keeps_inbox``: the
    app keeps an inbox (keksdose). ``service``: a service notice, in the app and by push
    where the type offers them, whatever the switches say, never by mail.

    0.16 (§10.2): the push follows ``switches.push``, not ``app``. Push on with in-app off
    sends the push with no inbox row. A device's own mutes are :func:`device_receives`'.
    """
    if not active or is_actor:
        return Delivery.NONE
    app = notification_type.app is not None and (service or switches.app is True)
    offers_push = notification_type.push is not None
    push = offers_push and (service or switches.push is True) and not (notification_type.quiet and quiet_now)
    email = not service and notification_type.email is not None and switches.email is True and email_verified
    return Delivery(inbox=app and keeps_inbox, push=push, email=email)


def device_receives(muted_types: Iterable[str] | None, type_id: str) -> bool:
    """Whether one device takes a push of ``type_id`` (§10.2 rule 3): a device can mute
    types, never add them, so this goes AFTER the account's ``push`` switch. A muted id the
    registry has since dropped matches nothing and is ignored."""
    return type_id not in (muted_types or ())


def read_muted_types(types: NotificationTypes, stored: Iterable[str] | None) -> list[str]:
    """A device's ``muted_types`` as ``GET /notifications/devices`` answers them: the
    stored ids the account still receives with push, in the registry's order — an id
    the registry dropped is ignored (and goes on the next write, :func:`apply_muted_types`).
    ``types`` is the account's registry."""
    kept = set(stored or ())
    return [item.id for item in types if item.id in kept and item.push is not None]


def apply_muted_types(types: NotificationTypes, muted: Iterable[str]) -> list[str]:
    """What ``PATCH /notifications/devices/{id} {"muted_types": [...]}`` stores (§10.2
    rule 3): the whole list, once each, in the registry's order. ``types`` is the
    account's registry; an id it doesn't hold is a :class:`~.errors.NotificationTypeError`
    and one without push a :class:`~.errors.NotificationChannelError` (both 422), raised
    before anything is answered. Another account's device is the app's 404."""
    wanted = list(dict.fromkeys(muted))
    if refused := [type_id for type_id in wanted if type_id not in types]:
        raise NotificationTypeError(refused)
    if pushless := [type_id for type_id in wanted if types[type_id].push is None]:
        raise NotificationChannelError(pushless, "push")
    chosen = set(wanted)
    return [item.id for item in types if item.id in chosen]
