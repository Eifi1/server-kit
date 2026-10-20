"""Named sync scopes (local-first §2.4): what a push may write, and what a pull withholds."""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eifi1_server_kit.sync import (
    SYNC_REJECTED,
    AfterApplyContext,
    EntityRule,
    RuleContext,
    ScopeRows,
    SyncChange,
    SyncScope,
    SyncScopeError,
    page_of,
    sort_changes,
    withhold,
)

OURS, THEIRS = 1, 2
TOKEN = uuid.UUID(int=7)


class Kind(enum.StrEnum):
    MOVE_IN = "move_in"
    MOVE_OUT = "move_out"


@dataclass
class Row:
    company: int
    parents: dict[str, str | None] = field(default_factory=dict)
    kind: Kind = Kind.MOVE_IN
    finalised: bool = False
    day: date = date(2026, 10, 19)
    at: datetime = datetime(2026, 10, 19, 8, tzinfo=UTC)
    amount: Decimal = Decimal("1.50")
    token: uuid.UUID = TOKEN
    note: str | None = None
    status: str = "open"


class InMemoryRows:
    """The port over a dict, scoped to one company as the app's session is."""

    references: Mapping[str, str] = {
        "unit_uuid": "units",
        "protocol_uuid": "protocols",
        "room_uuid": "rooms",
        "inspector_uuid": "contacts",
    }

    def __init__(self, rows: dict[tuple[str, str], Row], *, company: int = OURS) -> None:
        self.rows = rows
        self.company = company
        self.finds: list[tuple[str, str]] = []

    async def find(self, entity: str, row_id: str) -> object | None:
        self.finds.append((entity, row_id))
        row = self.rows.get((entity, row_id))
        return row if row is not None and row.company == self.company else None

    async def parent_id(self, row: object, wire_key: str) -> str | None:
        return cast(Row, row).parents.get(wire_key)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProtocolUpdate(_Wire):
    unit_uuid: str | None = None
    kind: Kind | None = None
    notes: str | None = None
    status: str | None = None
    day: date | None = None
    at: datetime | None = None
    amount: Decimal | None = None
    token: uuid.UUID | None = None
    note: str | None = None


class ProtocolCreate(ProtocolUpdate):
    unit_uuid: str
    kind: Kind


class RoomUpdate(_Wire):
    protocol_uuid: str | None = None
    name: str | None = Field(default=None, min_length=1)


class RoomCreate(RoomUpdate):
    protocol_uuid: str
    name: str = Field(min_length=1)


class DefectUpdate(_Wire):
    room_uuid: str | None = None
    text: str | None = None


class EstateUpdate(_Wire):
    name: str | None = None


async def not_finalised(context: RuleContext) -> str | None:
    if context.existing is not None and cast(Row, context.existing).finalised:
        return "the protocol is finalized; reopen it online first"
    return None


async def no_defects_on_a_finalised_room(context: RuleContext) -> str | None:
    if context.data.get("text") == "raise":
        raise SyncScopeError(context.change_id, "raised by the guard")
    return None


class Model:
    pass


HANDOVER = SyncScope(
    name="handover",
    roles=frozenset({"admin", "manager"}),
    readable=frozenset({"units", "protocols", "rooms", "defects", "estates"}),
    writable={
        "protocols": EntityRule(
            Model,
            ProtocolCreate,
            ProtocolUpdate,
            fixed_parents=("unit_uuid",),
            fixed_fields=("kind", "day", "at", "amount", "token", "note"),
            guard=not_finalised,
        ),
        "rooms": EntityRule(Model, RoomCreate, RoomUpdate, fixed_parents=("protocol_uuid",)),
        "defects": EntityRule(Model, DefectUpdate, DefectUpdate, deletable=True, guard=no_defects_on_a_finalised_room),
        "estates": EntityRule(Model, None, EstateUpdate),
    },
    withheld=frozenset({"estates.qr_iban"}),
)


def _store() -> dict[tuple[str, str], Row]:
    return {
        ("units", "u1"): Row(OURS),
        ("units", "u2"): Row(OURS),
        ("units", "x1"): Row(THEIRS),
        ("protocols", "p1"): Row(OURS, parents={"unit_uuid": "u1"}),
        ("protocols", "done"): Row(OURS, parents={"unit_uuid": "u1"}, finalised=True),
        ("rooms", "r1"): Row(OURS, parents={"protocol_uuid": "p1"}),
        ("defects", "d1"): Row(OURS),
        ("estates", "e1"): Row(OURS),
    }


def _change(entity: str, change_id: str, op: str = "upsert", **data: Any) -> dict[str, Any]:
    return {"entity": entity, "id": change_id, "op": op, "data": data or None}


async def _sort(*changes: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    result = await sort_changes(HANDOVER, changes, InMemoryRows(_store()))
    assert all(r.code == SYNC_REJECTED for r in result.rejections)
    return [str(c["id"]) for c in result.allowed], {r.change_id: r.detail for r in result.rejections}


async def test_an_entity_outside_the_scope_is_refused() -> None:
    assert await _sort(_change("meters", "m1", value=1)) == (
        [],
        {"m1": "meters cannot be written in the handover scope"},
    )


async def test_a_delete_needs_a_deletable_rule() -> None:
    allowed, refused = await _sort(_change("rooms", "r1", "delete"), _change("defects", "d1", "delete"))
    assert (allowed, refused) == (["d1"], {"r1": "rooms cannot be deleted"})


async def test_a_delete_of_a_row_the_server_never_had_is_a_no_op() -> None:
    assert await _sort(_change("defects", "gone", "delete")) == (["gone"], {})


async def test_a_rule_without_create_refuses_a_new_row() -> None:
    assert await _sort(_change("estates", "e9", name="New")) == ([], {"e9": "estates is not created through sync"})
    assert await _sort(_change("estates", "e1", name="Renamed")) == (["e1"], {})


async def test_the_create_and_update_schemas_validate() -> None:
    allowed, refused = await _sort(
        _change("protocols", "p9", unit_uuid="u1"),  # create: kind missing
        _change("protocols", "p1", colour="red"),  # update: an unknown field
        _change("rooms", "r1", name=""),  # update: a bad value
        _change("rooms", "r9"),  # create without data: the whole row is missing
    )
    assert allowed == []
    assert refused == {
        "p9": "kind: Field required",
        "p1": "colour: Extra inputs are not permitted",
        "r1": "name: String should have at least 1 character",
        "r9": "protocol_uuid: Field required",
    }


async def test_a_schema_error_without_a_location_names_the_entity() -> None:
    class Whole(BaseModel):
        @model_validator(mode="after")
        def _never(self) -> Whole:
            raise ValueError("never")

    scope = SyncScope(
        name="s", roles=frozenset(), readable=frozenset(), writable={"rooms": EntityRule(Model, Whole, Whole)}
    )
    result = await sort_changes(scope, [_change("rooms", "r9", a=1)], InMemoryRows(_store()))
    assert result.rejections[0].detail == "rooms: Value error, never"


async def test_a_reference_must_name_a_row_of_this_company_or_of_the_push() -> None:
    allowed, refused = await _sort(
        _change("protocols", "p9", unit_uuid="x1", kind="move_in"),  # another company's unit
        _change("protocols", "p8", unit_uuid="nowhere", kind="move_in"),
        _change("protocols", "p7", unit_uuid="u2", kind="move_out"),
        _change("rooms", "r7", protocol_uuid="p7", name="Kitchen"),  # new in the same push
    )
    assert allowed == ["p7", "r7"]
    assert refused == {
        "p9": "unit_uuid names no units row of this company",
        "p8": "unit_uuid names no units row of this company",
    }


async def test_a_fixed_parent_never_moves_but_may_be_sent_again() -> None:
    allowed, refused = await _sort(
        _change("protocols", "p1", unit_uuid="u1", notes="same unit"),
        _change("rooms", "r1", protocol_uuid="p1", name="Bath"),
    )
    assert (allowed, refused) == (["p1", "r1"], {})
    assert await _sort(_change("protocols", "p1", unit_uuid="u2")) == ([], {"p1": "unit_uuid cannot change"})


async def test_a_fixed_field_resent_unchanged_is_allowed_changed_is_refused() -> None:
    unchanged = _change(
        "protocols",
        "p1",
        kind="move_in",
        day="2026-10-19",
        at="2026-10-19T08:00:00Z",
        amount="1.5",
        token=str(uuid.UUID(int=7)),
        note=None,
    )
    assert await _sort(unchanged) == (["p1"], {})
    for name, value in (
        ("kind", "move_out"),
        ("day", "2026-10-20"),
        ("at", "2026-10-19T09:00:00Z"),
        ("amount", "2"),
        ("token", str(uuid.UUID(int=8))),
        ("note", "now set"),
    ):
        assert await _sort(_change("protocols", "p1", **{name: value})) == ([], {"p1": f"{name} cannot change"})


async def test_a_fixed_field_in_a_shape_it_cannot_be_is_a_change() -> None:
    """A loose schema lets any shape through; the fixed-field check still compares."""
    fixed = ("kind", "day", "at", "amount", "token")
    scope = SyncScope(
        name="s",
        roles=frozenset(),
        readable=frozenset(),
        writable={"protocols": EntityRule(Model, dict_model(), dict_model(), fixed_fields=fixed)},
    )
    sent: tuple[tuple[str, object], ...] = (
        ("day", "not a day"),
        ("at", "not a time"),
        ("amount", "lots"),
        ("token", "not a uuid"),
        ("amount", 2),
        ("kind", "move_in"),
    )
    for name, value in sent:
        change = {"entity": "protocols", "id": "p1", "op": "upsert", "data": {name: value}}
        result = await sort_changes(scope, [change], InMemoryRows(_store()))
        expected = [] if name == "kind" else [f"{name} cannot change"]
        assert [r.detail for r in result.rejections] == expected


async def test_a_new_rows_fixed_fields_are_not_checked() -> None:
    assert await _sort(_change("protocols", "p9", unit_uuid="u1", kind="move_out")) == (["p9"], {})


async def test_a_guard_reason_refuses_the_change() -> None:
    assert await _sort(_change("protocols", "done", notes="late")) == (
        [],
        {"done": "the protocol is finalized; reopen it online first"},
    )
    assert await _sort(_change("defects", "d1", text="raise")) == ([], {"d1": "raised by the guard"})


async def test_the_guard_sees_the_change_and_the_push() -> None:
    seen: list[RuleContext] = []

    async def spy(context: RuleContext) -> str | None:
        seen.append(context)
        return None

    scope = SyncScope(
        name="s",
        roles=frozenset(),
        readable=frozenset(),
        writable={"rooms": EntityRule(Model, RoomCreate, RoomUpdate, deletable=True, guard=spy)},
    )
    rows = InMemoryRows(_store())
    push = [_change("rooms", "r1", name="Hall"), _change("rooms", "r1", "delete")]
    await sort_changes(scope, push, rows)
    first, second = seen
    assert (first.scope, first.entity, first.change_id, first.op, first.data) == (
        scope,
        "rooms",
        "r1",
        "upsert",
        {"name": "Hall"},
    )
    assert first.existing is rows.rows[("rooms", "r1")] and first.rows is rows
    assert first.push == push and first.new_in_push == frozenset({"r1"})
    assert (second.op, second.data) == ("delete", {})


async def test_a_child_of_a_refused_new_row_is_refused_with_it_and_so_on_down() -> None:
    allowed, refused = await _sort(
        _change("rooms", "r8", protocol_uuid="p9", name="Hall"),  # the child comes first
        _change("protocols", "p9", unit_uuid="x1", kind="move_in"),
        _change("defects", "d8", room_uuid="r8"),  # the grandchild
        _change("defects", "d1", room_uuid="r8"),  # an existing row moved under it
        _change("rooms", "r7", protocol_uuid="p1", name="Bath"),
    )
    assert allowed == ["r7"]
    assert refused == {
        "p9": "unit_uuid names no units row of this company",
        "r8": "its parent was refused",
        "d8": "its parent was refused",
        "d1": "its parent was refused",
    }


async def test_a_refused_update_of_an_existing_row_keeps_its_children() -> None:
    allowed, refused = await _sort(
        _change("protocols", "done", notes="late"),
        _change("rooms", "r9", protocol_uuid="done", name="Hall"),
    )
    assert (allowed, list(refused)) == (["r9"], ["done"])


async def test_a_withheld_field_is_never_written() -> None:
    assert await _sort(_change("estates", "e1", qr_iban="CH00", name="Ok")) == (
        [],
        {"e1": "qr_iban cannot be written in the handover scope"},
    )


async def test_allowed_changes_keep_their_order_and_the_context_carries_the_rows_found() -> None:
    rows = InMemoryRows(_store())
    push = [
        _change("rooms", "r1", name="Hall"),
        _change("protocols", "p9", unit_uuid="u1", kind="move_in"),
        _change("estates", "e9", name="New"),
        _change("protocols", "p1", notes="n"),
        _change("defects", "d1", "delete"),
    ]
    result = await sort_changes(HANDOVER, push, rows)
    assert [c["id"] for c in result.allowed] == ["r1", "p9", "p1", "d1"]
    context = result.context
    assert (context.scope, context.rows, context.changes) == (HANDOVER, rows, result.allowed)
    assert context.found == {
        "r1": rows.rows[("rooms", "r1")],
        "p1": rows.rows[("protocols", "p1")],
        "d1": rows.rows[("defects", "d1")],
    }


async def test_the_context_keeps_the_sent_fields_as_they_stood_before_the_apply() -> None:
    rows = InMemoryRows(_store())
    push = [
        _change("protocols", "p1", status="done", note=None, notes="not an attribute"),
        _change("rooms", "r9", protocol_uuid="p1", name="New"),
        _change("defects", "d1", "delete"),
        _change("meters", "m1", value=1),
    ]
    result = await sort_changes(HANDOVER, push, rows)
    found = rows.rows[("protocols", "p1")]
    found.status = "done"  # the app's apply mutates the identity-mapped row
    assert result.context.found["p1"] is found
    assert result.context.before == {"p1": {"status": "open", "note": None}, "d1": {}}


async def test_a_uuid_id_and_reference_are_read_as_their_text() -> None:
    unit, protocol = uuid.uuid4(), uuid.uuid4()
    store = _store() | {("units", str(unit)): Row(OURS)}
    push = [
        SyncChange(
            entity="protocols", id=protocol, op="upsert", data={"unit_uuid": unit, "kind": "move_in"}
        ).model_dump(),
    ]
    scope = SyncScope(
        name="s",
        roles=frozenset(),
        readable=frozenset(),
        writable={"protocols": EntityRule(Model, dict_model(), dict_model())},
    )
    result = await sort_changes(scope, push, InMemoryRows(store))
    assert result.rejections == [] and result.allowed == push


def dict_model() -> type[BaseModel]:
    class Loose(BaseModel):
        model_config = ConfigDict(extra="allow")

    return Loose


async def test_an_older_client_gets_the_whole_push_refused() -> None:
    result = await sort_changes(
        HANDOVER, [_change("rooms", "r1", name="Hall"), _change("meters", "m1")], InMemoryRows(_store())
    )
    with pytest.raises(SyncScopeError) as caught:
        result.refuse_whole()
    assert (caught.value.change_id, caught.value.detail) == ("m1", "meters cannot be written in the handover scope")
    assert str(caught.value) == "Change m1: meters cannot be written in the handover scope"
    clean = await sort_changes(HANDOVER, [_change("rooms", "r1", name="Hall")], InMemoryRows(_store()))
    clean.refuse_whole()


async def test_after_apply_gets_the_context() -> None:
    calls: list[AfterApplyContext] = []

    async def finish(context: AfterApplyContext) -> None:
        calls.append(context)

    scope = SyncScope(name="s", roles=frozenset(), readable=frozenset(), after_apply=finish)
    result = await sort_changes(scope, [], InMemoryRows(_store()))
    assert scope.after_apply is not None
    await scope.after_apply(result.context)
    assert calls == [result.context] and result.allowed == [] and result.rejections == []


def test_the_in_memory_rows_satisfy_the_port() -> None:
    rows: ScopeRows = InMemoryRows({})
    assert rows.references["unit_uuid"] == "units"


# --- the pull ------------------------------------------------------------------------------


def test_withhold_strips_a_withheld_field_from_a_dict() -> None:
    pulled = {"entity": "estates", "id": "e1", "op": "upsert", "data": {"name": "Rose", "qr_iban": "CH44"}}
    assert withhold(HANDOVER, pulled) == {"entity": "estates", "id": "e1", "op": "upsert", "data": {"name": "Rose"}}
    assert pulled["data"] == {"name": "Rose", "qr_iban": "CH44"}  # never changed in place
    other = {"entity": "units", "id": "u1", "op": "upsert", "data": {"qr_iban": "kept: another entity"}}
    assert withhold(HANDOVER, other) == other
    tombstone = {"entity": "estates", "id": "e1", "op": "delete"}
    assert withhold(HANDOVER, tombstone) == tombstone


def test_withhold_strips_a_withheld_field_from_a_sync_change() -> None:
    pulled = SyncChange(entity="estates", id="e1", op="upsert", data={"name": "Rose", "qr_iban": "CH44"}, knowledge=3)
    stripped = withhold(HANDOVER, pulled)
    assert stripped.data == {"name": "Rose"} and stripped.knowledge == 3
    assert pulled.data == {"name": "Rose", "qr_iban": "CH44"}
    deleted = SyncChange(entity="estates", id="e1", op="delete")
    assert withhold(HANDOVER, deleted) is deleted


def test_withheld_names_an_entity_and_a_field() -> None:
    assert HANDOVER.withheld_fields("estates") == frozenset({"qr_iban"})
    assert HANDOVER.withheld_fields("units") == frozenset()
    for bad in ("qr_iban", ".qr_iban", "estates.", "estates.qr.iban"):
        with pytest.raises(ValueError, match=r"withheld names <entity>\.<field>"):
            SyncScope(name="s", roles=frozenset(), readable=frozenset(), withheld=frozenset({bad}))


def test_the_pull_order_is_the_apps_walk_order_of_the_readable_entities() -> None:
    walk = ("estates", "units", "contacts", "protocols", "rooms", "defects", "meters")
    assert HANDOVER.pull_order(walk) == ("estates", "units", "protocols", "rooms", "defects")
    with pytest.raises(ValueError, match=r"the handover scope reads \['defects', 'rooms'\]"):
        HANDOVER.pull_order(("estates", "units", "protocols"))


@dataclass(frozen=True)
class Pulled:
    entity: str
    id: str
    knowledge: int
    data: dict[str, Any]


def test_a_paged_pull_walks_the_scope_and_withholds() -> None:
    """The pull's pieces together: the scope's walk order into ``page_of``, each row
    withheld before it is sent."""
    rows = {
        "estates": [Pulled("estates", "e1", 2, {"name": "Rose", "qr_iban": "CH44"})],
        "units": [Pulled("units", "u1", 1, {"no": "1"}), Pulled("units", "u2", 3, {"no": "2"})],
        "meters": [Pulled("meters", "m1", 1, {})],
    }
    walk = HANDOVER.pull_order(("estates", "units", "protocols", "rooms", "defects", "meters", "x"))
    page, cursor = page_of(rows, entities=walk, cursor=None, last_knowledge=0, server_knowledge=3, limit=2)
    sent = [withhold(HANDOVER, {"entity": e, "id": r.id, "op": "upsert", "data": r.data}) for e, r in page]
    assert sent == [
        {"entity": "estates", "id": "e1", "op": "upsert", "data": {"name": "Rose"}},
        {"entity": "units", "id": "u1", "op": "upsert", "data": {"no": "1"}},
    ]
    assert cursor is not None and cursor.entity == "units"
    rest, done = page_of(rows, entities=walk, cursor=cursor, last_knowledge=0, server_knowledge=3, limit=2)
    assert [r.id for _, r in rest] == ["u2"] and done is None
