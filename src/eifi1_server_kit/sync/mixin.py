"""The synced row's columns and its bookkeeping, in SQLAlchemy (``docs/local-first-harmonization.md``
§1.6 in ``Eifi1/ui-kit``; keksdose ``domain/models/base.py`` ``SyncMixin`` and
``infrastructure/sync.py``). Needs the ``sync`` extra (``eifi1-server-kit[sync]``).

* :class:`SyncColumns` — ``knowledge``, ``deleted_at`` and a CLIENT-OWNED ``updated_at``
  (no ``onupdate``, so a device's stamp survives as the LWW stamp). The key column stays
  the app's (keksdose ``id``, kastlan ``sync_uuid``).
* :func:`install_sync_stamping` — the ``before_flush`` hook every session runs, so no write
  path can forget the knowledge stamp.
* :func:`tombstone`, :func:`bulk_tombstone`, :func:`bulk_restamp` — deletes are updates, so
  they propagate as a delta.
* :func:`install_tombstone_filter` and :data:`INCLUDE_TOMBSTONED` — tombstones are hidden
  from every ORM read but the ``/sync`` pull's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final, Protocol, cast

try:
    from sqlalchemy import BigInteger, ColumnElement, DateTime, Update, event, inspect, update
    from sqlalchemy.orm import (
        Mapped,
        Mapper,
        ORMExecuteState,
        Session,
        UOWTransaction,
        mapped_column,
        with_loader_criteria,
    )
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.sync.mixin needs SQLAlchemy: install the kit with its sync extra, eifi1-server-kit[sync]."
    ) from exc

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.sync.counter import KnowledgeCounter, TenantOf
from eifi1_server_kit.sync.merge import as_utc

__all__ = [
    "INCLUDE_TOMBSTONED",
    "SyncColumns",
    "bulk_restamp",
    "bulk_tombstone",
    "install_sync_stamping",
    "install_tombstone_filter",
    "tombstone",
]

#: Execution option that opts a query OUT of the tombstone filter: the ``/sync`` pull must
#: see deleted rows to propagate them (``.execution_options(include_tombstoned=True)``).
INCLUDE_TOMBSTONED: Final = "include_tombstoned"


class _Synced(Protocol):
    knowledge: Any
    updated_at: Any
    deleted_at: Any


#: Per mapped class and column name: ``None`` when the class has no such column, else
#: whether the column is naive (``DateTime()`` without ``timezone=True``).
_COLUMNS: dict[tuple[type, str], bool | None] = {}


def _naive_column(cls: type, name: str) -> bool | None:
    """Whether ``cls`` maps column ``name`` naive (``True``), aware (``False``), or not at all
    (``None``, also for an unmapped class), by a mapper check, cached per class."""
    key = (cls, name)
    if key not in _COLUMNS:
        mapper: Mapper[Any] | None = inspect(cls, raiseerr=False)
        column = None if mapper is None else mapper.columns.get(name)
        _COLUMNS[key] = None if column is None else not getattr(column.type, "timezone", True)
    return _COLUMNS[key]


def _fit(value: datetime, naive: bool | None) -> datetime:
    """``value`` as the column takes it: naive UTC for a naive column, unchanged otherwise."""
    return as_utc(value).replace(tzinfo=None) if naive else value


class SyncColumns:
    """The delta-sync columns, as a declarative mixin. ``updated_at`` is client-owned: it
    has a default but no ``onupdate``, and :func:`install_sync_stamping` sets server-now
    only when the writer did not set it."""

    #: The tenant's monotonic stamp; the pull returns rows with ``knowledge >
    #: last_knowledge``.
    knowledge: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    #: The tombstone: a delete is an update, so it propagates.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: The LWW stamp: the device's edit time for a synced write, server-now otherwise.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


type _SessionTarget = type[Session] | Session | Any


def install_sync_stamping(
    session_cls: _SessionTarget,
    *,
    counter: KnowledgeCounter,
    tenant_of: TenantOf,
    is_synced: Callable[[object], bool],
    now: Clock = utc_now,
) -> Callable[[], None]:
    """Listen on ``session_cls`` (``Session`` itself, a subclass or a ``sessionmaker``) for
    ``before_flush``, and stamp every new or modified synced row (``is_synced``) with its
    tenant's next knowledge, ONE bump per tenant per flush (keksdose
    ``infrastructure/sync.py:141-186``). The counter is called under ``no_autoflush``.

    ``updated_at`` is client-authoritative: a new row gets ``now()`` only when it has none,
    a modified row only when ``updated_at`` was not set in this flush (the ``/sync`` apply
    path writes the device's stamp). Deletes are tombstone updates, so they arrive as
    modified rows. Returns a function that removes the listener.

    **Rows of one flush share one value**: a parent and its children written together
    carry the same knowledge, so ``knowledge`` alone does not order them. A pull that needs
    parents first orders within a value itself — :func:`~eifi1_server_kit.sync.page_order`'s
    ``(knowledge, id)`` per entity, entities in the app's order (kastlan, moving from its
    own per-row bump, sorts by ``(knowledge, entity order)``; 0.16.0).

    **A synced model without an ``updated_at`` column** (kastlan's handover children) gets
    its knowledge and no stamp, checked on the mapper; it then has no LWW stamp to compare
    (:func:`~eifi1_server_kit.sync.merge_lww` with ``None`` always lets the upsert win), so
    an app that wants D2 on that table adds the column.

    **Naive columns** (``timestamp without time zone``, kastlan) are written naive UTC: the
    hook's ``now()`` and an aware stamp the writer set (``clamp_stamp``'s answer) are fitted
    to the column, so asyncpg never sees an aware value for a naive parameter. Aware
    columns (:class:`SyncColumns`) are written as given."""

    def stamp(session: Session, flush_context: UOWTransaction, instances: object) -> None:
        # ``session.new`` builds a fresh set on every read: read it once (keksdose measured
        # the per-object read as a quarter of a test run).
        pending = session.new
        touched = [obj for obj in pending if is_synced(obj)]
        touched += [
            obj for obj in session.dirty if is_synced(obj) and session.is_modified(obj, include_collections=False)
        ]
        if not touched:
            return
        moment = now()
        per_tenant: dict[object, int | None] = {}
        for obj in touched:
            tenant_id = tenant_of(obj)
            if tenant_id is None:
                continue
            if tenant_id not in per_tenant:
                with session.no_autoflush:
                    per_tenant[tenant_id] = counter.bump(session, tenant_id)
            knowledge = per_tenant[tenant_id]
            if knowledge is None:
                continue
            row = cast(_Synced, obj)
            row.knowledge = knowledge
            naive = _naive_column(type(obj), "updated_at")
            if naive is None:
                continue
            if obj in pending:
                if row.updated_at is None:
                    row.updated_at = moment
            elif not inspect(obj).attrs["updated_at"].history.has_changes():
                row.updated_at = moment
            if naive and row.updated_at.tzinfo is not None:
                row.updated_at = _fit(row.updated_at, naive)

    event.listen(session_cls, "before_flush", stamp)
    return lambda: event.remove(session_cls, "before_flush", stamp)


def install_tombstone_filter(session_cls: _SessionTarget, *, synced: type) -> Callable[[], None]:
    """Hide tombstoned rows (``deleted_at IS NOT NULL``) of every ``synced`` class from every
    ORM ``SELECT`` on ``session_cls``, relationship loads included, unless the statement
    carries :data:`INCLUDE_TOMBSTONED`. Returns a function that removes the listener."""

    def hide(state: ORMExecuteState) -> None:
        if not state.is_select or state.is_column_load:
            return
        if state.execution_options.get(INCLUDE_TOMBSTONED, False):
            return
        state.statement = state.statement.options(
            with_loader_criteria(synced, lambda cls: cls.deleted_at.is_(None), include_aliases=True)
        )

    event.listen(session_cls, "do_orm_execute", hide)
    return lambda: event.remove(session_cls, "do_orm_execute", hide)


def tombstone(obj: object, *, now: datetime | None = None) -> None:
    """Soft-delete a synced row: ``deleted_at`` and ``updated_at`` move together, so an older
    upsert cannot revive it (D2); the stamping hook gives it a fresh knowledge. A model
    without ``updated_at`` gets only ``deleted_at`` (and no D2: add the column), and a naive
    column gets naive UTC."""
    moment = now if now is not None else utc_now()
    row = cast(_Synced, obj)
    cls = type(obj)
    row.deleted_at = _fit(moment, _naive_column(cls, "deleted_at"))
    naive = _naive_column(cls, "updated_at")
    if naive is not None or inspect(cls, raiseerr=False) is None:
        row.updated_at = _fit(moment, naive)


def bulk_tombstone(model: type, *where: ColumnElement[bool], knowledge: int, now: datetime) -> Update:
    """The Core ``UPDATE`` that soft-deletes ``model``'s rows matching ``where``, stamping
    ``deleted_at``, ``knowledge`` and ``updated_at`` itself (a Core statement bypasses the
    ORM hook). Pass ONE ``(knowledge, now)`` pair across every statement of one logical
    delete, ``knowledge`` from the app's counter. Run it with ``session.execute`` (or
    ``await``); its ``rowcount`` is the rows removed. A model without ``updated_at`` gets
    no stamp, and naive columns get naive UTC, as with :func:`tombstone`."""
    return (
        update(model)
        .where(*where)
        .values(deleted_at=_fit(now, _naive_column(model, "deleted_at")), **_stamp(model, knowledge, now))
    )


def bulk_restamp(
    model: type, *where: ColumnElement[bool], values: Mapping[str, Any], knowledge: int, now: datetime
) -> Update:
    """Like :func:`bulk_tombstone` for a non-delete bulk ``UPDATE`` (an FK re-point): apply
    ``values`` and stamp the fresh ``knowledge`` and ``updated_at``, so the change reaches
    every other device."""
    return update(model).where(*where).values(**values, **_stamp(model, knowledge, now))


def _stamp(model: type, knowledge: int, now: datetime) -> dict[str, Any]:
    """A bulk statement's ``knowledge`` and, where the model has the column, ``updated_at``."""
    naive = _naive_column(model, "updated_at")
    return {"knowledge": knowledge} if naive is None else {"knowledge": knowledge, "updated_at": _fit(now, naive)}
