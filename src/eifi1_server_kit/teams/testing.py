"""Test helpers for the teams: an in-memory team store and the port's conformance check.

On the pattern of :mod:`eifi1_server_kit.auth.testing`. In the wheel and NOT re-exported from
:mod:`eifi1_server_kit.teams`: import it as ``eifi1_server_kit.teams.testing``. No SQLAlchemy and
no FastAPI.

* :class:`InMemoryTeamStore` — a :class:`~eifi1_server_kit.teams.ports.TeamStore` over
  :class:`MemoryTeamRow` and :class:`MemoryTeamMemberRow`, reading the accounts and the
  invitations of the account layer's in-memory stores (their ``rows``), as the app's tables
  join them; deleting a team takes its memberships and invitations, as the cascade does;
* :func:`check_team_store` — the port's promises, run once by an app against its own store
  (or the ``sql`` extra's), on a fresh session over its test database as it is. It raises
  :class:`AssertionError` naming the first promise that fails, writes under fresh names and
  digests, and asserts only on what it adds.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from eifi1_server_kit.auth.ports import AccountRow, InvitationRow, InvitationStore, NewInvitation
from eifi1_server_kit.auth.tokens import INVITE_TTL, hash_token
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.teams.ports import (
    Membership,
    NewTeam,
    TeamStore,
    invitation_team,
    invitation_team_role,
    team_scope,
)
from eifi1_server_kit.teams.schemas import TeamRole

__all__ = ["InMemoryTeamStore", "MemoryTeamMemberRow", "MemoryTeamRow", "check_team_store"]

#: An id no test database reaches: the largest a Postgres ``integer`` holds.
_MISSING_ID = 2**31 - 1


@dataclass(slots=True)
class MemoryTeamRow:
    """One team in memory; satisfies :class:`~eifi1_server_kit.teams.ports.TeamRow`."""

    id: int
    name: str
    created_at: datetime
    version: int = 1
    is_system: bool = False


@dataclass(slots=True)
class MemoryTeamMemberRow:
    """One membership in memory; satisfies :class:`~eifi1_server_kit.teams.ports.TeamMemberRow`."""

    id: int
    team_id: int
    user_id: int
    role: TeamRole
    created_at: datetime


class InMemoryTeamStore:
    """A :class:`~eifi1_server_kit.teams.ports.TeamStore` in memory.

    ``users`` are the accounts by id (an ``InMemoryUserStore``'s ``rows``), read for a team's
    member list; ``invitations`` the invitations by id (an ``InMemoryInvitationStore``'s
    ``rows``), read for a team's open ones and pruned with a deleted team. ``now`` stamps
    ``created_at``."""

    def __init__(
        self,
        users: Mapping[int, AccountRow],
        invitations: MutableMapping[int, Any] | None = None,
        *,
        now: Clock = utc_now,
    ) -> None:
        #: Every team, by id.
        self.rows: dict[int, MemoryTeamRow] = {}
        #: Every membership, by id.
        self.memberships: dict[int, MemoryTeamMemberRow] = {}
        self.users = users
        self.invitations: MutableMapping[int, Any] = invitations if invitations is not None else {}
        self.now = now
        self._ids = itertools.count(1)
        self._member_ids = itertools.count(1)

    async def get(self, team_id: int) -> MemoryTeamRow | None:
        return self.rows.get(team_id)

    async def names(self, team_ids: Sequence[int]) -> Mapping[int, str]:
        return {team_id: self.rows[team_id].name for team_id in team_ids if team_id in self.rows}

    async def name_taken(self, name: str, *, other_than: int | None = None) -> bool:
        return any(team.name == name and team.id != other_than for team in self.rows.values())

    async def listing(self) -> Sequence[MemoryTeamRow]:
        return sorted((team for team in self.rows.values() if not team.is_system), key=lambda team: team.name)

    async def of_user(self, user_id: int) -> Sequence[tuple[MemoryTeamRow, str]]:
        found = [
            (self.rows[member.team_id], member.role)
            for member in self.memberships.values()
            if member.user_id == user_id and not self.rows[member.team_id].is_system
        ]
        return sorted(found, key=lambda pair: pair[0].name)

    async def managed(self, team_id: int, user_id: int) -> MemoryTeamRow | None:
        member = await self.membership(team_id, user_id)
        team = self.rows.get(team_id)
        if member is None or team is None or team.is_system or member.role != TeamRole.MANAGER:
            return None
        return team

    async def add(self, new: NewTeam) -> MemoryTeamRow:
        team = MemoryTeamRow(id=next(self._ids), name=new.name, is_system=new.is_system, created_at=self.now())
        self.rows[team.id] = team
        return team

    async def rename(self, team: MemoryTeamRow, name: str) -> None:
        team.name = name

    async def bump(self, team: MemoryTeamRow) -> None:
        team.version += 1

    async def delete(self, team: MemoryTeamRow) -> None:
        self.rows.pop(team.id, None)
        for member_id in [key for key, member in self.memberships.items() if member.team_id == team.id]:
            del self.memberships[member_id]
        for invitation_id in [key for key, row in self.invitations.items() if invitation_team(row) == team.id]:
            del self.invitations[invitation_id]

    async def members(self, team_ids: Sequence[int]) -> Sequence[Membership[MemoryTeamMemberRow, Any]]:
        wanted = set(team_ids)
        found = [
            Membership(member, self.users[member.user_id])
            for member in self.memberships.values()
            if member.team_id in wanted and member.user_id in self.users
        ]
        return sorted(
            found,
            key=lambda entry: (entry.member.team_id, entry.user.last_name, entry.user.first_name, entry.user.id),
        )

    async def membership(self, team_id: int, user_id: int) -> MemoryTeamMemberRow | None:
        return next(
            (row for row in self.memberships.values() if row.team_id == team_id and row.user_id == user_id), None
        )

    async def add_member(self, team_id: int, user_id: int, role: TeamRole) -> MemoryTeamMemberRow:
        member = MemoryTeamMemberRow(
            id=next(self._member_ids), team_id=team_id, user_id=user_id, role=TeamRole(role), created_at=self.now()
        )
        self.memberships[member.id] = member
        return member

    async def set_member_role(self, member: MemoryTeamMemberRow, role: TeamRole) -> None:
        member.role = TeamRole(role)

    async def remove_member(self, member: MemoryTeamMemberRow) -> None:
        self.memberships.pop(member.id, None)

    async def open_invitations(self, team_ids: Sequence[int]) -> Mapping[int, Sequence[InvitationRow]]:
        wanted = set(team_ids)
        found: dict[int, list[InvitationRow]] = {}
        for row in sorted(self.invitations.values(), key=lambda row: (row.created_at, row.id)):
            team_id = invitation_team(row)
            if team_id in wanted and row.accepted_at is None and row.revoked_at is None:
                found.setdefault(team_id, []).append(row)
        return found

    async def set_invitation_role(self, invitation: InvitationRow, role: TeamRole) -> None:
        row: Any = invitation
        row.scope = {**(row.scope or {}), "team_role": TeamRole(role).value}


# ── The check ───────────────────────────────────────────────────────────────


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"TeamStore: {message}")


async def check_team_store(
    store: TeamStore[Any, Any],
    invitations: InvitationStore[Any],
    *,
    user_id: int,
    other_user_id: int,
) -> None:
    """Does the app's team store keep the port's promises? Raises :class:`AssertionError` naming
    the first that fails.

    ``user_id`` and ``other_user_id`` are two different existing accounts. ``invitations`` is the
    app's invitation store, the one whose rows ``store`` reads (on ``[sql]``,
    :class:`~eifi1_server_kit.teams.sql.SqlTeamInvitationStore`). The check adds three teams under
    fresh names (one a system team), puts both accounts in them, invites four fresh addresses
    into them, and deletes one team: the cascade is the database's, so foreign keys must be
    enforced (SQLite: ``PRAGMA foreign_keys=ON``). The member list's order is checked on the
    accounts' own names: give accounts whose names any collation orders alike (or the same
    name, which leaves the order to their ids).
    """
    if user_id == other_user_id:
        raise ValueError("check_team_store needs two different accounts")
    token = secrets.token_hex(6)

    # 1. add, get, names, name_taken.
    first = await store.add(NewTeam(f"Checked {token} A"))
    second = await store.add(NewTeam(f"Checked {token} B"))
    system = await store.add(NewTeam(f"Checked {token} S", is_system=True))
    ids = {first.id, second.id, system.id}
    _check(len(ids) == 3 and all(isinstance(team_id, int) for team_id in ids), "add: each team has its own id")
    _check(first.name == f"Checked {token} A" and first.version == 1, "add: the name, version 1")
    _check(not first.is_system and system.is_system, "add: is_system as given")
    _check(isinstance(first.created_at, datetime), "add: created_at is set")
    found = await store.get(system.id)
    _check(found is not None and found.id == system.id, "get: a team by its id, a system team too")
    _check(await store.get(_MISSING_ID) is None, "get: a missing id is None")
    names = await store.names([first.id, second.id, _MISSING_ID])
    _check(dict(names) == {first.id: first.name, second.id: second.name}, "names: the names of the teams that exist")
    _check(await store.name_taken(first.name), "name_taken: a team's own name is taken")
    _check(not await store.name_taken(first.name, other_than=first.id), "name_taken: not by the team itself")
    _check(not await store.name_taken(f"Checked {token} none"), "name_taken: a name no team has is free")

    # 2. listing: no system team, by name.
    listed = [team.id for team in await store.listing()]
    _check(system.id not in listed, "listing: no system team")
    _check(first.id in listed and second.id in listed, "listing: every other team")
    _check(listed.index(first.id) < listed.index(second.id), "listing: by name")

    # 3. rename and bump.
    await store.rename(second, f"Checked {token} C")
    renamed = await store.get(second.id)
    _check(renamed is not None and renamed.name == f"Checked {token} C", "rename: the new name reads back")
    await store.bump(second)
    bumped = await store.get(second.id)
    _check(bumped is not None and bumped.version == 2, "bump: the version one higher")

    # 4. memberships.
    manager = await store.add_member(first.id, user_id, TeamRole.MANAGER)
    member = await store.add_member(first.id, other_user_id, TeamRole.MEMBER)
    await store.add_member(second.id, user_id, TeamRole.MEMBER)
    in_system = await store.add_member(system.id, user_id, TeamRole.MANAGER)
    _check(
        (manager.team_id, manager.user_id, manager.role) == (first.id, user_id, TeamRole.MANAGER),
        "add_member: the team, the account and the role read back",
    )
    _check(isinstance(manager.created_at, datetime), "add_member: created_at is set")
    again = await store.membership(first.id, other_user_id)
    _check(again is not None and again.role == TeamRole.MEMBER, "membership: the account's membership of the team")
    _check(await store.membership(second.id, other_user_id) is None, "membership: None for a team it is not in")
    listed_members = await store.members([second.id, first.id])
    _check(
        {(entry.member.team_id, entry.member.user_id) for entry in listed_members}
        == {(first.id, user_id), (first.id, other_user_id), (second.id, user_id)},
        "members: every membership of the teams asked for, and no other",
    )
    _check(all(entry.user.id == entry.member.user_id for entry in listed_members), "members: each with its account")
    keys = [
        (entry.member.team_id, entry.user.last_name, entry.user.first_name, entry.user.id) for entry in listed_members
    ]
    _check(keys == sorted(keys), "members: by team, then last name, first name and id")

    # 5. of_user and managed.
    mine = [(team.id, str(role)) for team, role in await store.of_user(user_id)]
    _check(
        [pair for pair in mine if pair[0] in ids] == [(first.id, "manager"), (second.id, "member")],
        "of_user: the account's teams with its roles, by name, no system team",
    )
    managed = await store.managed(first.id, user_id)
    _check(managed is not None and managed.id == first.id, "managed: a team the account manages")
    _check(await store.managed(first.id, other_user_id) is None, "managed: not a team the account is merely in")
    _check(await store.managed(second.id, other_user_id) is None, "managed: not a team the account is not in")
    _check(await store.managed(system.id, user_id) is None, "managed: never a system team")

    # 6. set_member_role and remove_member.
    await store.set_member_role(member, TeamRole.MANAGER)
    promoted = await store.membership(first.id, other_user_id)
    _check(promoted is not None and promoted.role == TeamRole.MANAGER, "set_member_role: the new role reads back")
    await store.remove_member(in_system)
    _check(await store.membership(system.id, user_id) is None, "remove_member: the membership is gone")

    # 7. open_invitations and set_invitation_role.
    now = utc_now()

    async def invite(label: str, team_id: int) -> Any:
        return await invitations.add(
            NewInvitation(
                email=f"team-{token}-{label}@example.org",
                token_hash=hash_token(f"{token}-{label}"),
                expires_at=now + INVITE_TTL,
                locale="en",
                invited_by_user_id=user_id,
                scope=team_scope(team_id, TeamRole.MEMBER),
            )
        )

    older = await invite("older", first.id)
    newer = await invite("newer", first.id)
    accepted = await invite("accepted", first.id)
    revoked = await invite("revoked", first.id)
    elsewhere = await invite("elsewhere", second.id)
    await invitations.accept(accepted, user_id=other_user_id, at=now - timedelta(minutes=1))
    await invitations.revoke(revoked, at=now - timedelta(minutes=1))
    waiting = await store.open_invitations([first.id, second.id])
    _check(
        [row.id for row in waiting.get(first.id, ())] == [older.id, newer.id],
        "open_invitations: a team's invitations neither accepted nor withdrawn, oldest first",
    )
    _check([row.id for row in waiting.get(second.id, ())] == [elsewhere.id], "open_invitations: by team")
    await store.set_invitation_role(newer, TeamRole.MANAGER)
    reread = await invitations.get(newer.id)
    _check(
        reread is not None and invitation_team_role(reread) is TeamRole.MANAGER and invitation_team(reread) == first.id,
        "set_invitation_role: the scope names the team and the new role",
    )

    # 8. delete: the memberships and the invitations go with the team.
    await store.delete(first)
    _check(await store.get(first.id) is None, "delete: the team is gone")
    _check(await store.membership(first.id, user_id) is None, "delete: its memberships go with it")
    _check(await invitations.get(older.id) is None, "delete: its invitations go with it")
    _check(await store.get(second.id) is not None, "delete: no other team")
