"""The admin scope: which accounts an admin sees and acts on, how the last admin is counted,
whether a role is one value or a set, and which roles may be given.

``docs/auth-harmonization.md`` §12.4 in ``Eifi1/ui-kit``. ``user_admin_router`` asks the
scope, never the stores, for everything that depends on WHO the admin administers, so one
router serves every app:

* **the global scope** (:class:`GlobalScope`): keksdose, Kurvenschmiede and Staunlabor, and
  kastlan's platform under ``/platform``: every person on the install, one role per account
  (the account row's ``role``), the last admin counted over the install;
* **a company scope** (kastlan's company admin, written by kastlan when it moves): the acting
  company's members, their roles in it as a set, ``other_companies`` for an action that
  reaches the whole account, ``last_admin`` naming the companies, the company on every log
  row. Its seats are the app's ``role_changing`` and ``activating`` hooks
  (:class:`~eifi1_server_kit.user_admin.service.UserAdminHooks`).

The scope is per request — the app's dependency answers it, on the request's session, as the
stores are — because a company scope's company is the caller's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from eifi1_server_kit.auth.ports import AccountRow, UserStore
from eifi1_server_kit.user_admin.actions import AdminAction
from eifi1_server_kit.user_admin.ports import RosterCounts, RosterStore
from eifi1_server_kit.user_admin.roster import UserListQuery
from eifi1_server_kit.user_admin.rules import refuse_last_admin

__all__ = ["AdminScope", "GlobalScope"]

#: The review scope's columns, cleared with the reviewer role (keksdose, Kurvenschmiede).
_REVIEW_SCOPE = {"translation_review_locales": None, "translation_review_areas": None}


class AdminScope[UserT: AccountRow](Protocol):
    """The accounts one admin administers, for one request (§12.4)."""

    async def find(self, user_id: int) -> UserT | None:
        """The account, when it is in the scope; ``None`` otherwise — none at all, or one
        outside it, alike (``user_not_found``). A demo account is found; the router refuses
        it (``demo_refused``)."""

    async def roster(self, query: UserListQuery) -> tuple[Sequence[UserT], int]:
        """One page of the people in the scope, and how many match in all
        (:meth:`~eifi1_server_kit.user_admin.ports.RosterStore.page`'s promises)."""

    async def counts(self, *, since: datetime) -> RosterCounts:
        """The overview's counts over the people in the scope."""

    async def roles_of(self, user: UserT) -> frozenset[str]:
        """The account's roles in the scope: one in the global scope."""

    async def grantable_roles(self) -> frozenset[str]:
        """The roles the role change may give (``role_not_grantable`` for any other)."""

    async def set_roles(self, user: UserT, roles: frozenset[str]) -> None:
        """Give the account exactly ``roles`` in the scope, flushed. The router has checked
        the rules and the confirmation."""

    async def active_admins(self) -> int:
        """The active admins in the scope, demo accounts never: the roster's ``summary``."""

    async def refuse_last_admin(self, user: UserT) -> None:
        """The change takes ``user``'s admin role in the scope away, or switches it off: raise
        ``AccountError(LAST_ADMIN)`` — with the scope's ``extra`` (kastlan's ``companies``) —
        when that would leave no active admin."""

    async def refuse_account_wide(self, user: UserT, action: AdminAction) -> None:
        """``action`` reaches the whole account, beyond the scope (deactivating, reactivating,
        a forced password change, an erasure): raise where the scope may not
        (kastlan's ``other_companies``)."""

    def record_fields(self) -> Mapping[str, Any]:
        """The scope's fields on every log row the router writes (kastlan's ``company_id``);
        empty in the global scope."""


class GlobalScope[UserT: AccountRow]:
    """Every person on the install, one role per account (§12.4): keksdose, Kurvenschmiede,
    Staunlabor, kastlan's platform. Over the account store and the roster; the role is the
    row's ``role``, and leaving the reviewer role leaves its scope behind (a reviewer made a
    member or an admin keeps no locales)."""

    def __init__(
        self,
        *,
        users: UserStore[UserT],
        roster: RosterStore[UserT],
        admin_role: str,
        reviewer_role: str | None,
        grantable: frozenset[str],
    ) -> None:
        self._users = users
        self._roster = roster
        self._admin_role = admin_role
        self._reviewer_role = reviewer_role
        self._grantable = grantable

    async def find(self, user_id: int) -> UserT | None:
        return await self._users.get(user_id)

    async def roster(self, query: UserListQuery) -> tuple[Sequence[UserT], int]:
        return await self._roster.page(query, admin_role=self._admin_role, reviewer_role=self._reviewer_role)

    async def counts(self, *, since: datetime) -> RosterCounts:
        return await self._roster.counts(since=since)

    async def roles_of(self, user: UserT) -> frozenset[str]:
        return frozenset({user.role})

    async def grantable_roles(self) -> frozenset[str]:
        return self._grantable

    async def set_roles(self, user: UserT, roles: frozenset[str]) -> None:
        if len(roles) != 1:
            raise ValueError("the global scope gives an account exactly one role")
        (role,) = roles
        await self._users.apply(user, {"role": role, **_REVIEW_SCOPE})

    async def active_admins(self) -> int:
        return await self._users.count_active(role=self._admin_role)

    async def refuse_last_admin(self, user: UserT) -> None:
        refuse_last_admin(
            target_is_admin=user.is_active and user.role == self._admin_role,
            active_admins=await self.active_admins(),
            change_removes_admin=True,
        )

    async def refuse_account_wide(self, user: UserT, action: AdminAction) -> None:
        """The whole account is the scope's: nothing to refuse."""

    def record_fields(self) -> Mapping[str, Any]:
        return {}
