"""Local-first sync, the server half (``docs/local-first-harmonization.md`` §1.5 D1, D2, D5 and
§1.6 in ``Eifi1/ui-kit``): keksdose's ``/sync`` protocol, shared with kastlan.

* :mod:`~eifi1_server_kit.sync.schemas` — the wire shapes, ``refused`` (one
  :class:`SyncRefusal` for the whole push, moved here from billing, which re-exports it) and
  the new per-change ``rejections``;
* :mod:`~eifi1_server_kit.sync.merge` — whole-row LWW on the device's edit time, deletes
  that always tombstone;
* :mod:`~eifi1_server_kit.sync.counter` — the knowledge counter as a port;
* :mod:`~eifi1_server_kit.sync.paging` — the ``(entity, knowledge, id)`` page cursor;
* :mod:`~eifi1_server_kit.sync.mixin` — the columns, the ``before_flush`` stamping, the
  tombstones and their filter, in SQLAlchemy: only with the ``sync`` extra
  (``eifi1-server-kit[sync]``), and imported lazily, so this package loads without it.

``eifi1_server_kit.sync.testing`` has :class:`InMemoryKnowledgeCounter`; it is not
re-exported. Stays in the apps: the routes, the entity registries, tenancy and RLS, E2EE,
billing's refusals, unique-key merges and revival rules, and kastlan's scopes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eifi1_server_kit.sync.counter import KnowledgeCounter, TenantOf
from eifi1_server_kit.sync.merge import as_utc, clamp_stamp, merge_delete, merge_lww
from eifi1_server_kit.sync.paging import (
    SYNC_PAGE_LIMIT,
    PageStart,
    begin_page,
    cursor_at_start,
    cut_page,
    is_after,
    page_of,
    page_order,
)
from eifi1_server_kit.sync.schemas import (
    SYNC_PLAN_LIMIT,
    SYNC_REJECTED,
    SyncChange,
    SyncPageCursor,
    SyncRefusal,
    SyncRejection,
    SyncRequest,
    SyncResponse,
)

if TYPE_CHECKING:
    from eifi1_server_kit.sync.mixin import (
        INCLUDE_TOMBSTONED,
        SyncColumns,
        bulk_restamp,
        bulk_tombstone,
        install_sync_stamping,
        install_tombstone_filter,
        tombstone,
    )

__all__ = [
    "INCLUDE_TOMBSTONED",
    "SYNC_PAGE_LIMIT",
    "SYNC_PLAN_LIMIT",
    "SYNC_REJECTED",
    "KnowledgeCounter",
    "PageStart",
    "SyncChange",
    "SyncColumns",
    "SyncPageCursor",
    "SyncRefusal",
    "SyncRejection",
    "SyncRequest",
    "SyncResponse",
    "TenantOf",
    "as_utc",
    "begin_page",
    "bulk_restamp",
    "bulk_tombstone",
    "clamp_stamp",
    "cursor_at_start",
    "cut_page",
    "install_sync_stamping",
    "install_tombstone_filter",
    "is_after",
    "merge_delete",
    "merge_lww",
    "page_of",
    "page_order",
    "tombstone",
]

#: The names :mod:`~eifi1_server_kit.sync.mixin` provides, loaded on first use.
_MIXIN = frozenset(
    {
        "INCLUDE_TOMBSTONED",
        "SyncColumns",
        "bulk_restamp",
        "bulk_tombstone",
        "install_sync_stamping",
        "install_tombstone_filter",
        "tombstone",
    }
)


def __getattr__(name: str) -> object:
    """The SQLAlchemy half, imported only when asked for (it needs the ``sync`` extra)."""
    if name in _MIXIN:
        from eifi1_server_kit.sync import mixin

        return getattr(mixin, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
