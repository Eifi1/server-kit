"""The port the teams run on: the teams, who is in them, and the invitations into them.

Layer 2, on the pattern of :mod:`eifi1_server_kit.auth.ports`. An app implements
:class:`TeamStore` over its own session and tables, or inherits the ``sql`` extra's
(:class:`~eifi1_server_kit.teams.sql.SqlTeamStore` over
:class:`~eifi1_server_kit.teams.sql.TeamColumns` and
:class:`~eifi1_server_kit.teams.sql.TeamMemberColumns`); the kit never sees the app's models,
and no SQLAlchemy or FastAPI is imported here. **Every write flushes and none commits.**

**An invitation into a team** is the account layer's invitation
(:class:`~eifi1_server_kit.auth.ports.InvitationRow`) whose ``scope`` names the team:
``{"team_id": 7, "team_role": "member"}`` (:func:`team_scope`, Kurvenschmiede's spelling). The
account layer stores, sends, re-sends and withdraws it; the team store finds a team's open
ones (:meth:`TeamStore.open_invitations`) and takes them with the team when it goes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from eifi1_server_kit.auth.ports import AccountRow, InvitationRow
from eifi1_server_kit.teams.schemas import TeamRole

__all__ = [
    "Membership",
    "NewTeam",
    "TeamMemberRow",
    "TeamRow",
    "TeamStore",
    "invitation_team",
    "invitation_team_role",
    "team_scope",
]


class TeamRow(Protocol):
    """A team as the kit reads it: read-only members; every write goes through the store."""

    @property
    def id(self) -> int: ...
    #: Unique, kept stripped.
    @property
    def name(self) -> str: ...
    #: Bumped by a rename and every change of who is in it (an invitation into it and a join
    #: by one included), so a page tells a stale list from a current one. Checked when a
    #: client sends ``If-Match``, never demanded.
    @property
    def version(self) -> int: ...
    @property
    def created_at(self) -> datetime: ...
    #: The app's own team, not a group of people (Kurvenschmiede's "Demo" team every demo
    #: account is in): hidden from every route, a ``404`` wherever it is asked for.
    @property
    def is_system(self) -> bool: ...


class TeamMemberRow(Protocol):
    """One person in one team, and whether they manage it."""

    @property
    def team_id(self) -> int: ...
    @property
    def user_id(self) -> int: ...
    #: A :class:`~eifi1_server_kit.teams.TeamRole` value (a ``TeamRole`` is one).
    @property
    def role(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class NewTeam:
    """A team to insert: its stripped name, and ``is_system`` for the app's own (a demo's)."""

    name: str
    is_system: bool = False


@dataclass(frozen=True, slots=True)
class Membership[MemberT: TeamMemberRow = Any, UserT: AccountRow = Any]:
    """One membership and the account it is: what a team's member list is made of."""

    member: MemberT
    user: UserT


class TeamStore[TeamT: TeamRow = Any, MemberT: TeamMemberRow = Any](Protocol):
    """The app's teams, their members and the invitations into them, on the request's session.
    Async; every write flushes and never commits."""

    async def get(self, team_id: int) -> TeamT | None:
        """The team, a system team too, or ``None``."""

    async def names(self, team_ids: Sequence[int]) -> Mapping[int, str]:
        """The names of the teams that exist among ``team_ids``, by id (the admin log's)."""

    async def name_taken(self, name: str, *, other_than: int | None = None) -> bool:
        """Whether a team other than ``other_than`` is called exactly ``name``."""

    async def listing(self) -> Sequence[TeamT]:
        """Every team but the system teams, by name: the admin's list."""

    async def of_user(self, user_id: int) -> Sequence[tuple[TeamT, str]]:
        """The teams the account is in, but the system teams, by name, each with its role there."""

    async def managed(self, team_id: int, user_id: int) -> TeamT | None:
        """The team, if the account manages it and it is no system team; else ``None`` — for a
        team it is merely in as much as for one it is not in. One query: the manager's
        predicate."""

    async def add(self, new: NewTeam) -> TeamT:
        """Insert and flush, ``version`` 1."""

    async def rename(self, team: TeamT, name: str) -> None:
        """The new name, flushed (the service bumps the version)."""

    async def bump(self, team: TeamT) -> None:
        """``version`` one higher, flushed."""

    async def delete(self, team: TeamT) -> None:
        """Delete the team and flush; its memberships and every invitation into it go with it
        (Kurvenschmiede's ``CASCADE``)."""

    async def members(self, team_ids: Sequence[int]) -> Sequence[Membership[MemberT, Any]]:
        """The memberships of these teams with their accounts, by team, then by the account's
        ``last_name``, ``first_name`` and id."""

    async def membership(self, team_id: int, user_id: int) -> MemberT | None:
        """The account's membership of the team, or ``None``."""

    async def add_member(self, team_id: int, user_id: int, role: TeamRole) -> MemberT:
        """Insert and flush. One membership per team and account (the app's unique
        constraint); the kit asks :meth:`membership` first."""

    async def set_member_role(self, member: MemberT, role: TeamRole) -> None:
        """The new role, flushed."""

    async def remove_member(self, member: MemberT) -> None:
        """Delete and flush."""

    async def open_invitations(self, team_ids: Sequence[int]) -> Mapping[int, Sequence[InvitationRow]]:
        """The invitations into these teams neither accepted nor withdrawn (expired ones too),
        by team, each oldest first, the lower id first on a tie. A team with none may be left
        out."""

    async def set_invitation_role(self, invitation: InvitationRow, role: TeamRole) -> None:
        """The role in the team an open invitation gives, written into its ``scope`` (and the
        app's own column of it), flushed: inviting the address again in another role."""


def team_scope(team_id: int, role: TeamRole | str) -> dict[str, Any]:
    """What the account layer stores as a team invitation's ``scope``: the team and the role in
    it, Kurvenschmiede's spelling, so its stored invitations read as the kit's."""
    return {"team_id": team_id, "team_role": TeamRole(role).value}


def invitation_team(invitation: InvitationRow) -> int | None:
    """The team an invitation brings its invitee into, or ``None`` for one that names none."""
    team_id = (invitation.scope or {}).get("team_id")
    return team_id if isinstance(team_id, int) and not isinstance(team_id, bool) else None


def invitation_team_role(invitation: InvitationRow) -> TeamRole:
    """The role in the team an invitation gives: its scope's, a member's when it names none."""
    return TeamRole((invitation.scope or {}).get("team_role") or TeamRole.MEMBER)
