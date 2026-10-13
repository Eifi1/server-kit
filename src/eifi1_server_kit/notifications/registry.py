"""The notification types: each app declares its own; the kit keeps no list (§2.1, §3.1).

A type is an id, a default per channel it offers, and whether quiet hours hold its push.
The app builds its registry once, at import, and the server's registry is the ONE list: the
settings ``GET`` answers every type the account can receive, in the registry's order, and
the card draws what it answers — the client keeps no defaults (keksdose's
``PREF_DEFAULTS`` goes).

**The default rule is keksdose's** (``domain/models/push_subscription.py:25-56``,
``NOTIFICATION_TYPE_DEFAULTS``): off, unless the type can only fire in answer to something
the person did — a reply to their message, a row they dated forward, an answer to their
report. "A new trigger never surprises an existing user." keksdose's two launch types stay
on as shipped.

**Two channels** (§2.2, §2.3): ``app`` — the inbox and push, one channel to the person, and
one switch — and ``email``. A type offers the ones the app has: ``app=None`` in an app with
no "in the app" channel (Kurvenschmiede), ``email=None`` for a type that sends no mail.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

__all__ = ["TYPE_ID_PATTERN", "NotificationType", "NotificationTypes"]

#: A type id: lower case, digits and ``_``, 2–40 characters, starting with a letter — it
#: fits keksdose's ``notification_preferences.notification_type String(40)``.
TYPE_ID_PATTERN = r"^[a-z][a-z0-9_]{1,39}$"

_TYPE_ID = re.compile(TYPE_ID_PATTERN)


def _is_type_id(value: object) -> bool:
    """Whether ``value`` is a :data:`TYPE_ID_PATTERN` id (no trailing newline, unlike ``re.match``)."""
    return isinstance(value, str) and _TYPE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class NotificationType:
    """One notification type (§3.1)::

        NotificationType("support_chat", app=True, quiet=False)  # keksdose
        NotificationType("feedback_answered", app=None, email=True)  # Kurvenschmiede

    A :class:`ValueError` for an id outside :data:`TYPE_ID_PATTERN`, or a type with
    neither channel.
    """

    #: The id the switches, the inbox rows, the payload's ``data.type`` and the
    #: unsubscribe token carry (``support_chat``).
    id: str
    #: The "in the app" default (inbox and push); ``None``: the app has no such channel.
    app: bool | None
    #: The e-mail default; ``None``: the type sends no mail.
    email: bool | None = None
    #: Quiet hours hold its push (§2.4). ``False`` for what a person sends — a share, a
    #: support reply, an announcement — and ``True`` for what the machine sends.
    quiet: bool = True

    def __post_init__(self) -> None:
        if not _is_type_id(self.id):
            raise ValueError(f"a notification type id matches {TYPE_ID_PATTERN}: {self.id!r}")
        if self.app is None and self.email is None:
            raise ValueError(f"the notification type {self.id!r} offers no channel: give app or email a default")


class NotificationTypes:
    """The app's registry: its types, in its order (§3.1)::

        TYPES = NotificationTypes(
            NotificationType("invoices_overdue", app=False, email=False),
            NotificationType("dunnings_pending", app=False),
        )

    A :class:`ValueError` for an id declared twice. ``iter`` and ``len`` go over the
    types in order, ``"digest" in TYPES`` asks for an id, ``TYPES["digest"]`` answers its
    type (a :class:`KeyError` for an unknown one), :attr:`ids` the ids in order, and
    :meth:`receivable` narrows it to what one account can receive.
    """

    __slots__ = ("_types",)

    def __init__(self, *types: NotificationType) -> None:
        by_id: dict[str, NotificationType] = {}
        for item in types:
            if item.id in by_id:
                raise ValueError(f"the notification type {item.id!r} is declared twice")
            by_id[item.id] = item
        self._types = by_id

    def __iter__(self) -> Iterator[NotificationType]:
        return iter(self._types.values())

    def __len__(self) -> int:
        return len(self._types)

    def __contains__(self, type_id: object) -> bool:
        return isinstance(type_id, str) and type_id in self._types

    def __getitem__(self, type_id: str) -> NotificationType:
        return self._types[type_id]

    def __repr__(self) -> str:
        return f"NotificationTypes({', '.join(repr(item) for item in self)})"

    @property
    def ids(self) -> tuple[str, ...]:
        """Every id, in the registry's order."""
        return tuple(self._types)

    def receivable(self, keep: Callable[[NotificationType], bool]) -> NotificationTypes:
        """The types one account can receive, in the same order: the account's narrowing.

        kastlan's two types go to an account that is staff in any of its companies (roles
        are per company); a tenant receives neither, so its ``GET`` lists none and a
        ``PATCH`` naming one is a 422::

            types = TYPES.receivable(lambda _type: is_staff_anywhere)
        """
        return NotificationTypes(*(item for item in self if keep(item)))
