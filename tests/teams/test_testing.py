"""``eifi1_server_kit.teams.testing``: the in-memory team store keeps the port's promises, and the
contract suite names the first promise a store breaks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from eifi1_server_kit.auth.ports import NewAccount, NewInvitation
from eifi1_server_kit.auth.testing import InMemoryInvitationStore, InMemoryUserStore, MemoryInvitationRow
from eifi1_server_kit.teams import TeamRole
from eifi1_server_kit.teams.ports import NewTeam, invitation_team, invitation_team_role, team_scope
from eifi1_server_kit.teams.testing import InMemoryTeamStore, MemoryTeamRow, check_team_store

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _account(email: str, *, last_name: str = "Lovelace", first_name: str = "Ada") -> NewAccount:
    return NewAccount(
        email=email, password_hash="x", first_name=first_name, last_name=last_name, locale="en", role="MEMBER"
    )


async def _world() -> tuple[InMemoryUserStore, InMemoryInvitationStore, InMemoryTeamStore]:
    users = InMemoryUserStore()
    invitations = InMemoryInvitationStore()
    return users, invitations, InMemoryTeamStore(users.rows, invitations.rows)


async def test_the_in_memory_store_keeps_the_contract() -> None:
    users, invitations, teams = await _world()
    one = await users.add(_account("one@example.org"))
    two = await users.add(_account("two@example.org", last_name="Babbage"))
    await check_team_store(teams, invitations, user_id=one.id, other_user_id=two.id)
    # Twice on the same store: every row the suite writes carries a fresh token.
    await check_team_store(teams, invitations, user_id=one.id, other_user_id=two.id)


async def test_without_invitations_a_store_has_none_to_list_or_take() -> None:
    users = InMemoryUserStore()
    teams = InMemoryTeamStore(users.rows)
    team = await teams.add(NewTeam("Rig"))
    assert await teams.open_invitations([team.id]) == {}
    await teams.delete(team)
    assert await teams.get(team.id) is None


async def test_a_membership_whose_account_is_gone_is_not_listed() -> None:
    users, _invitations, teams = await _world()
    ada = await users.add(_account("ada@example.org"))
    team = await teams.add(NewTeam("Rig"))
    await teams.add_member(team.id, ada.id, TeamRole.MEMBER)
    await users.delete(ada)
    assert await teams.members([team.id]) == []


class _ListsTheSystemTeams(InMemoryTeamStore):
    async def listing(self) -> Sequence[MemoryTeamRow]:
        return sorted(self.rows.values(), key=lambda team: team.name)


async def test_the_suite_names_the_promise_a_store_breaks() -> None:
    users = InMemoryUserStore()
    invitations = InMemoryInvitationStore()
    teams = _ListsTheSystemTeams(users.rows, invitations.rows)
    one = await users.add(_account("one@example.org"))
    two = await users.add(_account("two@example.org"))
    with pytest.raises(AssertionError, match="listing: no system team"):
        await check_team_store(teams, invitations, user_id=one.id, other_user_id=two.id)


async def test_the_suite_wants_two_accounts() -> None:
    _users, invitations, teams = await _world()
    with pytest.raises(ValueError, match="two different accounts"):
        await check_team_store(teams, invitations, user_id=1, other_user_id=1)


def _invitation(scope: dict[str, object] | None) -> MemoryInvitationRow:
    return MemoryInvitationRow(
        id=1,
        email="ada@example.org",
        token_hash=None,
        expires_at=NOW + timedelta(days=14),
        locale="en",
        invited_by_user_id=None,
        created_at=NOW,
        scope=scope,
    )


def test_a_team_invitation_names_its_team_and_role_in_its_scope() -> None:
    assert team_scope(7, TeamRole.MANAGER) == {"team_id": 7, "team_role": "manager"}
    assert team_scope(7, "member") == {"team_id": 7, "team_role": "member"}
    named = _invitation(team_scope(7, TeamRole.MANAGER))
    assert (invitation_team(named), invitation_team_role(named)) == (7, TeamRole.MANAGER)
    # No scope, another app's scope, or a team id that is no integer: no team, a member's role.
    scopes: list[dict[str, object] | None] = [None, {"company_id": 3}, {"team_id": "7"}, {"team_id": True}]
    for scope in scopes:
        unnamed = _invitation(scope)
        assert (invitation_team(unnamed), invitation_team_role(unnamed)) == (None, TeamRole.MEMBER)


async def test_an_invitation_added_by_the_store_keeps_its_scope() -> None:
    _users, invitations, teams = await _world()
    team = await teams.add(NewTeam("Rig"))
    invitation = await invitations.add(
        NewInvitation(
            email="ada@example.org",
            token_hash="d" * 64,
            expires_at=NOW + timedelta(days=14),
            locale="en",
            invited_by_user_id=None,
            scope=team_scope(team.id, TeamRole.MEMBER),
        )
    )
    assert [row.id for row in (await teams.open_invitations([team.id]))[team.id]] == [invitation.id]
