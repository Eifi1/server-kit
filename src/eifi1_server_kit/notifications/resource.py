"""The settings resource: ``GET/PATCH /notifications/settings`` (§3.2).

One resource per account replaces keksdose's two PUTs (``/push/preferences``,
``/push/settings``; ``adapters/api/push_router.py:133-172``)::

    {"types": {"recurring_posted": {"app": true, "email": null}, …},
     "quiet_hours_start": "22:00", "quiet_hours_end": "07:00",
     "timezone": "Europe/Zurich", "digest_cadence": "weekly"}

``types`` is in every app; the quiet hours and the zone where the app has push
(:class:`QuietHoursFields`, :class:`QuietHoursUpdate`); the cadence where it has a digest
(:class:`DigestFields`, :class:`DigestUpdate`); an app adds its own knobs beside them
(keksdose's ``low_balance_threshold``). Mixins first, so the JSON lists ``types`` first::

    class SettingsUpdate(DigestUpdate, QuietHoursUpdate, NotificationSettingsUpdate):
        registry = TYPES
        low_balance_threshold: Decimal | None = Field(default=None, ge=0)

**The PATCH** follows settings §6.1: :func:`~eifi1_server_kit.settings.apply_patch` writes
the plain fields and refuses a ``null`` on ``types`` and ``digest_cadence``; ``types``
itself is skipped there and goes through :func:`apply_switches_patch`, which reads its own
rules — a type's ``null`` resets both its switches, a switch's ``null`` that one::

    patch = apply_switches_patch(types, stored, body.types)  # 422 before anything is written
    apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    write(patch)
    return SettingsResponse(types=resolve_switches(types, {**stored, **patch}), …)  # rule 5

An app with no digest refuses a null on ``types`` alone: ``not_nullable=SETTINGS_SKIP``.
The storage is the app's; keksdose's rows (one per touched type) fit, as an unset switch
is a missing row or a null column (:class:`StoredSwitches`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eifi1_server_kit.notifications.errors import NotificationTypeError
from eifi1_server_kit.notifications.registry import NotificationTypes
from eifi1_server_kit.notifications.timing import QUIET_HOURS_PATTERN, DigestCadence, is_known_zone

__all__ = [
    "SETTINGS_NOT_NULLABLE",
    "SETTINGS_SKIP",
    "DigestFields",
    "DigestUpdate",
    "NotificationSettingsResponse",
    "NotificationSettingsUpdate",
    "QuietHoursFields",
    "QuietHoursUpdate",
    "StoredSwitches",
    "TypeSwitches",
    "TypeSwitchesPatch",
    "apply_switches_patch",
    "resolve_switches",
]

#: ``apply_patch``'s ``not_nullable`` for a body with :class:`DigestUpdate` (§3.2 rule 3):
#: ``types: null`` and ``digest_cadence: null`` are a 422 ``not_nullable``. Without a
#: digest, ``("types",)`` — :data:`SETTINGS_SKIP`.
SETTINGS_NOT_NULLABLE: tuple[str, ...] = ("types", "digest_cadence")
#: ``apply_patch``'s ``skip``: ``types`` is checked for a null there and written by the app
#: from :func:`apply_switches_patch`.
SETTINGS_SKIP: tuple[str, ...] = ("types",)

#: The longest IANA name keksdose's ``notification_settings.timezone String(64)`` holds.
_ZONE_MAX_LENGTH = 64


class TypeSwitches(BaseModel):
    """One type in the ``GET``: each switch as it applies, ``null`` where the type doesn't
    offer the channel (Kurvenschmiede: ``{"app": null, "email": true}``)."""

    #: The "in the app" switch (inbox and push); ``None``: the app has no such channel.
    app: bool | None
    #: The e-mail switch; ``None``: the type sends no mail.
    email: bool | None


class TypeSwitchesPatch(BaseModel):
    """One type in the ``PATCH``: a switch left out keeps, ``null`` resets it to the type's
    default. Unknown fields are a 422."""

    model_config = ConfigDict(extra="forbid")

    app: bool | None = None
    email: bool | None = None


@dataclass(frozen=True, slots=True)
class StoredSwitches:
    """What the app stores for one type: a switch the person set, or ``None`` — unset,
    read as the type's default by :func:`resolve_switches`. A type with no row is
    ``StoredSwitches()``."""

    app: bool | None = None
    email: bool | None = None


class NotificationSettingsResponse(BaseModel):
    """``GET /notifications/settings`` and the ``PATCH``'s answer (rule 5): every type the
    account can receive, in the registry's order (:func:`resolve_switches`). Add
    :class:`QuietHoursFields` and :class:`DigestFields` as the app has them."""

    types: dict[str, TypeSwitches]


class QuietHoursFields(BaseModel):
    """The quiet hours and the zone, where the app has push (§3.2, §3.4)."""

    #: Local ``HH:MM``; with ``quiet_hours_end``, the window — which may cross midnight.
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    #: An IANA name; ``null`` is the app's zone, ``Europe/Zurich`` (decision 6).
    timezone: str | None = None


class DigestFields(BaseModel):
    """The digest's cadence, where the app has a digest type (§2.5)."""

    digest_cadence: DigestCadence = "weekly"


class NotificationSettingsUpdate(BaseModel):
    """``PATCH /notifications/settings`` (§3.2), checked against the app's registry.

    Set :attr:`registry` in the subclass, as ``ProfileUpdate.offered_locales`` is set
    (settings §7.2); a class without it is a :class:`TypeError` on its first validation.
    A type the registry doesn't know, or an ``app`` / ``email`` switch on a type that
    doesn't offer it, is a validation error (422). A type the registry knows but this
    account doesn't receive is :func:`apply_switches_patch`'s
    :class:`~.errors.NotificationTypeError`. Unknown fields are a 422.
    """

    model_config = ConfigDict(extra="forbid")

    #: The app's whole registry; the account's narrowing is :func:`apply_switches_patch`'s.
    registry: ClassVar[NotificationTypes | None] = None

    #: ``{type id: {"app"?, "email"?} | null}``; a type left out keeps, a type's ``null``
    #: resets both switches. ``"types": null`` is refused through ``apply_patch``.
    types: dict[str, TypeSwitchesPatch | None] | None = None

    @model_validator(mode="after")
    def _known_types(self) -> Self:
        registry = type(self).registry
        if registry is None:
            raise TypeError(
                f"{type(self).__name__}.registry is unset: set it to the app's NotificationTypes in the subclass"
            )
        for type_id, patch in (self.types or {}).items():
            if type_id not in registry:
                raise ValueError(f"unknown notification type: {type_id[:40]!r}")
            if patch is None:
                continue
            declared = registry[type_id]
            for channel in sorted(patch.model_fields_set):
                if getattr(declared, channel) is None:
                    raise ValueError(f"the notification type {type_id!r} has no {channel} switch")
        return self


class QuietHoursUpdate(BaseModel):
    """The quiet hours and the zone in the ``PATCH``: ``HH:MM`` bounds, an IANA zone
    ``zoneinfo`` knows; ``null`` clears each."""

    model_config = ConfigDict(extra="forbid")

    quiet_hours_start: str | None = Field(default=None, pattern=QUIET_HOURS_PATTERN)
    quiet_hours_end: str | None = Field(default=None, pattern=QUIET_HOURS_PATTERN)
    timezone: str | None = Field(default=None, max_length=_ZONE_MAX_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        if value is not None and not is_known_zone(value):
            raise ValueError("timezone must be an IANA zone name, such as Europe/Zurich")
        return value


class DigestUpdate(BaseModel):
    """The cadence in the ``PATCH``; ``null`` is refused (:data:`SETTINGS_NOT_NULLABLE`)."""

    model_config = ConfigDict(extra="forbid")

    digest_cadence: DigestCadence | None = None


def _applied(default: bool | None, stored: bool | None) -> bool | None:
    if default is None:
        return None
    return default if stored is None else stored


def resolve_switches(types: NotificationTypes, stored: Mapping[str, StoredSwitches]) -> dict[str, TypeSwitches]:
    """The ``GET``'s ``types``: every type of ``types`` — the account's registry — in its
    order, an unset switch as the type's default, ``None`` where the type doesn't offer
    the channel. A stored row for a type ``types`` doesn't hold is left out."""
    unset = StoredSwitches()
    resolved: dict[str, TypeSwitches] = {}
    for item in types:
        row = stored.get(item.id, unset)
        resolved[item.id] = TypeSwitches(app=_applied(item.app, row.app), email=_applied(item.email, row.email))
    return resolved


def apply_switches_patch(
    types: NotificationTypes,
    stored: Mapping[str, StoredSwitches],
    patch: Mapping[str, TypeSwitchesPatch | None] | None,
) -> dict[str, StoredSwitches]:
    """The new stored value of each type the body names, in the registry's order (§3.2
    rules 1–2): a type's ``null`` unsets both switches, a switch's ``null`` that one, a
    switch left out keeps its stored value. The app writes what this answers.

    ``types`` is the account's registry (:meth:`~.registry.NotificationTypes.receivable`);
    a type it doesn't hold is a :class:`~.errors.NotificationTypeError` (422,
    ``notification_type_not_received``), raised before anything is answered. ``patch`` is
    the body's ``types``; ``None`` (left out) names nothing.
    """
    if not patch:
        return {}
    if refused := [type_id for type_id in patch if type_id not in types]:
        raise NotificationTypeError(refused)
    unset = StoredSwitches()
    changed: dict[str, StoredSwitches] = {}
    for item in types:
        if item.id not in patch:
            continue
        switches = patch[item.id]
        if switches is None:
            changed[item.id] = unset
            continue
        current = stored.get(item.id, unset)
        sent = switches.model_fields_set
        changed[item.id] = StoredSwitches(
            app=switches.app if "app" in sent else current.app,
            email=switches.email if "email" in sent else current.email,
        )
    return changed
