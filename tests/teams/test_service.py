"""The teams' service on Kurvenschmiede's rules (``team_service``, ``invitation_service``): the
version, the coded refusals, a re-add as a role change, the manager's reach, the invitations
into a team, the audit rows, the views, the system team, and the joins by invitation."""

from collections.abc import Sequence
from typing import Any

import pytest

from eifi1_server_kit.auth import AuthError, AuthErrorCode
from eifi1_server_kit.auth.ports import NewInvitation
from eifi1_server_kit.auth.schemas import RegisterRequest
from eifi1_server_kit.auth.tokens import INVITE_TTL
from eifi1_server_kit.teams import NewTeam, TeamError, TeamErrorCode, TeamRole, team_scope
from eifi1_server_kit.teams.service import (
    TeamAccountHooks,
    TeamHooks,
    TeamInvitationSent,
    add_system_member,
    join_team,
)
from eifi1_server_kit.user_admin import AccountError, AccountErrorCode, GroupRef
from tests.auth_fakes import MemoryUser
from tests.teams._teams_world import TeamsWorld

MEMBER, MANAGER = TeamRole.MEMBER, TeamRole.MANAGER


def _code(error: pytest.ExceptionInfo[TeamError]) -> tuple[TeamErrorCode, int, dict[str, object]]:
    return error.value.code, error.value.status_code, dict(error.value.extra)


# ── Creating, renaming, deleting ────────────────────────────────────────────


async def test_a_team_is_created_by_its_stripped_name_once(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team = await service.create(admin, "  Rig  ")
    assert (team.name, team.version, team.is_system) == ("Rig", 1, False)
    row = world.last()
    assert (row.action, row.actor_id, row.target_group_id, row.detail) == ("group_create", admin.id, team.id, {})
    with pytest.raises(TeamError) as taken:
        await service.create(admin, " Rig")
    assert _code(taken) == (TeamErrorCode.TEAM_NAME_TAKEN, 409, {"name": "Rig"})
    assert str(taken.value) == "There is already a team called Rig."
    assert world.actions() == ["group_create"]


async def test_a_rename_bumps_the_version_and_names_no_name_and_the_same_name_writes_nothing(
    world: TeamsWorld,
) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team = await service.create(admin, "Rig")
    await service.create(admin, "Bench")
    await service.rename(admin, team, " Rig ")
    assert (team.name, team.version, world.actions()) == ("Rig", 1, ["group_create", "group_create"])
    with pytest.raises(TeamError) as taken:
        await service.rename(admin, team, "Bench")
    assert _code(taken) == (TeamErrorCode.TEAM_NAME_TAKEN, 409, {"name": "Bench"})
    await service.rename(admin, team, "Test rig")
    row = world.last()
    assert (team.name, team.version) == ("Test rig", 2)
    assert (row.action, row.target_group_id, row.detail) == ("group_rename", team.id, {})


async def test_a_stale_version_is_refused_with_the_team_s_name_and_none_is_never_demanded(world: TeamsWorld) -> None:
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    service.check_version(team, None)
    service.check_version(team, 1)
    with pytest.raises(TeamError) as stale:
        service.check_version(team, 2)
    assert _code(stale) == (TeamErrorCode.TEAM_CHANGED, 409, {"name": "Rig"})
    assert "Rig" in str(stale.value)


async def test_a_delete_is_acknowledged_and_its_row_counts_what_goes_and_keeps_the_name(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    ada, bob = world.person("ada@example.com"), world.person("bob@example.com")
    asked: list[Sequence[int]] = []

    async def deleting(team_ids: Sequence[int]) -> dict[int, int]:
        asked.append(list(team_ids))
        return dict.fromkeys(team_ids, 4)

    world.team_hooks = TeamHooks(deleting=deleting)
    service = world.teams_service()
    team = await service.create(admin, "Rig")
    await service.add(admin, team, ada.email, MANAGER, by_admin=True)
    await service.add(admin, team, bob.email, MEMBER, by_admin=True)
    await service.add(admin, team, "new@example.com", MEMBER, by_admin=True)
    accepted = await service.add(admin, team, "taken@example.com", MEMBER, by_admin=True)
    assert isinstance(accepted, TeamInvitationSent)
    world.invitations.rows[max(world.invitations.rows)].accepted_at = world.clock()
    with pytest.raises(AccountError) as unconfirmed:
        await service.delete(admin, team, acknowledged=False)
    assert unconfirmed.value.code is AccountErrorCode.CONFIRMATION_REQUIRED
    assert await world.teams.get(team.id) is not None
    await service.delete(admin, team, acknowledged=True)
    row = world.last()
    assert (row.action, row.target_group_id, row.target_group_name) == ("group_delete", team.id, "Rig")
    assert row.detail == {"members": 2, "grants": 4, "invitations": 1}, "an accepted invitation is history"
    assert asked == [[team.id]]
    assert await world.teams.get(team.id) is None
    assert world.invitations.rows == {}, "the team's invitations go with it"


async def test_without_the_deleting_hook_a_team_holds_no_grants(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team = await service.create(admin, "Rig")
    assert [view.grants for view in await service.all_teams()] == [0]
    await service.delete(admin, team, acknowledged=True)
    assert world.last().detail == {"members": 0, "grants": 0, "invitations": 0}


# ── Who is in it ────────────────────────────────────────────────────────────


async def test_an_add_is_a_new_membership_its_row_and_the_app_s_notice(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    ada = world.person("ada@example.com")
    told: list[tuple[int, int, int]] = []

    async def member_added(actor: MemoryUser, team: Any, user: MemoryUser) -> None:
        told.append((actor.id, team.id, user.id))

    world.team_hooks = TeamHooks(member_added=member_added)
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    added = await service.add(admin, team, "  ADA@example.com ", MEMBER, by_admin=True)
    assert added is ada and team.version == 2
    member = await world.teams.membership(team.id, ada.id)
    assert member is not None and member.role is MEMBER
    row = world.last()
    assert (row.action, row.actor_id, row.target_user_id, row.target_email, row.target_group_id, row.detail) == (
        "membership_add",
        admin.id,
        ada.id,
        "ada@example.com",
        team.id,
        {"role": "member"},
    )
    assert told == [(admin.id, team.id, ada.id)]


async def test_a_re_add_is_a_role_change_and_the_same_role_changes_nothing(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    ada = world.person("ada@example.com")
    told: list[int] = []

    async def member_added(_actor: MemoryUser, _team: Any, user: MemoryUser) -> None:
        told.append(user.id)

    world.team_hooks = TeamHooks(member_added=member_added)
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    await service.add(admin, team, ada.email, MEMBER, by_admin=True)
    await service.add(admin, team, ada.email, MEMBER, by_admin=True)
    assert (team.version, world.actions()) == (2, ["membership_add"])
    await service.add(admin, team, ada.email, MANAGER, by_admin=True)
    row = world.last()
    assert (row.action, row.detail, team.version) == ("membership_role", {"from": "member", "to": "manager"}, 3)
    assert told == [ada.id], "a role change is not news"


async def test_a_manager_adds_members_only_and_never_changes_a_manager(world: TeamsWorld) -> None:
    manager, other = world.person("lead@example.com"), world.person("other@example.com")
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    await world.teams.add_member(team.id, manager.id, MANAGER)
    await world.teams.add_member(team.id, other.id, MANAGER)
    with pytest.raises(TeamError) as named:
        await service.add(manager, team, "nobody@example.com", MANAGER, by_admin=False)
    assert _code(named) == (TeamErrorCode.MANAGER_NAMED_BY_ADMIN, 409, {})
    with pytest.raises(TeamError) as kept:
        await service.add(manager, team, " OTHER@example.com", MEMBER, by_admin=False)
    assert _code(kept) == (TeamErrorCode.MANAGER_KEPT_BY_ADMIN, 409, {"email": "other@example.com"})
    assert "other@example.com" in str(kept.value)
    assert world.actions() == [] and team.version == 1


async def test_a_demo_s_address_is_refused_by_its_shape_before_anything_is_looked_up(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    asked: list[str] = []

    async def refuse_address(address: str) -> None:
        asked.append(address)
        if address.endswith("@demo.example"):
            raise AuthError(AuthErrorCode.EMAIL_TAKEN, "a demo's address")

    world.team_hooks = TeamHooks(refuse_address=refuse_address)
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    with pytest.raises(AuthError):
        await service.add(admin, team, " Demo+1@Demo.example", MEMBER, by_admin=True)
    assert asked == ["demo+1@demo.example"] and world.invitations.rows == {} and team.version == 1


async def test_an_admin_names_and_un_names_a_manager_and_the_same_role_writes_nothing(world: TeamsWorld) -> None:
    admin, ada = world.person("root@example.com", role="ADMIN"), world.person("ada@example.com")
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    with pytest.raises(TeamError) as missing:
        await service.set_role(admin, team, ada.id, MANAGER)
    assert _code(missing) == (TeamErrorCode.TEAM_NOT_FOUND, 404, {})
    assert str(missing.value) == "Not a member of this team"
    await world.teams.add_member(team.id, ada.id, MEMBER)
    await service.set_role(admin, team, ada.id, MEMBER)
    assert (world.actions(), team.version) == ([], 1)
    await service.set_role(admin, team, ada.id, MANAGER)
    row = world.last()
    assert (row.action, row.target_user_id, row.detail) == (
        "membership_role",
        ada.id,
        {"from": "member", "to": "manager"},
    )
    assert team.version == 2


async def test_a_removal_says_the_role_it_took_and_a_manager_removes_members_only(world: TeamsWorld) -> None:
    admin, lead = world.person("root@example.com", role="ADMIN"), world.person("lead@example.com")
    ada = world.person("ada@example.com")
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    await world.teams.add_member(team.id, lead.id, MANAGER)
    await world.teams.add_member(team.id, ada.id, MEMBER)
    with pytest.raises(TeamError) as missing:
        await service.remove(lead, team, 999, by_admin=False)
    assert _code(missing) == (TeamErrorCode.TEAM_NOT_FOUND, 404, {})
    with pytest.raises(TeamError) as manager:
        await service.remove(lead, team, lead.id, by_admin=False)
    assert _code(manager) == (TeamErrorCode.MANAGER_REMOVED_BY_ADMIN, 409, {})
    await service.remove(lead, team, ada.id, by_admin=False)
    row = world.last()
    assert (row.action, row.actor_id, row.target_user_id, row.target_email, row.detail) == (
        "membership_remove",
        lead.id,
        ada.id,
        "ada@example.com",
        {"role": "member"},
    )
    await service.remove(admin, team, lead.id, by_admin=True)
    assert world.last().detail == {"role": "manager"} and team.version == 3
    assert await world.teams.members([team.id]) == []


# ── Invitations into a team ─────────────────────────────────────────────────


async def test_an_address_with_no_account_is_invited_into_the_team_in_the_inviter_s_language(
    world: TeamsWorld,
) -> None:
    admin = world.person("root@example.com", role="ADMIN", locale="fr")
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    sent = await service.add(admin, team, "New@Example.com", MANAGER, by_admin=True)
    assert sent == TeamInvitationSent(email="new@example.com", sent=True, link=None)
    (invitation,) = world.invitations.rows.values()
    assert (invitation.email, invitation.role, invitation.locale) == ("new@example.com", "MEMBER", "fr")
    assert invitation.scope == {"team_id": team.id, "team_role": "manager"}
    assert team.version == 2, "an invitation into the team moves its version"
    row = world.last()
    assert (row.action, row.target_email, row.target_group_id) == ("invite", "new@example.com", team.id)
    assert row.detail == {"invitation_id": invitation.id, "role": "MEMBER", "team_role": "manager", "sent": True}
    (mail,) = world.mailer.sent
    assert mail.to == "new@example.com" and "Rig" in mail.message.text, "the mail names the team"


async def test_an_invitation_goes_out_in_the_language_picked_and_shows_its_link_on_the_console(
    world: TeamsWorld,
) -> None:
    admin = world.person("root@example.com", role="ADMIN", locale="fr")
    service = world.teams_service(show_link=True)
    team = await world.teams.add(NewTeam("Rig"))
    sent = await service.add(admin, team, "new@example.com", MEMBER, by_admin=True, locale="it")
    assert isinstance(sent, TeamInvitationSent) and sent.link is not None and "/register?invite=" in sent.link
    assert next(iter(world.invitations.rows.values())).locale == "it"


async def test_inviting_an_address_again_sends_its_open_invitation_again_in_the_role_asked(
    world: TeamsWorld,
) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team, other = await world.teams.add(NewTeam("Rig")), await world.teams.add(NewTeam("Bench"))
    await service.add(admin, team, "new@example.com", MEMBER, by_admin=True)
    await service.add(admin, other, "new@example.com", MEMBER, by_admin=True)
    (first, elsewhere) = world.invitations.rows.values()
    digest = first.token_hash
    world.clock.advance(minutes=1)
    await service.add(admin, team, "new@example.com", MEMBER, by_admin=True)
    assert len(world.invitations.rows) == 2 and first.token_hash != digest, "the same row, a new link"
    assert world.last().action == "invite_resend" and team.version == 3
    await service.add(admin, team, "new@example.com", MANAGER, by_admin=True)
    assert first.scope == {"team_id": team.id, "team_role": "manager"}
    assert elsewhere.scope == {"team_id": other.id, "team_role": "member"}


async def test_a_team_s_invitation_is_found_only_through_its_own_team(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team, other = await world.teams.add(NewTeam("Rig")), await world.teams.add(NewTeam("Bench"))
    await service.add(admin, team, "new@example.com", MEMBER, by_admin=True)
    (invitation,) = world.invitations.rows.values()
    assert await service.invitation(team, invitation.id) is invitation
    for asked_team, invitation_id in ((other, invitation.id), (team, 999)):
        with pytest.raises(TeamError) as missing:
            await service.invitation(asked_team, invitation_id)
        assert _code(missing) == (TeamErrorCode.TEAM_NOT_FOUND, 404, {})
        assert str(missing.value) == "Invitation not found"


async def test_a_team_s_invitation_is_sent_again_and_withdrawn_through_the_account_layer(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    team = await world.teams.add(NewTeam("Rig"))
    await service.add(admin, team, "new@example.com", MEMBER, by_admin=True)
    (invitation,) = world.invitations.rows.values()
    again = await service.resend(admin, invitation)
    assert again == TeamInvitationSent(email="new@example.com", sent=True, link=None)
    assert world.last().action == "invite_resend" and world.last().target_group_id == team.id
    await service.withdraw(admin, invitation)
    assert invitation.revoked_at == world.clock(), "a withdrawal is stamped and kept"
    assert world.last().action == "invite_revoke"
    with pytest.raises(AuthError) as closed:
        await service.resend(admin, invitation)
    assert closed.value.code is AuthErrorCode.INVITATION_CLOSED
    assert team.version == 2, "a re-send and a withdrawal leave the version alone"


async def test_the_billing_gate_asks_about_the_manager_s_add_and_re_send(world: TeamsWorld) -> None:
    lead = world.person("lead@example.com")
    asked: list[tuple[int, str]] = []

    async def billing_gate(user: MemoryUser, what: str) -> None:
        asked.append((user.id, what))

    await world.teams_service().gate(lead, "add")
    world.team_hooks = TeamHooks(billing_gate=billing_gate)
    service = world.teams_service()
    await service.gate(lead, "add")
    await service.gate(lead, "resend")
    assert asked == [(lead.id, "add"), (lead.id, "resend")]


# ── What each one sees ──────────────────────────────────────────────────────


async def test_the_system_team_is_nobody_s_to_reach(world: TeamsWorld) -> None:
    lead = world.person("lead@example.com")
    service = world.teams_service()
    system = await world.teams.add(NewTeam("Demo", is_system=True))
    await world.teams.add_member(system.id, lead.id, MANAGER)
    for team_id in (system.id, 999):
        with pytest.raises(TeamError) as missing:
            await service.get(team_id)
        assert _code(missing) == (TeamErrorCode.TEAM_NOT_FOUND, 404, {})
        with pytest.raises(TeamError):
            await service.managed(lead, team_id)
    assert await service.teams_of(lead) == [] and await service.all_teams() == []
    team = await world.teams.add(NewTeam("Rig"))
    assert await service.get(team.id) is team


async def test_a_manager_s_reach_ends_at_the_teams_they_manage(world: TeamsWorld) -> None:
    lead = world.person("lead@example.com")
    service = world.teams_service()
    managed, member_of = await world.teams.add(NewTeam("Rig")), await world.teams.add(NewTeam("Bench"))
    await world.teams.add_member(managed.id, lead.id, MANAGER)
    await world.teams.add_member(member_of.id, lead.id, MEMBER)
    assert await service.managed(lead, managed.id) is managed
    with pytest.raises(TeamError) as missing:
        await service.managed(lead, member_of.id)
    assert _code(missing) == (TeamErrorCode.TEAM_NOT_FOUND, 404, {}) and str(missing.value) == "Team not found"


async def test_a_member_sees_their_teams_by_name_and_only_a_manager_the_invitations(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    lead = world.person("lead@example.com", first_name="Lea", last_name="Zed")
    ada = world.person("ada@example.com", first_name="Ada", last_name="Byron")
    service = world.teams_service()
    rig, bench = await world.teams.add(NewTeam("Rig")), await world.teams.add(NewTeam("Bench"))
    await world.teams.add_member(rig.id, lead.id, MANAGER)
    await world.teams.add_member(rig.id, ada.id, MEMBER)
    await world.teams.add_member(bench.id, lead.id, MEMBER)
    await service.add(admin, rig, "new@example.com", MEMBER, by_admin=True)
    await service.add(admin, bench, "else@example.com", MEMBER, by_admin=True)
    views = await service.teams_of(lead)
    assert [(view.team.name, view.my_role) for view in views] == [("Bench", MEMBER), ("Rig", MANAGER)]
    assert [entry.user.id for entry in views[1].members] == [ada.id, lead.id], "by last name"
    assert [row.email for row in views[1].invitations] == ["new@example.com"]
    assert views[0].invitations == (), "a member is not shown who is waiting"
    assert await service.teams_of(world.person("none@example.com")) == []
    one = await service.view(rig, MANAGER)
    assert (one.my_role, [row.email for row in one.invitations]) == (MANAGER, ["new@example.com"])


async def test_the_admin_sees_every_team_with_its_grants_and_its_invitations(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")

    async def deleting(team_ids: Sequence[int]) -> dict[int, int]:
        return {team_id: team_id * 10 for team_id in team_ids}

    world.team_hooks = TeamHooks(deleting=deleting)
    service = world.teams_service()
    rig, bench = await world.teams.add(NewTeam("Rig")), await world.teams.add(NewTeam("Bench"))
    await world.teams.add(NewTeam("Demo", is_system=True))
    await service.add(admin, rig, "new@example.com", MEMBER, by_admin=True)
    views = await service.all_teams()
    assert [(view.team.name, view.my_role, view.grants) for view in views] == [
        ("Bench", None, bench.id * 10),
        ("Rig", None, rig.id * 10),
    ]
    assert [row.email for row in views[1].invitations] == ["new@example.com"]


# ── Joining by invitation, and the demo ─────────────────────────────────────


def _invite(world: TeamsWorld, email: str, scope: dict[str, Any] | None) -> Any:
    return world.invitations.seed(email=email, scope=scope, token_hash=f"digest-{email}")


async def test_an_invitation_taken_up_joins_its_team_as_the_invitee(world: TeamsWorld) -> None:
    team = await world.teams.add(NewTeam("Rig"))
    ada = world.person("ada@example.com")
    invitation = _invite(world, ada.email, team_scope(team.id, MANAGER))
    assert await join_team(world.teams, world.admin_log, invitation, ada, now=world.clock)
    member = await world.teams.membership(team.id, ada.id)
    assert member is not None and member.role is MANAGER and team.version == 2
    row = world.last()
    assert (row.action, row.actor_id, row.target_user_id, row.target_group_id, row.detail) == (
        "membership_add",
        ada.id,
        ada.id,
        team.id,
        {"role": "manager"},
    )
    assert not await join_team(world.teams, world.admin_log, invitation, ada), "already in: nothing"
    assert (team.version, world.actions()) == (2, ["membership_add"])


async def test_an_invitation_with_no_team_or_a_team_gone_joins_nothing(world: TeamsWorld) -> None:
    ada = world.person("ada@example.com")
    for scope in (None, team_scope(999, MEMBER)):
        assert not await join_team(world.teams, world.admin_log, _invite(world, ada.email, scope), ada)
    assert world.actions() == []


async def test_the_ready_account_hooks_join_at_a_sign_up_and_at_a_redemption(world: TeamsWorld) -> None:
    team = await world.teams.add(NewTeam("Rig"))
    hooks = world.account_hooks
    ada, bob = world.person("ada@example.com"), world.person("bob@example.com")
    body = RegisterRequest(email=ada.email, password="correct horse", first_name="Ada", last_name="L", locale="en")
    await hooks.on_register(ada, None, body)
    await hooks.on_register(ada, _invite(world, ada.email, team_scope(team.id, MEMBER)), body)
    await hooks.on_invitation_redeemed(bob, _invite(world, bob.email, team_scope(team.id, MEMBER)))
    assert {entry.user.id for entry in await world.teams.members([team.id])} == {ada.id, bob.id}


async def test_the_ready_account_hooks_name_an_invitation_s_team(world: TeamsWorld) -> None:
    team = await world.teams.add(NewTeam("Rig"))
    system = await world.teams.add(NewTeam("Demo", is_system=True))
    admin = world.person("root@example.com", role="ADMIN")
    hooks = world.account_hooks
    named = _invite(world, "new@example.com", team_scope(team.id, MANAGER))
    unnamed = _invite(world, "else@example.com", None)
    gone = _invite(world, "gone@example.com", team_scope(999, MEMBER))
    assert await hooks.invitation_group(named) == GroupRef(id=team.id, name="Rig")
    assert await hooks.invitation_group(unnamed) is None and await hooks.invitation_group(gone) is None
    assert await hooks.describe_invitation(named) == {"scope": team.id, "team_name": "Rig", "team_role": "manager"}
    assert await hooks.describe_invitation(unnamed) == {"scope": None, "team_name": None, "team_role": None}
    assert await hooks.describe_invitation(gone) == {"scope": 999, "team_name": None, "team_role": "member"}
    assert await hooks.invitation_scope(admin, team.id) == {"team_id": team.id, "team_role": "member"}
    assert await hooks.invitation_scope(admin, str(team.id)) == {"team_id": team.id, "team_role": "member"}
    for scope in (system.id, 999, "rig"):
        with pytest.raises(AuthError) as invalid:
            await hooks.invitation_scope(admin, scope)
        assert invalid.value.code is AuthErrorCode.INVITATION_SCOPE_INVALID


async def test_the_log_names_a_live_team_and_a_deleted_one_by_its_stamp(world: TeamsWorld) -> None:
    admin = world.person("root@example.com", role="ADMIN")
    service = world.teams_service()
    live, gone = await service.create(admin, "Rig"), await service.create(admin, "Bench")
    await service.delete(admin, gone, acknowledged=True)
    assert await world.account_hooks.group_names([live.id, gone.id, 999]) == {live.id: "Rig", gone.id: "Bench"}
    assert await world.account_hooks.group_names([live.id]) == {live.id: "Rig"}

    class PlainLog:
        async def add(self, record: Any) -> Any:  # pragma: no cover - never written
            raise AssertionError(record)

    plain: TeamAccountHooks[Any] = TeamAccountHooks(world.teams, PlainLog())  # type: ignore[arg-type]
    assert await plain.group_names([live.id, gone.id]) == {live.id: "Rig"}, "without the stamps: the live names"


async def test_a_demo_joins_the_system_team_with_no_row_and_no_notice(world: TeamsWorld) -> None:
    demo = world.person("demo+1@demo.example")
    system = await world.teams.add(NewTeam("Demo", is_system=True))
    assert await add_system_member(world.teams, system, demo)
    assert not await add_system_member(world.teams, system, demo)
    member = await world.teams.membership(system.id, demo.id)
    assert member is not None and member.role is MEMBER
    assert (system.version, world.actions()) == (1, [])
    team = await world.teams.add(NewTeam("Rig"))
    with pytest.raises(ValueError, match="system team"):
        await add_system_member(world.teams, team, demo)


async def test_an_invitation_into_a_team_reads_its_team_and_role_from_a_fresh_row(world: TeamsWorld) -> None:
    """The same scope the kit's own route stores: an admin's ``POST /admin/invitations`` with a
    team's id, through the ready ``invitation_scope``."""
    team = await world.teams.add(NewTeam("Rig"))
    added = await world.invitations.add(
        NewInvitation(
            email="new@example.com",
            token_hash="d" * 64,
            expires_at=world.clock() + INVITE_TTL,
            locale="en",
            invited_by_user_id=None,
            scope=await world.account_hooks.invitation_scope(world.person("root@example.com"), team.id),
        )
    )
    assert await world.account_hooks.invitation_group(added) == GroupRef(id=team.id, name="Rig")
