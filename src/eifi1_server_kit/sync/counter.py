"""The knowledge counter, as a port (``docs/local-first-harmonization.md`` §1.6 in
``Eifi1/ui-kit``).

Every write to a synced row advances a monotonic ``knowledge`` counter and copies the new
value onto the row, so a device pulls "everything with ``knowledge > last_knowledge``". Where
the counter lives is the app's: keksdose counts per household (``households.knowledge``),
kastlan keeps one database-wide row and ignores the tenant.

**The lock is load-bearing.** :meth:`KnowledgeCounter.bump` is a read-modify-write on one
row: without ``SELECT … FOR UPDATE`` (or an atomic ``UPDATE … RETURNING``) two overlapping
flushes both read N and write N+1, and a device that records N+1 never sees the other
device's rows. The stamping hook calls it inside ``before_flush`` under ``no_autoflush``
(keksdose ``infrastructure/sync.py:45-63``). SQLAlchemy is needed by the implementations,
not by this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["KnowledgeCounter", "TenantOf"]


class KnowledgeCounter(Protocol):
    """Hands out the next knowledge value for a tenant."""

    def bump(self, session: Session, tenant_id: object) -> int | None:
        """Advance ``tenant_id``'s counter and return the new value, under a row lock.

        Synchronous: it runs inside ``before_flush`` (an ``AsyncSession`` flushes through
        its sync ``Session``), already under ``session.no_autoflush``. Mutate the counter's
        ORM row in memory (keksdose) or run one atomic statement (kastlan's ``INSERT … ON
        CONFLICT DO UPDATE … RETURNING``); never a statement that flushes. ``None`` when the
        tenant has no counter: its rows are left unstamped."""
        ...


#: A synced row's tenant key, or ``None`` for a row that is not stamped. keksdose: a
#: budget's ``household_id``, any other row's ``tenant_id``, falling back to the session's
#: active tenant (``object_session(obj).info``); kastlan: a constant.
type TenantOf = Callable[[object], object | None]
