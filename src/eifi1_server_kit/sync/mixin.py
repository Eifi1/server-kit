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
    from sqlalchemy.orm import Mapped, ORMExecuteState, Session, UOWTransaction, mapped_column, with_loader_criteria
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.sync.mixin needs SQLAlchemy: install the kit with its sync extra, eifi1-server-kit[sync]."
    ) from exc

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.sync.counter import KnowledgeCounter, TenantOf

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
    modified rows. Returns a function that removes the listener."""

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
            if obj in pending:
                if row.updated_at is None:
                    row.updated_at = moment
            elif not inspect(obj).attrs["updated_at"].history.has_changes():
                row.updated_at = moment

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
    upsert cannot revive it (D2); the stamping hook gives it a fresh knowledge."""
    moment = now if now is not None else utc_now()
    row = cast(_Synced, obj)
    row.deleted_at = moment
    row.updated_at = moment


def bulk_tombstone(model: type, *where: ColumnElement[bool], knowledge: int, now: datetime) -> Update:
    """The Core ``UPDATE`` that soft-deletes ``model``'s rows matching ``where``, stamping
    ``deleted_at``, ``knowledge`` and ``updated_at`` itself (a Core statement bypasses the
    ORM hook). Pass ONE ``(knowledge, now)`` pair across every statement of one logical
    delete, ``knowledge`` from the app's counter. Run it with ``session.execute`` (or
    ``await``); its ``rowcount`` is the rows removed."""
    return update(model).where(*where).values(deleted_at=now, knowledge=knowledge, updated_at=now)


def bulk_restamp(
    model: type, *where: ColumnElement[bool], values: Mapping[str, Any], knowledge: int, now: datetime
) -> Update:
    """Like :func:`bulk_tombstone` for a non-delete bulk ``UPDATE`` (an FK re-point): apply
    ``values`` and stamp the fresh ``knowledge`` and ``updated_at``, so the change reaches
    every other device."""
    return update(model).where(*where).values(**values, knowledge=knowledge, updated_at=now)
