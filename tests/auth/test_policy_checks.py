"""Staunlabor's 0.29 pilot (0.30): the roles an invitation may name, and one list of the app's
locales for the sign-up, the invitation and the profile alike."""

from dataclasses import replace
from typing import Any

import pytest

from eifi1_server_kit.auth.router import AuthDeps, auth_router, invitations_admin_router
from eifi1_server_kit.settings import profile_update_model
from tests.auth._world import POLICY, World

OFFERED = ("de-CH", "en", "fr", "it")
REGISTER = {"password": "correct horse battery", "first_name": "Ada", "last_name": "Lovelace"}


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


# ── the roles an invitation may name ────────────────────────────────────────


async def test_an_app_that_lists_its_invitable_roles_refuses_any_other() -> None:
    world = World(policy=replace(POLICY, invitable_roles=("ADMIN", "MEMBER")))
    async with world.client:
        token = await world.token_of(world.account(role="ADMIN"))
        refused = await world.call(
            "POST", "/admin/invitations", token=token, json={"email": "rev@example.com", "role": "REVIEWER"}
        )
        assert (refused.status_code, code(refused)) == (422, "role_not_grantable")
        assert world.invitations.rows == {} and world.mailer.sent == [] and world.log.rows == []
        sent = await world.call(
            "POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER"}
        )
        assert sent.status_code == 201, sent.text


async def test_without_a_list_an_invitation_names_any_role(world: World) -> None:
    token = await world.token_of(world.account(role="ADMIN"))
    sent = await world.call("POST", "/admin/invitations", token=token, json={"email": "r@example.com", "role": "X"})
    assert sent.status_code == 201, "0.29's behaviour: unchecked"


def test_the_default_role_is_one_an_invitation_may_name() -> None:
    with pytest.raises(ValueError, match="default_role"):
        replace(POLICY, invitable_roles=("ADMIN",))


# ── one list of locales ─────────────────────────────────────────────────────


def _locale_error(response: Any) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    found: dict[str, Any] = error
    return found


async def test_one_list_of_locales_for_the_sign_up_the_invitation_and_the_profile() -> None:
    world = World(offered_locales=OFFERED)
    async with world.client:
        signed_up = await world.call(
            "POST", "/auth/register", json={"email": "ada@example.com", "locale": "de_ch", **REGISTER}
        )
        assert signed_up.status_code == 201, signed_up.text
        assert world.users.rows[1].locale == "de-CH", "stored as the offered tag"
        token = signed_up.json()["access_token"]
        profile = _locale_error(await world.call("PATCH", "/auth/me", token=token, json={"locale": "pt-BR"}))
        register = _locale_error(
            await world.call("POST", "/auth/register", json={"email": "b@example.com", "locale": "pt-BR", **REGISTER})
        )
        invitation = _locale_error(
            await world.call(
                "POST",
                "/admin/invitations",
                token=token,
                json={"email": "c@example.com", "role": "MEMBER", "locale": "pt-BR"},
            )
        )
        assert profile["loc"] == register["loc"] == invitation["loc"] == ["body", "locale"]
        assert profile["msg"] == register["msg"] == invitation["msg"], "one refusal, one shape"
        assert profile["type"] == register["type"] == invitation["type"] == "value_error"
        moved = await world.call("PATCH", "/auth/me", token=token, json={"locale": "FR"})
        assert moved.status_code == 200 and moved.json()["locale"] == "fr"


async def test_without_a_list_any_well_formed_tag_is_taken(world: World) -> None:
    response = await world.call(
        "POST", "/auth/register", json={"email": "ada@example.com", "locale": "pt-BR", **REGISTER}
    )
    assert response.status_code == 201, "0.29's behaviour: the tag's shape only"


async def test_an_app_s_own_bodies_keep_their_fields_and_meet_the_list() -> None:
    from eifi1_server_kit.auth import RegisterRequest

    class Register(RegisterRequest):
        company: str | None = None

    world = World(offered_locales=OFFERED, register_model=Register, profile_model=profile_update_model(OFFERED))
    async with world.client:
        refused = await world.call(
            "POST", "/auth/register", json={"email": "a@example.com", "locale": "pt-BR", "company": "X", **REGISTER}
        )
        assert _locale_error(refused)["loc"] == ["body", "locale"]
        taken = await world.call(
            "POST", "/auth/register", json={"email": "a@example.com", "locale": "it", "company": "X", **REGISTER}
        )
        assert taken.status_code == 201, taken.text


def test_a_body_whose_own_list_disagrees_is_refused_when_the_router_is_built() -> None:
    world = World()
    values = {name: getattr(world.deps, name) for name in AuthDeps.__dataclass_fields__}
    values.update(offered_locales=OFFERED, profile_model=profile_update_model(("en",)))
    with pytest.raises(ValueError, match="offered_locales"):
        auth_router(AuthDeps(**values))
    values.update(profile_model=profile_update_model(OFFERED))
    invitations_admin_router(AuthDeps(**values))
