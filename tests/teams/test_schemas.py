"""A team's wire shapes, field for field Kurvenschmiede's (``domain/schemas/teams.py``), so the
app adopts them with no change on the wire."""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from eifi1_server_kit.teams import (
    AdminTeamOut,
    AdminTeamsResponse,
    TeamIn,
    TeamInvitationOut,
    TeamInvited,
    TeamMemberIn,
    TeamMemberOut,
    TeamMemberRoleIn,
    TeamOut,
    TeamRole,
)

AT = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _fields(model: type[BaseModel]) -> list[str]:
    return list(model.model_fields)


def test_the_two_roles_are_kurvenschmiedes_values() -> None:
    assert [role.value for role in TeamRole] == ["member", "manager"]
    assert str(TeamRole.MANAGER) == "manager"


def test_the_answers_carry_kurvenschmiedes_fields_in_its_order() -> None:
    assert _fields(TeamMemberOut) == [
        "user_id",
        "first_name",
        "last_name",
        "display_name",
        "email",
        "role",
        "is_active",
    ]
    assert _fields(TeamInvitationOut) == ["id", "email", "role", "status", "expires_at"]
    assert _fields(TeamInvited) == ["email", "sent", "link"]
    assert _fields(TeamOut) == ["id", "name", "version", "created_at", "my_role", "members", "invitations", "invited"]
    assert _fields(AdminTeamOut) == [*_fields(TeamOut), "grant_count"]
    assert _fields(AdminTeamsResponse) == ["teams", "levels"]


def test_a_team_answers_as_kurvenschmiedes_json() -> None:
    team = TeamOut(
        id=3,
        name="Rig",
        version=2,
        created_at=AT,
        my_role=TeamRole.MANAGER,
        members=[
            TeamMemberOut(
                user_id=7,
                first_name="Ada",
                last_name="Lovelace",
                display_name="Ada Lovelace",
                email="ada@example.com",
                role=TeamRole.MEMBER,
                is_active=True,
            )
        ],
    )
    assert team.model_dump(mode="json") == {
        "id": 3,
        "name": "Rig",
        "version": 2,
        "created_at": "2026-11-14T09:00:00Z",
        "my_role": "manager",
        "members": [
            {
                "user_id": 7,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "display_name": "Ada Lovelace",
                "email": "ada@example.com",
                "role": "member",
                "is_active": True,
            }
        ],
        "invitations": [],
        "invited": None,
    }
    listed = AdminTeamsResponse(teams=[AdminTeamOut(**team.model_dump(), grant_count=4)])
    assert listed.model_dump(mode="json")["teams"][0]["grant_count"] == 4
    assert listed.levels == {}


def test_a_name_is_kept_stripped_and_a_blank_one_is_refused() -> None:
    assert TeamIn(name="  Rig  ").name == "Rig"
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            TeamIn(name=blank)
    with pytest.raises(ValidationError):
        TeamIn(name="x" * 121)
    with pytest.raises(ValidationError):
        TeamIn.model_validate({"name": "Rig", "colour": "red"})


def test_an_add_is_by_exact_address_as_a_member_unless_it_says_otherwise() -> None:
    body = TeamMemberIn(email="  Ada@Example.COM ")
    assert (body.email, body.role, body.locale) == ("ada@example.com", TeamRole.MEMBER, None)
    assert TeamMemberIn(email="ada@example.com", role="manager", locale="fr").role is TeamRole.MANAGER
    with pytest.raises(ValidationError):
        TeamMemberIn(email="not an address")
    with pytest.raises(ValidationError):
        TeamMemberIn.model_validate({"email": "ada@example.com", "role": "owner"})
    with pytest.raises(ValidationError):
        TeamMemberIn.model_validate({"email": "ada@example.com", "note": "x"})
    with pytest.raises(ValidationError):
        TeamMemberRoleIn.model_validate({"role": "manager", "email": "x"})


def test_the_invitation_language_is_one_the_app_offers_once_it_says_which() -> None:
    assert TeamMemberIn(email="ada@example.com", locale="xx").locale == "xx"

    offering = types.new_class(
        "TeamMemberIn", (TeamMemberIn,), exec_body=lambda ns: ns.update({"offered_locales": ("de-CH", "en")})
    )
    assert offering(email="ada@example.com", locale="de_ch").locale == "de-CH"
    with pytest.raises(ValidationError, match="locale must be one of de-CH, en"):
        offering(email="ada@example.com", locale="fr")
