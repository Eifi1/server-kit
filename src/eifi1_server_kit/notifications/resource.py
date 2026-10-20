"""The settings resource: ``GET/PATCH /notifications/settings`` (§3.2).

One resource per account replaces keksdose's two PUTs (``/push/preferences``,
``/push/settings``; ``adapters/api/push_router.py:133-172``)::

    {"types": {"recurring_posted": {"app": true, "push": true, "email": null}, …},
     "quiet_hours_start": "22:00", "quiet_hours_end": "07:00", "digest_cadence": "weekly"}

``types`` is in every app; the quiet hours where the app has push
(:class:`QuietHoursFields`, :class:`QuietHoursUpdate`); the cadence where it has a digest
(:class:`DigestFields`, :class:`DigestUpdate`); an app adds its own knobs beside them
(keksdose's ``low_balance_threshold``). Mixins first, so the JSON lists ``types`` first::

    class SettingsUpdate(DigestUpdate, QuietHoursUpdate, NotificationSettingsUpdate):
        registry = TYPES
        low_balance_threshold: Decimal | None = Field(default=None, ge=0)

**The PATCH** follows settings §6.1: :func:`~eifi1_server_kit.settings.apply_patch` writes
the plain fields and refuses a ``null`` on ``types`` and ``digest_cadence``; ``types``
itself is skipped there and goes through :func:`apply_switches_patch`, which reads its own
rules — a type's ``null`` resets all its switches, a switch's ``null`` that one::

    patch = apply_switches_patch(types, stored, body.types)  # 422 before anything is written
    apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    write(patch)
    return SettingsResponse(types=resolve_switches(types, {**stored, **patch}), …)  # rule 5

An app with no digest refuses a null on ``types`` alone: ``not_nullable=SETTINGS_SKIP``;
one whose body is ``types`` alone (no push, no digest) has no row to write and passes
``None``: ``apply_patch(None, body, not_nullable=SETTINGS_SKIP, skip=SETTINGS_SKIP)``.
The storage is the app's; keksdose's rows (one per touched type) fit, as an unset switch
is a missing row or a null column (:class:`StoredSwitches`).

**A switch per channel** (§10.2, 0.16): ``app`` (in the app), ``push`` and ``email``.
``push`` is ``null`` where the app or the type has no push (kastlan, Kurvenschmiede:
``NotificationTypes(..., push_channel=False)``), and a PATCH sending it there is a 422.
keksdose's stored ``enabled`` becomes both in-app switches
(:func:`migrate_enabled_to_channels`); while an old bundle can still send ``{app}`` alone,
its legacy ``/push/preferences`` shim passes ``couple_push_to_app=True`` (it goes with the
``/push/*`` aliases). ``PATCH /notifications/settings`` never couples: the 0.46 card's "In
the app" column sends ``{app}`` alone, and coupling there would flip push with it.

**The zone is the account's** (§10.3, 0.16; amended 2026-10-20, Marcel: automatic only):
the zone of the device last used (``users.device_timezone``, ``effective_zone``), never a
field of this resource. **A ``timezone`` in the PATCH is ignored in 0.16, refused from
0.17**: a stale 0.45 bundle (its zone picker, its quiet hours' first entry) still sends
one, here and to keksdose's ``/push/settings`` shim, and the PWA keeps old bundles for days.
:class:`NotificationSettingsUpdate` and :class:`QuietHoursUpdate` drop it before the
checks, so it is never in ``model_fields_set`` and ``apply_patch`` never writes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eifi1_server_kit.notifications.errors import NotificationChannelError, NotificationTypeError
from eifi1_server_kit.notifications.registry import NotificationTypes
from eifi1_server_kit.notifications.timing import QUIET_HOURS_PATTERN, DigestCadence

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
    "migrate_enabled_to_channels",
    "resolve_switches",
]

#: ``apply_patch``'s ``not_nullable`` for a body with :class:`DigestUpdate` (§3.2 rule 3):
#: ``types: null`` and ``digest_cadence: null`` are a 422 ``not_nullable``. Without a
#: digest, ``("types",)`` — :data:`SETTINGS_SKIP`.
SETTINGS_NOT_NULLABLE: tuple[str, ...] = ("types", "digest_cadence")
#: ``apply_patch``'s ``skip``: ``types`` is checked for a null there and written by the app
#: from :func:`apply_switches_patch`.
SETTINGS_SKIP: tuple[str, ...] = ("types",)


def _drop_stale_timezone(data: object) -> object:
    """The input without ``timezone``: a 0.45 bundle's zone, ignored in 0.16 and refused
    as an unknown field from 0.17 (§10.3, automatic only)."""
    if isinstance(data, dict) and "timezone" in data:
        return {key: value for key, value in data.items() if key != "timezone"}
    return data


class TypeSwitches(BaseModel):
    """One type in the ``GET``: each switch as it applies, ``null`` where the type doesn't
    offer the channel (Kurvenschmiede: ``{"app": null, "push": null, "email": true}``)."""

    #: The "in the app" switch (the inbox; kastlan's poller); ``None``: the app has no such channel.
    app: bool | None
    #: The e-mail switch; ``None``: the type sends no mail.
    email: bool | None
    #: The push switch (0.16), independent of ``app``; ``None``: no push for this type or app.
    push: bool | None = None


class TypeSwitchesPatch(BaseModel):
    """One type in the ``PATCH``: a switch left out keeps, ``null`` resets it to the type's
    default. Unknown fields are a 422."""

    model_config = ConfigDict(extra="forbid")

    app: bool | None = None
    email: bool | None = None
    #: 0.16; a 422 where the type or the app has no push.
    push: bool | None = None


@dataclass(frozen=True, slots=True)
class StoredSwitches:
    """What the app stores for one type: a switch the person set, or ``None`` — unset,
    read as the type's default by :func:`resolve_switches`. A type with no row is
    ``StoredSwitches()``."""

    app: bool | None = None
    email: bool | None = None
    #: 0.16. kastlan's ``StoredSwitches(app, email)`` keeps working: it has no push.
    push: bool | None = None


class NotificationSettingsResponse(BaseModel):
    """``GET /notifications/settings`` and the ``PATCH``'s answer (rule 5): every type the
    account can receive, in the registry's order (:func:`resolve_switches`). Add
    :class:`QuietHoursFields` and :class:`DigestFields` as the app has them."""

    types: dict[str, TypeSwitches]


class QuietHoursFields(BaseModel):
    """The quiet hours, where the app has push (§3.2, §3.4), in the account's zone."""

    #: Local ``HH:MM``; with ``quiet_hours_end``, the window — which may cross midnight.
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class DigestFields(BaseModel):
    """The digest's cadence, where the app has a digest type (§2.5)."""

    digest_cadence: DigestCadence = "weekly"


class NotificationSettingsUpdate(BaseModel):
    """``PATCH /notifications/settings`` (§3.2), checked against the app's registry.

    Set :attr:`registry` in the subclass, as ``ProfileUpdate.offered_locales`` is set
    (settings §7.2); a class without it is a :class:`TypeError` on its first validation.
    A type the registry doesn't know, or an ``app`` / ``push`` / ``email`` switch on a
    type that doesn't offer it (``push`` in an app with no push), is a validation error
    (422). A type the registry knows but this
    account doesn't receive is :func:`apply_switches_patch`'s
    :class:`~.errors.NotificationTypeError`. Unknown fields are a 422 — except
    ``timezone``, which a stale 0.45 bundle sends: ignored in 0.16, refused from 0.17.
    """

    model_config = ConfigDict(extra="forbid")

    _ignore_stale_timezone = model_validator(mode="before")(_drop_stale_timezone)

    #: The app's whole registry; the account's narrowing is :func:`apply_switches_patch`'s.
    registry: ClassVar[NotificationTypes | None] = None

    #: ``{type id: {"app"?, "push"?, "email"?} | null}``; a type left out keeps, a type's
    #: ``null`` resets all its switches. ``"types": null`` is refused through ``apply_patch``.
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
    """The quiet hours in the ``PATCH``: ``HH:MM`` bounds; ``null`` clears each. No zone:
    it is the account's (§10.3, automatic only). A ``timezone`` (a stale 0.45 bundle's, also
    through keksdose's ``/push/settings`` shim) is ignored in 0.16, refused from 0.17."""

    model_config = ConfigDict(extra="forbid")

    _ignore_stale_timezone = model_validator(mode="before")(_drop_stale_timezone)

    quiet_hours_start: str | None = Field(default=None, pattern=QUIET_HOURS_PATTERN)
    quiet_hours_end: str | None = Field(default=None, pattern=QUIET_HOURS_PATTERN)


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
        resolved[item.id] = TypeSwitches(
            app=_applied(item.app, row.app),
            email=_applied(item.email, row.email),
            push=_applied(item.push, row.push),
        )
    return resolved


def apply_switches_patch(
    types: NotificationTypes,
    stored: Mapping[str, StoredSwitches],
    patch: Mapping[str, TypeSwitchesPatch | None] | None,
    *,
    couple_push_to_app: bool = False,
) -> dict[str, StoredSwitches]:
    """The new stored value of each type the body names, in the registry's order (§3.2
    rules 1–2): a type's ``null`` unsets all its switches, a switch's ``null`` that one, a
    switch left out keeps its stored value. The app writes what this answers.

    ``couple_push_to_app`` (§10.2 rule 4, keksdose while old bundles send ``{app}``
    alone): an ``app`` sent without ``push`` writes ``push`` too, on a type that offers
    it. It goes with the ``/push/*`` aliases.

    ``types`` is the account's registry (:meth:`~.registry.NotificationTypes.receivable`);
    a type it doesn't hold is a :class:`~.errors.NotificationTypeError` (422,
    ``notification_type_not_received``), and a switch for a channel the type doesn't
    offer (``push`` in an app with no push) a :class:`~.errors.NotificationChannelError`
    (422, ``notification_channel_not_offered``), both raised before anything is answered. ``patch`` is
    the body's ``types``; ``None`` (left out) names nothing.
    """
    if not patch:
        return {}
    if refused := [type_id for type_id in patch if type_id not in types]:
        raise NotificationTypeError(refused)
    for channel in ("app", "push", "email"):
        if offless := [
            type_id
            for type_id, switches in patch.items()
            if switches is not None
            and channel in switches.model_fields_set
            and getattr(types[type_id], channel) is None
        ]:
            raise NotificationChannelError(offless, channel)
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
        push = current.push
        if "push" in sent:
            push = switches.push
        elif couple_push_to_app and "app" in sent and item.push is not None:
            push = switches.app
        changed[item.id] = StoredSwitches(
            app=switches.app if "app" in sent else current.app,
            email=switches.email if "email" in sent else current.email,
            push=push,
        )
    return changed


def migrate_enabled_to_channels(enabled: bool | None) -> StoredSwitches:
    """keksdose's one stored ``enabled`` per type as the switches of three channels
    (§10.2 rule 4): ``app = push = enabled``, e-mail unset. ``None`` — no row — stays
    unset, so both channels keep the type's default."""
    return StoredSwitches(app=enabled, push=enabled)
