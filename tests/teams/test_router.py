"""The teams' routes as an app mounts them: ``/teams`` (a member's and a manager's) and
``/admin/teams`` (an admin's), Kurvenschmiede's paths, bodies, answers and refusals; the joins
by invitation through the account routes; and the admin log naming a team.

No ``from __future__ import annotations``: FastAPI reads the dependencies below.
"""

from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends

from eifi1_server_kit.auth.router import current_user
from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.teams import NewTeam, TeamRole
from eifi1_server_kit.teams.router import TeamsDeps, if_match_version, no_team_hooks, teams_admin_router
from eifi1_server_kit.teams.service import TeamHooks
from eifi1_server_kit.user_admin import AccountError, AccountErrorCode
from tests.auth._world import PASSWORD
from tests.auth_fakes import MemoryUser
from tests.teams._teams_world import TeamsWorld

MEMBER, MANAGER = TeamRole.MEMBER, TeamRole.MANAGER


def _refusal(response: httpx.Response) -> tuple[int, Any]:
    return response.status_code, response.json().get("code")


async def _team(world: TeamsWorld, name: str = "Rig", **members: TeamRole) -> Any:
    """A team with ``members`` by address (``ada="manager"``), the accounts made as needed."""
    team = await world.teams.add(NewTeam(name))
    for local, role in members.items():
        email = f"{local}@example.com"
        user = await world.users.by_email(email) or world.person(email)
        await world.teams.add_member(team.id, user.id, role)
    return team


async def _token(world: TeamsWorld, email: str, **values: Any) -> tuple[MemoryUser, str]:
    user = await world.users.by_email(email) or world.person(email, **values)
    return user, await world.token_of(user)


# ── A member's and a manager's /teams ───────────────────────────────────────


async def test_a_member_sees_their_teams_as_kurvenschmiede_answers_them(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    ada = world.person("ada@example.com", first_name="Ada", last_name="Lovelace", is_active=False)
    await world.teams.add_member(team.id, ada.id, MEMBER)
    reader, token = await _token(world, "zsofia@example.com", locale="hu", first_name="Zsófia", last_name="Kiss")
    await world.teams.add_member(team.id, reader.id, MEMBER)
    await _team(world, "Elsewhere", lead=MANAGER)
    response = await world.call("GET", "/teams", token=token)
    assert response.status_code == 200
    (answer,) = response.json()
    assert answer == {
        "id": team.id,
        "name": "Rig",
        "version": 1,
        "created_at": answer["created_at"],
        "my_role": "member",
        "members": [
            {
                "user_id": reader.id,
                "first_name": "Zsófia",
                "last_name": "Kiss",
                "display_name": "Kiss Zsófia",
                "email": "zsofia@example.com",
                "role": "member",
                "is_active": True,
            },
            {
                "user_id": world.users.rows[1].id,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "display_name": "Lovelace Ada",
                "email": "lead@example.com",
                "role": "manager",
                "is_active": True,
            },
            {
                "user_id": ada.id,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "display_name": "Lovelace Ada",
                "email": "ada@example.com",
                "role": "member",
                "is_active": False,
            },
        ],
        "invitations": [],
        "invited": None,
    }
    assert (await world.client.get("/api/v1/teams")).status_code == 401


async def test_a_manager_adds_a_member_and_the_answer_is_the_whole_team(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    world.person("ada@example.com")
    _lead, token = await _token(world, "lead@example.com")
    response = await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "ADA@example.com"})
    assert response.status_code == 200
    answer = response.json()
    assert (answer["version"], answer["my_role"], answer["invited"]) == (2, "manager", None)
    assert sorted(member["email"] for member in answer["members"]) == ["ada@example.com", "lead@example.com"]


async def test_a_manager_s_add_checks_the_version_sent_and_none_is_never_demanded(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    world.person("ada@example.com")
    world.person("bob@example.com")
    _lead, token = await _token(world, "lead@example.com")
    path = f"/teams/{team.id}/members"
    stale = await world.call("POST", path, token=token, json={"email": "ada@example.com"}, headers={"If-Match": '"2"'})
    assert stale.status_code == 409 and stale.json() == {
        "detail": "The team Rig was changed after you opened it. Reload to see the change.",
        "code": "team_changed",
        "name": "Rig",
    }
    weak = await world.call("POST", path, token=token, json={"email": "ada@example.com"}, headers={"If-Match": 'W/"1"'})
    assert weak.status_code == 200 and weak.json()["version"] == 2
    bare = await world.call("POST", path, token=token, json={"email": "bob@example.com"}, headers={"If-Match": "2"})
    assert bare.status_code == 200 and bare.json()["version"] == 3
    garbled = await world.call("POST", path, token=token, json={"email": "bob@example.com"}, headers={"If-Match": "x"})
    assert _refusal(garbled) == (409, "team_changed")


def test_an_if_match_is_read_in_its_three_spellings() -> None:
    assert [if_match_version(value) for value in (None, "3", '"3"', 'W/"3"', ' W/"3" ', "three")] == [
        None,
        3,
        3,
        3,
        3,
        0,
    ]


async def test_a_manager_s_reach_ends_at_the_teams_they_manage(world: TeamsWorld) -> None:
    team = await _team(world, lead=MEMBER, ada=MEMBER)
    _lead, token = await _token(world, "lead@example.com")
    for method, path, body in (
        ("POST", f"/teams/{team.id}/members", {"email": "bob@example.com"}),
        ("DELETE", f"/teams/{team.id}/members/2", None),
        ("POST", f"/teams/{team.id}/invitations/1/resend", None),
        ("DELETE", f"/teams/{team.id}/invitations/1", None),
        ("POST", "/teams/999/members", {"email": "bob@example.com"}),
    ):
        response = await world.call(method, path, token=token, json=body)
        assert response.status_code == 404 and response.json() == {"detail": "Team not found", "code": "team_not_found"}


async def test_a_manager_never_names_or_changes_a_manager(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER, other=MANAGER)
    _lead, token = await _token(world, "lead@example.com")
    path = f"/teams/{team.id}/members"
    named = await world.call("POST", path, token=token, json={"email": "ada@example.com", "role": "manager"})
    assert _refusal(named) == (409, "manager_named_by_admin")
    kept = await world.call("POST", path, token=token, json={"email": "other@example.com"})
    assert _refusal(kept) == (409, "manager_kept_by_admin") and kept.json()["email"] == "other@example.com"
    other = await world.users.by_email("other@example.com")
    assert other is not None
    removed = await world.call("DELETE", f"{path}/{other.id}", token=token)
    assert _refusal(removed) == (409, "manager_removed_by_admin")


async def test_a_manager_removes_a_member_against_the_version_read(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER, ada=MEMBER)
    _lead, token = await _token(world, "lead@example.com")
    ada = await world.users.by_email("ada@example.com")
    assert ada is not None
    path = f"/teams/{team.id}/members/{ada.id}"
    assert _refusal(await world.call("DELETE", path, token=token, headers={"If-Match": '"7"'})) == (409, "team_changed")
    response = await world.call("DELETE", path, token=token, headers={"If-Match": '"1"'})
    assert response.status_code == 200
    assert [member["email"] for member in response.json()["members"]] == ["lead@example.com"]
    assert _refusal(await world.call("DELETE", path, token=token)) == (404, "team_not_found")


async def test_a_manager_invites_an_address_with_no_account_and_sees_it_waiting(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    _lead, token = await _token(world, "lead@example.com", locale="fr")
    body = {"email": "new@example.com", "locale": "it"}
    response = await world.call("POST", f"/teams/{team.id}/members", token=token, json=body)
    assert response.status_code == 200
    answer = response.json()
    assert answer["invited"] == {"email": "new@example.com", "sent": True, "link": None}
    (waiting,) = answer["invitations"]
    assert {key: waiting[key] for key in ("email", "role", "status")} == {
        "email": "new@example.com",
        "role": "member",
        "status": "open",
    }
    assert answer["version"] == 2
    invitation = world.invitations.rows[waiting["id"]]
    assert (invitation.locale, invitation.scope) == ("it", {"team_id": team.id, "team_role": "member"})
    unknown = await world.call("POST", f"/teams/{team.id}/members", token=token, json={**body, "locale": "xx-YY-Z"})
    assert unknown.status_code == 422
    world.clock.advance(days=15)
    (listed,) = (await world.call("GET", "/teams", token=await world.token_of(_lead))).json()
    assert listed["invitations"][0]["status"] == "expired"


async def test_on_the_console_the_answer_shows_the_link_once() -> None:
    world = TeamsWorld(mail_backend="console")
    async with world.client:
        team = await _team(world, lead=MANAGER)
        _lead, token = await _token(world, "lead@example.com")
        response = await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "new@example.com"})
        link = response.json()["invited"]["link"]
        assert link is not None and "/register?invite=" in link
        listed = await world.call("GET", "/teams", token=token)
        assert "link" not in listed.json()[0]["invitations"][0]


async def test_a_manager_sends_a_team_invitation_again_and_withdraws_it(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    other = await _team(world, "Bench")
    _lead, token = await _token(world, "lead@example.com")
    await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "new@example.com"})
    (invitation,) = world.invitations.rows.values()
    resent = await world.call("POST", f"/teams/{team.id}/invitations/{invitation.id}/resend", token=token)
    assert resent.status_code == 200 and resent.json()["invited"]["email"] == "new@example.com"
    assert _refusal(await world.call("POST", f"/teams/{other.id}/invitations/{invitation.id}/resend", token=token)) == (
        404,
        "team_not_found",
    )
    withdrawn = await world.call("DELETE", f"/teams/{team.id}/invitations/{invitation.id}", token=token)
    assert withdrawn.status_code == 200 and withdrawn.json()["invitations"] == []
    assert invitation.revoked_at is not None
    again = await world.call("POST", f"/teams/{team.id}/invitations/{invitation.id}/resend", token=token)
    assert _refusal(again) == (409, "invitation_closed")
    missing = await world.call("DELETE", f"/teams/{team.id}/invitations/999", token=token)
    assert missing.json() == {"detail": "Invitation not found", "code": "team_not_found"}


async def test_the_billing_gate_stops_a_manager_s_add_and_re_send_but_never_an_admin_s(world: TeamsWorld) -> None:
    asked: list[str] = []

    async def billing_gate(_user: MemoryUser, what: str) -> None:
        asked.append(what)
        raise AccountError(AccountErrorCode.ACCOUNT_INACTIVE, "Your plan does not allow this.")

    team = await _team(world, lead=MANAGER, ada=MEMBER)
    _lead, token = await _token(world, "lead@example.com")
    _admin, admin_token = await _token(world, "root@example.com", role="ADMIN")
    await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "new@example.com"})
    (invitation,) = world.invitations.rows.values()
    world.team_hooks = TeamHooks(billing_gate=billing_gate)
    add = await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "bob@example.com"})
    resend = await world.call("POST", f"/teams/{team.id}/invitations/{invitation.id}/resend", token=token)
    assert _refusal(add) == _refusal(resend) == (409, "account_inactive")
    withdraw = await world.call("DELETE", f"/teams/{team.id}/invitations/{invitation.id}", token=token)
    ada = await world.users.by_email("ada@example.com")
    assert ada is not None
    remove = await world.call("DELETE", f"/teams/{team.id}/members/{ada.id}", token=token)
    admin_add = await world.call(
        "POST", f"/admin/teams/{team.id}/members", token=admin_token, json={"email": "bob@example.com"}
    )
    assert withdraw.status_code == remove.status_code == admin_add.status_code == 200
    assert asked == ["add", "resend"]


async def test_the_app_s_demo_guard_keeps_a_demo_from_every_change_but_not_from_its_list() -> None:
    world: TeamsWorld
    guarded: list[str] = []

    def not_in_the_demo(what: str) -> Any:
        async def check(user: Annotated[Any, Depends(current_user(world.deps))]) -> None:
            guarded.append(what)
            refuse_demo(getattr(user, "is_demo", False), what)

        return check

    world = TeamsWorld(cutoff=True, demo_guard=not_in_the_demo)
    async with world.client:
        team = await _team(world, lead=MANAGER)
        lead = await world.users.by_email("lead@example.com")
        assert lead is not None
        lead.is_demo = True  # type: ignore[attr-defined]
        token = await world.token_of(lead)
        assert (await world.call("GET", "/teams", token=token)).status_code == 200
        for method, path in (
            ("POST", f"/teams/{team.id}/members"),
            ("DELETE", f"/teams/{team.id}/members/1"),
            ("POST", f"/teams/{team.id}/invitations/1/resend"),
            ("DELETE", f"/teams/{team.id}/invitations/1"),
        ):
            response = await world.call(method, path, token=token, json={"email": "ada@example.com"})
            assert _refusal(response) == (403, "demo_refused"), path
        assert set(guarded) == {"teams"}


# ── The admin's /admin/teams ────────────────────────────────────────────────


async def test_only_an_admin_reaches_the_admin_s_teams(world: TeamsWorld) -> None:
    _member, token = await _token(world, "ada@example.com")
    assert _refusal(await world.call("GET", "/admin/teams", token=token)) == (403, "admin_required")
    assert (await world.client.get("/api/v1/admin/teams")).status_code == 401


async def test_the_admin_lists_every_team_with_its_grants_and_the_levels(world: TeamsWorld) -> None:
    async def deleting(team_ids: Any) -> dict[int, int]:
        return dict.fromkeys(team_ids, 3)

    world.team_hooks = TeamHooks(deleting=deleting)
    team = await _team(world, lead=MANAGER)
    await world.teams.add(NewTeam("Demo", is_system=True))
    admin, token = await _token(world, "root@example.com", role="ADMIN")
    await world.teams.add_member(team.id, admin.id, MEMBER)
    response = await world.call("GET", "/admin/teams", token=token)
    assert response.status_code == 200
    answer = response.json()
    (listed,) = answer["teams"]
    assert (listed["name"], listed["my_role"], listed["grant_count"]) == ("Rig", None, 3)
    assert answer["levels"]["group_delete"] == "acknowledge"
    assert answer["levels"]["membership_add"] == "none"


async def test_the_admin_creates_renames_and_deletes_a_team(world: TeamsWorld) -> None:
    _admin, token = await _token(world, "root@example.com", role="ADMIN")
    created = await world.call("POST", "/admin/teams", token=token, json={"name": "  Rig "})
    assert created.status_code == 201
    team = created.json()
    assert (team["name"], team["version"], team["my_role"], team["members"]) == ("Rig", 1, None, [])
    assert "grant_count" not in team
    taken = await world.call("POST", "/admin/teams", token=token, json={"name": "Rig"})
    assert _refusal(taken) == (409, "team_name_taken") and taken.json()["name"] == "Rig"
    assert (await world.call("POST", "/admin/teams", token=token, json={"name": " "})).status_code == 422
    path = f"/admin/teams/{team['id']}"
    stale = await world.call("PATCH", path, token=token, json={"name": "Bench"}, headers={"If-Match": '"5"'})
    assert _refusal(stale) == (409, "team_changed")
    renamed = await world.call("PATCH", path, token=token, json={"name": "Bench"}, headers={"If-Match": '"1"'})
    assert renamed.status_code == 200 and (renamed.json()["name"], renamed.json()["version"]) == ("Bench", 2)
    assert (await world.call("PATCH", path, token=token, json={"name": "Test"})).json()["version"] == 3
    dropped = await world.call("DELETE", path, token=token)
    assert _refusal(dropped) == (409, "confirmation_required")
    unticked = await world.call("DELETE", path, token=token, json={"acknowledged": False})
    assert _refusal(unticked) == (409, "confirmation_required")
    deleted = await world.call("DELETE", path, token=token, json={"acknowledged": True})
    assert deleted.status_code == 204 and deleted.content == b""
    assert await world.teams.get(team["id"]) is None


async def test_the_admin_puts_anybody_in_names_a_manager_and_takes_them_out(world: TeamsWorld) -> None:
    team = await _team(world)
    ada = world.person("ada@example.com")
    _admin, token = await _token(world, "root@example.com", role="ADMIN")
    path = f"/admin/teams/{team.id}/members"
    added = await world.call("POST", path, token=token, json={"email": "ada@example.com", "role": "manager"})
    assert added.status_code == 200 and added.json()["members"][0]["role"] == "manager"
    invited = await world.call("POST", path, token=token, json={"email": "new@example.com", "role": "manager"})
    assert invited.json()["invited"]["email"] == "new@example.com"
    assert invited.json()["invitations"][0]["role"] == "manager"
    demoted = await world.call("PATCH", f"{path}/{ada.id}", token=token, json={"role": "member"})
    assert demoted.status_code == 200 and demoted.json()["members"][0]["role"] == "member"
    assert _refusal(await world.call("PATCH", f"{path}/999", token=token, json={"role": "member"})) == (
        404,
        "team_not_found",
    )
    removed = await world.call("DELETE", f"{path}/{ada.id}", token=token)
    assert removed.status_code == 200 and removed.json()["members"] == []


async def test_the_system_team_is_a_404_on_every_admin_route(world: TeamsWorld) -> None:
    system = await world.teams.add(NewTeam("Demo", is_system=True))
    _admin, token = await _token(world, "root@example.com", role="ADMIN")
    path = f"/admin/teams/{system.id}"
    for method, route, body in (
        ("PATCH", path, {"name": "Mine"}),
        ("DELETE", path, {"acknowledged": True}),
        ("POST", f"{path}/members", {"email": "ada@example.com"}),
        ("PATCH", f"{path}/members/1", {"role": "manager"}),
        ("DELETE", f"{path}/members/1", None),
    ):
        assert _refusal(await world.call(method, route, token=token, json=body)) == (404, "team_not_found"), route


# ── Joining by invitation, through the account routes ───────────────────────


async def test_a_sign_up_through_a_team_invitation_joins_the_team(world: TeamsWorld) -> None:
    team = await _team(world, lead=MANAGER)
    _lead, token = await _token(world, "lead@example.com")
    await world.call("POST", f"/teams/{team.id}/members", token=token, json={"email": "new@example.com"})
    invite = world.mailer.token("invitation")
    body = {
        "email": "new@example.com",
        "password": PASSWORD,
        "first_name": "New",
        "last_name": "Person",
        "locale": "en",
        "invite_token": invite,
    }
    assert (await world.call("POST", "/auth/register", json=body)).status_code in (200, 201)
    newcomer = await world.users.by_email("new@example.com")
    assert newcomer is not None
    member = await world.teams.membership(team.id, newcomer.id)
    assert member is not None and member.role is MEMBER
    row = world.last()
    assert (row.action, row.actor_id, row.target_group_id) == ("membership_add", newcomer.id, team.id)
    (listed,) = (await world.call("GET", "/teams", token=token)).json()
    assert listed["invitations"] == [] and listed["version"] == 3


async def test_an_existing_account_accepts_a_team_invitation_made_on_the_admin_s_page(world: TeamsWorld) -> None:
    """The kit's own ``POST /admin/invitations`` with a team's id as ``scope``, through the ready
    ``invitation_scope``: the row names the team, the mail too, and taking it up joins it."""
    team = await _team(world)
    _admin, admin_token = await _token(world, "root@example.com", role="ADMIN")
    body = {"email": "ada@example.com", "role": "MEMBER", "scope": team.id}
    created = await world.call("POST", "/admin/invitations", token=admin_token, json=body)
    assert created.status_code == 201
    assert created.json()["scope"] == team.id
    assert world.last().target_group_id == team.id
    ada, token = await _token(world, "ada@example.com")
    accepted = await world.call(
        "POST", "/auth/invitations/accept", token=token, json={"token": world.mailer.token("invitation")}
    )
    assert accepted.status_code == 200
    assert accepted.json()["group"] == {"id": team.id, "name": "Rig"}
    assert await world.teams.membership(team.id, ada.id) is not None


async def test_the_admin_log_names_a_team_and_a_deleted_one_by_the_name_it_had(world: TeamsWorld) -> None:
    _admin, token = await _token(world, "root@example.com", role="ADMIN")
    kept = (await world.call("POST", "/admin/teams", token=token, json={"name": "Rig"})).json()
    gone = (await world.call("POST", "/admin/teams", token=token, json={"name": "Bench"})).json()
    await world.call("DELETE", f"/admin/teams/{gone['id']}", token=token, json={"acknowledged": True})
    page = (await world.call("GET", "/admin/actions", token=token)).json()
    named = {(item["action"], (item.get("target_group") or {}).get("name")) for item in page["actions"]}
    assert {("group_create", "Rig"), ("group_create", "Bench"), ("group_delete", "Bench")} <= named
    assert kept["id"] != gone["id"]


@pytest.mark.parametrize("prefix", ["/teams", "/admin/teams"])
async def test_the_routes_are_described_with_their_bearer(world: TeamsWorld, prefix: str) -> None:
    schema = world.app.openapi()
    paths = [path for path in schema["paths"] if path.startswith(f"/api/v1{prefix}")]
    assert paths
    for path in paths:
        for operation in schema["paths"][path].values():
            assert operation.get("security"), (path, operation.get("operationId"))


async def test_without_hooks_an_app_s_teams_hold_no_grants() -> None:
    """``TeamsDeps.hooks`` left out: ``no_team_hooks``, every hook absent."""
    world = TeamsWorld()
    async with world.client:
        assert await no_team_hooks() == TeamHooks()
        plain = TeamsDeps(auth=world.deps, teams=lambda: world.teams)
        world.app.include_router(teams_admin_router(plain, prefix="/plain/teams"), prefix="/api/v1")
        await _team(world)
        _admin, token = await _token(world, "root@example.com", role="ADMIN")
        listed = await world.call("GET", "/plain/teams", token=token)
        assert [team["grant_count"] for team in listed.json()["teams"]] == [0]
