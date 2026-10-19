"""What a device may read and write through ``POST /sync``: one named scope per job
(``docs/local-first-harmonization.md`` §2.4 in ``Eifi1/ui-kit``; lifted from kastlan's
``domain/services/sync_scopes.py``, its handover rules left to the app as guards).

A request names a scope; the scope says which entities it pulls (``readable``), which it
may write and how (``writable``, one :class:`EntityRule` each), which fields never reach a
device (``withheld``, D14), and for which roles. Anything else is refused one change at a
time: :func:`sort_changes` answers the changes the scope allows, in push order, and a
:class:`~eifi1_server_kit.sync.SyncRejection` with its reason for each one it refuses, so
the rest of the push applies (D1). An older client that sends no ``refusals`` gets the
whole request refused instead (:meth:`SortedChanges.refuse_whole`, a 422 in the app).

Layer 1 and a port: the kit never touches the database. The app's :class:`ScopeRows`
finds a row by its sync id in the request's company (tombstones included), names a row's
parent, and lists the relation wire keys a change may carry. A guard or ``after_apply``
needs the app's session: keep it on the app's ``ScopeRows`` and read it from the context's
``rows``.

The pull uses the same scope: :meth:`SyncScope.pull_order` gives the readable entities in
the app's walk order for :func:`~eifi1_server_kit.sync.page_of` /
:func:`~eifi1_server_kit.sync.begin_page`, and :func:`withhold` strips the withheld fields
from each pulled row before it is sent.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from eifi1_server_kit.sync.schemas import SYNC_REJECTED, SyncChange, SyncRejection

__all__ = [
    "AfterApplyContext",
    "EntityRule",
    "RuleContext",
    "ScopeRows",
    "SortedChanges",
    "SyncScope",
    "SyncScopeError",
    "sort_changes",
    "withhold",
]

#: A pushed change as the app holds it: ``SyncChange.model_dump()`` (``entity``, ``id``,
#: ``op``, ``data``, ``updated_at``).
Change = Mapping[str, Any]

#: :attr:`SyncRejection.detail` for a change that points at a refused new row of its push.
PARENT_REFUSED = "its parent was refused"


class SyncScopeError(ValueError):
    """One change a scope does not allow, with its reason (kastlan's ``SyncRefusedError``).

    :func:`sort_changes` turns each into a rejection; :meth:`SortedChanges.refuse_whole`
    raises the first for an older client, which the app answers as a 422 with nothing
    written. A guard may raise it too instead of returning its reason."""

    def __init__(self, change_id: str, detail: str) -> None:
        super().__init__(f"Change {change_id}: {detail}")
        self.change_id = change_id
        self.detail = detail


class ScopeRows(Protocol):
    """The port: how the app finds rows, scoped to the request's company."""

    #: Every relation wire key a scope's writes carry, and the entity it points at
    #: (kastlan: ``{"unit_uuid": "units", "room_uuid": "handover_rooms", …}``). A key that
    #: names no row of this company (and no new row of the same push) refuses the change.
    @property
    def references(self) -> Mapping[str, str]: ...

    async def find(self, entity: str, row_id: str) -> object | None:
        """The company's row of ``entity`` with this sync id, tombstones included; ``None``
        when there is none (or it is another company's)."""
        ...

    async def parent_id(self, row: object, wire_key: str) -> str | None:
        """The sync id of the row ``wire_key`` of an existing ``row`` points at today."""
        ...


@dataclass(frozen=True, slots=True)
class RuleContext:
    """What an :class:`EntityRule`'s guard sees of one change, after the kit's checks."""

    scope: SyncScope
    entity: str
    change_id: str
    op: str
    #: The change's ``data`` (``{}`` for a delete).
    data: Mapping[str, Any]
    #: The server's row as it stands (``None``: a new row).
    existing: object | None
    rows: ScopeRows
    #: The whole push, in order (kastlan reads a new protocol's unit from it).
    push: Sequence[Change]
    #: The ids of the push's upserts: rows that may be new in the same request.
    new_in_push: frozenset[str]


@dataclass(frozen=True, slots=True)
class AfterApplyContext:
    """What :attr:`SyncScope.after_apply` sees once the allowed changes are applied."""

    scope: SyncScope
    rows: ScopeRows
    #: The changes the scope allowed, in push order.
    changes: Sequence[Change]
    #: The server's rows as they stood BEFORE the push, by change id (new rows absent).
    found: Mapping[str, object]


#: An app rule on one change: a reason refuses it, ``None`` lets it through.
Guard = Callable[[RuleContext], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class EntityRule:
    """How a scope lets one entity be written."""

    #: The app's model (the kit never reads it; the app's ``ScopeRows`` may).
    model: type
    #: Validates a new row's data; ``None``: rows are not created through sync.
    create: type[BaseModel] | None
    #: Validates a change to an existing row.
    update: type[BaseModel]
    deletable: bool = False
    #: Relation wire keys whose target may not change once the row exists.
    fixed_parents: tuple[str, ...] = ()
    #: Fields that may not change once the row exists. Both kinds may be sent again
    #: unchanged: a device that lost the server's answer re-sends its whole create.
    fixed_fields: tuple[str, ...] = ()
    #: The app's own rule, run last (kastlan: a finalised protocol, a completed meeting, a
    #: reading in the protocol's unit).
    guard: Guard | None = None


@dataclass(frozen=True, slots=True)
class SyncScope:
    """One named job a device syncs: what it reads, what it writes, for which roles."""

    name: str
    #: The app's role values that may sync it; the app's router checks them (403).
    roles: frozenset[str]
    #: The entities a device pulls (the app's entity names).
    readable: frozenset[str]
    #: The entities it may write, and how.
    writable: Mapping[str, EntityRule] = field(default_factory=dict)
    #: ``"<entity>.<field>"`` never pulled and never written (D14; kastlan:
    #: ``estates.qr_iban``).
    withheld: frozenset[str] = frozenset()
    #: Runs after the allowed changes are applied, in the same transaction (kastlan:
    #: what finalising a protocol runs online).
    after_apply: Callable[[AfterApplyContext], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        for name in self.withheld:
            entity, dot, field_name = name.partition(".")
            if not (entity and dot and field_name) or "." in field_name:
                raise ValueError(f"withheld names <entity>.<field>, not {name!r}")

    def withheld_fields(self, entity: str) -> frozenset[str]:
        """The fields of ``entity`` this scope withholds."""
        prefix = f"{entity}."
        return frozenset(name.removeprefix(prefix) for name in self.withheld if name.startswith(prefix))

    def pull_order(self, entities: Iterable[str]) -> tuple[str, ...]:
        """The readable entities in the app's walk order (parents first), for the paged
        pull's ``entities``. Every readable entity must be in ``entities``: one left out
        would never reach a device."""
        order = tuple(entity for entity in entities if entity in self.readable)
        missing = self.readable.difference(order)
        if missing:
            raise ValueError(f"the {self.name} scope reads {sorted(missing)}, which the walk order leaves out")
        return order


@dataclass(frozen=True, slots=True)
class SortedChanges:
    """:func:`sort_changes`' answer: apply ``allowed``, send ``rejections``, then hand
    ``context`` to :attr:`SyncScope.after_apply`."""

    allowed: list[Change]
    rejections: list[SyncRejection]
    context: AfterApplyContext

    def refuse_whole(self) -> None:
        """For a client without ``refusals``: raise the first refusal as
        :class:`SyncScopeError`, so nothing of the push is written."""
        if self.rejections:
            first = self.rejections[0]
            raise SyncScopeError(first.change_id, first.detail)


def _same(sent: object, current: object) -> bool:
    """Whether a wire value (JSON-safe) names the stored value: an enum by its value, a
    UUID, date, time stamp or decimal by its text."""
    if isinstance(current, enum.Enum):
        current = current.value
    if sent == current:
        return True
    if not isinstance(sent, str):
        return False
    try:
        if isinstance(current, uuid.UUID):
            return uuid.UUID(sent) == current
        if isinstance(current, datetime):
            return datetime.fromisoformat(sent) == current
        if isinstance(current, date):
            return date.fromisoformat(sent) == current
        if isinstance(current, Decimal):
            return Decimal(sent) == current
    except ValueError, InvalidOperation:
        return False
    return False


def _first_error(entity: str, exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or entity
    return f"{where}: {first['msg']}"


async def _check(
    scope: SyncScope,
    change: Change,
    rows: ScopeRows,
    push: Sequence[Change],
    new_in_push: frozenset[str],
    found: dict[str, object],
) -> None:
    """Refuse (:class:`SyncScopeError`) a change the scope does not allow."""
    entity, change_id, op = str(change["entity"]), str(change["id"]), change["op"]
    rule = scope.writable.get(entity)
    if rule is None:
        raise SyncScopeError(change_id, f"{entity} cannot be written in the {scope.name} scope")
    data: Mapping[str, Any] = change.get("data") or {}
    existing = await rows.find(entity, change_id)
    if existing is not None:
        found[change_id] = existing

    if op == "delete":
        if not rule.deletable:
            raise SyncScopeError(change_id, f"{entity} cannot be deleted")
        if existing is None:
            return  # nothing to delete: the push's delete is a no-op
    else:
        written = sorted(scope.withheld_fields(entity).intersection(data))
        if written:
            raise SyncScopeError(change_id, f"{written[0]} cannot be written in the {scope.name} scope")
        schema = rule.update if existing is not None else rule.create
        if schema is None:
            raise SyncScopeError(change_id, f"{entity} is not created through sync")
        try:
            schema.model_validate(data)
        except ValidationError as exc:
            raise SyncScopeError(change_id, _first_error(entity, exc)) from exc

    for key, target in rows.references.items():
        ref = data.get(key)
        if ref is None or str(ref) in new_in_push:
            continue
        if await rows.find(target, str(ref)) is None:
            raise SyncScopeError(change_id, f"{key} names no {target} row of this company")

    if existing is not None:
        for key in rule.fixed_parents:
            if key in data and not _same(data[key], await rows.parent_id(existing, key)):
                raise SyncScopeError(change_id, f"{key} cannot change")
        for name in rule.fixed_fields:
            if name in data and not _same(data[name], getattr(existing, name)):
                raise SyncScopeError(change_id, f"{name} cannot change")

    if rule.guard is not None:
        context = RuleContext(
            scope=scope,
            entity=entity,
            change_id=change_id,
            op=op,
            data=data,
            existing=existing,
            rows=rows,
            push=push,
            new_in_push=new_in_push,
        )
        reason = await rule.guard(context)
        if reason is not None:
            raise SyncScopeError(change_id, reason)


async def sort_changes(scope: SyncScope, changes: Sequence[Change], rows: ScopeRows) -> SortedChanges:
    """The changes ``scope`` allows, in push order, and a rejection for each it refuses.

    Each change is checked against the server's rows as they stand, in this order: the
    entity is writable; a delete is allowed (a delete of a row the server never had is
    let through, a no-op); no withheld field is written; the data passes the rule's
    ``create`` (a new row) or ``update`` schema; every reference names a row of this
    company or a new row of the same push; an existing row's fixed parents and fields are
    unchanged (re-sending them is fine); then the rule's ``guard``. A change that points
    at a NEW row of the push which was refused is refused with it ("its parent was
    refused"), and so on down."""
    new_in_push = frozenset(str(change["id"]) for change in changes if change["op"] == "upsert")
    found: dict[str, object] = {}
    rejections: dict[str, SyncRejection] = {}
    for change in changes:
        try:
            await _check(scope, change, rows, changes, new_in_push, found)
        except SyncScopeError as exc:
            rejections.setdefault(
                exc.change_id, SyncRejection(change_id=exc.change_id, code=SYNC_REJECTED, detail=exc.detail)
            )

    keys = tuple(rows.references)
    refused_new = {change_id for change_id in rejections if change_id in new_in_push and change_id not in found}
    while refused_new:
        cascade: set[str] = set()
        for change in changes:
            change_id = str(change["id"])
            data = change.get("data") or {}
            refs = (data.get(key) for key in keys)
            if change_id not in rejections and any(ref is not None and str(ref) in refused_new for ref in refs):
                rejections[change_id] = SyncRejection(change_id=change_id, code=SYNC_REJECTED, detail=PARENT_REFUSED)
                if change_id in new_in_push and change_id not in found:
                    cascade.add(change_id)
        refused_new = cascade

    allowed = [change for change in changes if str(change["id"]) not in rejections]
    return SortedChanges(
        allowed=allowed,
        rejections=list(rejections.values()),
        context=AfterApplyContext(
            scope=scope,
            rows=rows,
            changes=allowed,
            found={str(change["id"]): found[str(change["id"])] for change in allowed if str(change["id"]) in found},
        ),
    )


def withhold[C: (SyncChange, dict[str, Any])](scope: SyncScope, change: C) -> C:
    """A pulled row without the fields ``scope`` withholds from its ``data`` (D14): a
    :class:`~eifi1_server_kit.sync.SyncChange` or the app's dict, answered in kind and
    never changed in place (a ``SyncChange`` without ``data`` comes back as it is)."""
    if isinstance(change, SyncChange):
        if change.data is None:
            return change
        return change.model_copy(update={"data": _without(scope, change.entity, change.data)})
    stripped = dict(change)
    data = stripped.get("data")
    if data is not None:
        stripped["data"] = _without(scope, str(stripped["entity"]), data)
    return stripped


def _without(scope: SyncScope, entity: str, data: Mapping[str, Any]) -> dict[str, Any]:
    hidden = scope.withheld_fields(entity)
    return {key: value for key, value in data.items() if key not in hidden}
