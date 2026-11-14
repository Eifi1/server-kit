"""The admin router's ports beside the account layer's: the roster, and the invitations an
erasure forgets.

Layer 2 (``docs/auth-harmonization.md`` §12.3–§12.4, ``docs/user-admin-harmonization.md``
§3, §6.4 in ``Eifi1/ui-kit``). **New protocols, never new methods on 0.29's**: an app's
``UserStore`` and ``InvitationStore`` keep the shape they have, and the admin's reads come
through the ports here. Each app implements them over its own tables, or takes the ``sql``
extra's stores (``SqlRosterStore``, ``SqlInvitationStore.forget_accepted``); the in-memory
ones and the ``check_*`` suites are :mod:`eifi1_server_kit.user_admin.testing`.

As the account layer's: async, in the caller's transaction, every write flushes and none
commits (``docs/server-services-harmonization.md`` §2.7). No SQLAlchemy and no FastAPI here.

**Which accounts** these see is the global scope's: every person on the install. A company
scope (kastlan, §12.4) answers its own roster through its
:class:`~eifi1_server_kit.user_admin.scope.AdminScope` and needs no ``RosterStore``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from eifi1_server_kit.auth.ports import AccountRow
from eifi1_server_kit.user_admin.roster import UserListQuery

__all__ = ["AcceptedInvitations", "AccountLogScrub", "RosterCounts", "RosterStore"]


@dataclass(frozen=True, slots=True)
class RosterCounts:
    """What the admin's overview counts (``GET /admin/attention``, admin §2.4): people only,
    a demo account never (§6.4)."""

    #: Every person, active or not, a deactivated one and one asking for its deletion too.
    people: int
    #: The people whose account was made at or after the ``since`` asked for.
    new: int
    #: The people who asked for their deletion and are not erased yet.
    deletion_requests: int


class RosterStore[UserT: AccountRow](Protocol):
    """The user list over the app's accounts (user-admin §3.1): paged, filtered, sorted, on the
    request's session. **People only** everywhere: a demo account (``is_demo``) never, and an
    app's own system account not either (its store narrows what a person is, as
    ``count_people`` does)."""

    async def page(
        self, query: UserListQuery, *, admin_role: str, reviewer_role: str | None
    ) -> tuple[Sequence[UserT], int]:
        """One page of ``query`` (:func:`~eifi1_server_kit.user_admin.parse_roster_query`) and
        how many people match it across every page.

        * ``q`` (its :attr:`~eifi1_server_kit.user_admin.UserListQuery.search_pattern`):
          the address, the first name, the last name, and both orders of the two together
          (``"Ada Lovelace"``, ``"Lovelace Ada"``), case-insensitively; its own ``%`` and
          ``_`` match themselves.
        * ``role``: the account's role is one of them.
        * ``state``: any of the tokens (OR-ed), AND-ed with the rest: ``active``,
          ``deactivated``, ``unverified``, ``verified``, ``password_change_required``,
          ``deletion_scheduled`` (a deletion asked for), ``never_logged_in``, ``admin`` (the
          role is ``admin_role``), ``reviewer`` (the role is ``reviewer_role``; with
          ``None``, any account with review locales), and ``invited``, which no account row
          of the kit's is (an invitation is its own row), so it matches none.
        * ``sort``: ``name`` (last name, then first name), ``email``, ``role``, ``created``,
          ``last_login``, each ascending or descending, NULLs LAST either way, and the id
          ascending as the last key, so equal values never trade places between pages.
        """

    async def counts(self, *, since: datetime) -> RosterCounts:
        """The overview's counts, the account made at or after ``since`` as new."""

    async def deletion_requested(self) -> Sequence[int]:
        """The ids of the deactivated people who asked for their deletion, the oldest request
        first (the lower id first on a tie): the ``after_days`` job's candidates, each then
        re-read and checked in its own transaction
        (:meth:`~eifi1_server_kit.auth.service.AccountService.erase_if_due`)."""


@runtime_checkable
class AcceptedInvitations(Protocol):
    """An invitation store that can forget the invitations an account accepted — what an
    erasure does with them (user-admin §6.4; Kurvenschmiede's third plan, F19): their
    ``accepted_by_user_id`` is ``SET NULL`` when the account goes, and a ``SET NULL`` is
    allowed only together with the scrub, so the row would keep the person's address and
    the admin's note for good.

    Optional: the ``sql`` extra's and the in-memory invitation stores have it; an app's own
    store adds the method, or forgets them in its ``erase`` hook. The erasure checks for it
    with ``isinstance``."""

    async def forget_accepted(self, user_id: int) -> Sequence[str]:
        """Delete every invitation ``user_id`` accepted, flushed, and answer the addresses
        they were sent to (the erasure scrubs those from the admin log too)."""


@runtime_checkable
class AccountLogScrub(Protocol):
    """An admin log that can scrub the rows about an account by its id (0.31, G2;
    Kurvenschmiede): an erasure scrubs the log by address (``AdminActionLog.scrub_email``),
    and a row written before an address change names the earlier one, which no address the
    erasure knows matches.

    Optional: the ``sql`` extra's and the in-memory logs have it; an app's own log adds the
    method. The erasure checks for it with ``isinstance`` and calls it **before the account's
    row is deleted**: on ``[sql]`` the log's keys are ``SET NULL``, so afterwards no row names
    the account any more."""

    async def scrub_account(self, user_id: int) -> None:
        """Clear ``target_email`` on every row whose ``target_user_id`` is ``user_id``,
        flushed; the rows stay, and so does everything else on them."""
