"""A fake :class:`~eifi1_server_kit.sync.KnowledgeCounter` for an app's tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["InMemoryKnowledgeCounter"]


class InMemoryKnowledgeCounter:
    """A counter in memory: one per tenant (keksdose), or with ``per_tenant=False`` one for
    everything, the tenant ignored (kastlan's database-wide row).

    ``calls`` records each bump as ``(tenant_id, autoflush)``, so a test sees that the hook
    bumps once per tenant per flush, and under ``no_autoflush`` (``autoflush`` False)."""

    def __init__(self, *, per_tenant: bool = True, start: int = 0) -> None:
        self.per_tenant = per_tenant
        self.start = start
        self.values: dict[object, int] = {}
        self.calls: list[tuple[object, bool]] = []

    def _key(self, tenant_id: object) -> object:
        return tenant_id if self.per_tenant else None

    def bump(self, session: Session, tenant_id: object) -> int:
        self.calls.append((tenant_id, bool(session.autoflush)))
        key = self._key(tenant_id)
        self.values[key] = self.values.get(key, self.start) + 1
        return self.values[key]

    def current(self, tenant_id: object = None) -> int:
        """The tenant's value now (``start`` before the first bump)."""
        return self.values.get(self._key(tenant_id), self.start)
