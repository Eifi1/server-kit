"""Passkeys over HTTP, against py_webauthn's own checks and a software authenticator (§12.5
item 8: usernameless, user verification required, one use per challenge)."""

import builtins
import importlib
import sys
from typing import Any

import pytest

from eifi1_server_kit.auth import ChallengeKind, challenge_claims
from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.passkeys import authentication_options
from eifi1_server_kit.auth.service import AuthHooks
from tests.auth._world import ORIGIN, PASSWORD, RP, World
from tests.auth.soft_webauthn import SoftAuthenticator
from tests.auth_fakes import MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


async def begin(world: World, token: str) -> dict[str, Any]:
    response = await world.call(
        "POST", "/auth/passkey/register/begin", token=token, json={"current_password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def add_passkey(world: World, token: str, device: SoftAuthenticator, name: str = "Laptop") -> Any:
    started = await begin(world, token)
    return await world.call(
        "POST",
        "/auth/passkey/register/finish",
        token=token,
        json={
            "challenge_token": started["challenge_token"],
            "credential": device.create(started["options"]),
            "name": name,
        },
    )


async def sign_in(world: World, device: SoftAuthenticator, **options: Any) -> Any:
    started = (await world.call("POST", "/auth/passkey/login/begin")).json()
    credential = device.get(started["options"], **options)
    return await world.call(
        "POST",
        "/auth/passkey/login/finish",
        json={"challenge_token": started["challenge_token"], "credential": credential},
    )


# --- registering ---------------------------------------------------------------------------------


async def test_adding_a_passkey_asks_the_password_and_requires_user_verification(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    response = await world.call("POST", "/auth/passkey/register/begin", token=token, json={"current_password": "x"})
    assert response.status_code == 400 and code(response) == "password_incorrect"
    started = await begin(world, token)
    selection = started["options"]["authenticatorSelection"]
    assert (selection["residentKey"], selection["userVerification"]) == ("required", "required")
    assert started["options"]["rp"]["id"] == "staunlabor.example"
    device = SoftAuthenticator(ORIGIN)
    commits = world.after.commits
    response = await world.call(
        "POST",
        "/auth/passkey/register/finish",
        token=token,
        json={
            "challenge_token": started["challenge_token"],
            "credential": device.create(started["options"]),
            "name": " ",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Passkey" and set(response.json()) == {"id", "name", "created_at", "last_used_at"}
    (row,) = world.passkey_store.rows.values()
    assert row.credential_id == device.credential_id and row.transports == "internal,hybrid"
    (mail,) = world.mailer.of("passkey_added_notice")
    assert mail.after_commits == commits + 1 and "“Passkey”" in mail.message.text


async def test_the_extensions_hook_rides_on_both_ceremonies(world: World) -> None:
    asked: list[Any] = []

    async def passkey_extensions(account: MemoryUser | None, ceremony: str) -> dict[str, Any]:
        asked.append((account.id if account else None, ceremony))
        return {"prf": {}}

    world.hooks = AuthHooks(passkey_extensions=passkey_extensions)
    user = world.account()
    started = await begin(world, await world.token_of(user))
    login = (await world.call("POST", "/auth/passkey/login/begin")).json()
    assert started["options"]["extensions"] == {"prf": {}} == login["options"]["extensions"]
    assert asked == [(user.id, "registration"), (None, "authentication")]
    assert "allowCredentials" not in login["options"] or login["options"]["allowCredentials"] == []


async def test_a_registration_answer_is_refused_unless_it_is_genuine_and_new(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    device = SoftAuthenticator(ORIGIN)
    started = await begin(world, token)
    credential = device.create(started["options"])
    body = {"challenge_token": started["challenge_token"], "credential": credential}
    assert (await world.call("POST", "/auth/passkey/register/finish", token=token, json=body)).status_code == 201
    again = await world.call("POST", "/auth/passkey/register/finish", token=token, json=body)
    assert again.status_code == 400 and code(again) == "passkey_registration_refused", "a challenge answers once"
    duplicate = await add_passkey(world, token, device)
    assert code(duplicate) == "passkey_registration_refused", "the same credential again"
    no_pin = SoftAuthenticator(ORIGIN, verifies=False)
    assert code(await add_passkey(world, token, no_pin)) == "passkey_registration_refused"
    elsewhere = SoftAuthenticator("https://evil.example")
    assert code(await add_passkey(world, token, elsewhere)) == "passkey_registration_refused"
    other = world.account(email="bob@example.com")
    theirs = await begin(world, await world.token_of(other))
    foreign = {
        "challenge_token": theirs["challenge_token"],
        "credential": SoftAuthenticator(ORIGIN).create(theirs["options"]),
    }
    for challenge_token in (theirs["challenge_token"], "jwt.nope", (await world.sign_in(user))["access_token"]):
        response = await world.call(
            "POST", "/auth/passkey/register/finish", token=token, json={**foreign, "challenge_token": challenge_token}
        )
        assert response.status_code == 400 and code(response) == "passkey_registration_refused"
    bad = world.signer.sign(
        challenge_claims(user.id, ChallengeKind.WEBAUTHN_REGISTER, now=world.clock(), extra={"challenge": "!!"})
    )
    response = await world.call(
        "POST", "/auth/passkey/register/finish", token=token, json={**foreign, "challenge_token": bad}
    )
    assert code(response) == "passkey_registration_refused"
    assert len(world.passkey_store.rows) == 1


# --- signing in ---------------------------------------------------------------------------------------


async def test_a_passkey_signs_in_usernameless_and_counts_as_both_factors(world: World) -> None:
    user = world.account()
    device = SoftAuthenticator(ORIGIN)
    await add_passkey(world, await world.token_of(user), device)
    response = await sign_in(world, device)
    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] == user.id
    (row,) = world.passkey_store.rows.values()
    assert row.sign_count == 1 and row.last_used_at == world.clock()
    assert user.last_login_at == world.clock()


async def test_a_passkey_sign_in_on_a_shared_computer_and_with_a_forced_change(world: World) -> None:
    user = world.account()
    device = SoftAuthenticator(ORIGIN)
    await add_passkey(world, await world.token_of(user), device)
    started = (await world.call("POST", "/auth/passkey/login/begin")).json()
    response = await world.call(
        "POST",
        "/auth/passkey/login/finish",
        json={
            "challenge_token": started["challenge_token"],
            "credential": device.get(started["options"]),
            "shared_device": True,
        },
    )
    sid = world.signer.claims(response.json()["access_token"])["sid"]
    assert world.sessions.rows[sid].shared_device is True
    user.password_change_required_at = world.clock()
    response = await sign_in(world, device)
    assert response.json()["requires_password_change"] is True


async def test_every_refused_passkey_sign_in_is_one_answer(world: World) -> None:
    user = world.account()
    device = SoftAuthenticator(ORIGIN)
    await add_passkey(world, await world.token_of(user), device)
    stranger = SoftAuthenticator(ORIGIN)
    stranger.rp_id, stranger.user_handle = device.rp_id, device.user_handle
    assert code(await sign_in(world, stranger)) == "passkey_refused", "an unknown credential"
    assert code(await sign_in(world, device, user_handle=SoftAuthenticator.handle_of(99))) == "passkey_refused"
    assert code(await sign_in(world, device, user_handle="not base64!")) == "passkey_refused"
    assert (
        code(await sign_in(world, device, user_handle=SoftAuthenticator.handle_of(0)[:-1] + "x")) == "passkey_refused"
    )
    device.verifies = False
    assert code(await sign_in(world, device)) == "passkey_refused", "no user verification"
    device.verifies = True
    started = (await world.call("POST", "/auth/passkey/login/begin")).json()
    answer = device.get(started["options"])
    body = {"challenge_token": started["challenge_token"], "credential": answer}
    assert (await world.call("POST", "/auth/passkey/login/finish", json=body)).status_code == 200
    replay = await world.call("POST", "/auth/passkey/login/finish", json=body)
    assert replay.status_code == 401 and code(replay) == "passkey_refused", "a challenge answers once"
    for challenge_token in (
        "jwt.nope",
        world.signer.sign(
            challenge_claims(user.id, ChallengeKind.WEBAUTHN_REGISTER, now=world.clock(), extra={"challenge": "abc"})
        ),
    ):
        response = await world.call(
            "POST", "/auth/passkey/login/finish", json={**body, "challenge_token": challenge_token}
        )
        assert code(response) == "passkey_refused"
    fresh = (await world.call("POST", "/auth/passkey/login/begin")).json()
    broken = {**device.get(fresh["options"]), "rawId": "***"}
    response = await world.call(
        "POST", "/auth/passkey/login/finish", json={"challenge_token": fresh["challenge_token"], "credential": broken}
    )
    assert code(response) == "passkey_refused"
    missing = {key: value for key, value in device.get(fresh["options"]).items() if key != "rawId"}
    response = await world.call(
        "POST", "/auth/passkey/login/finish", json={"challenge_token": fresh["challenge_token"], "credential": missing}
    )
    assert code(response) == "passkey_refused"
    user.is_active = False
    assert code(await sign_in(world, device)) == "passkey_refused", "a deactivated account"
    user.is_active = True
    del world.users.rows[user.id]
    assert code(await sign_in(world, device)) == "passkey_refused", "an account gone"


async def test_a_synced_passkey_s_answer_is_spent_by_its_challenge_not_its_counter(world: World) -> None:
    """A synced passkey's counter stays at zero, so only the spent challenge refuses a replay."""
    user = world.account()
    device = SoftAuthenticator(ORIGIN, counts=False)
    await add_passkey(world, await world.token_of(user), device)
    started = (await world.call("POST", "/auth/passkey/login/begin")).json()
    body = {"challenge_token": started["challenge_token"], "credential": device.get(started["options"])}
    assert (await world.call("POST", "/auth/passkey/login/finish", json=body)).status_code == 200
    replay = await world.call("POST", "/auth/passkey/login/finish", json=body)
    assert replay.status_code == 401 and code(replay) == "passkey_refused"
    malformed = {**body, "credential": {"id": "x", "rawId": body["credential"]["rawId"], "type": "public-key"}}
    assert code(await world.call("POST", "/auth/passkey/login/finish", json=malformed)) == "passkey_refused"


async def test_a_passkey_sign_in_spends_the_login_window(world: World) -> None:
    for _ in range(30):
        await world.call("POST", "/auth/passkey/login/finish", json={"challenge_token": "jwt.nope", "credential": {}})
    response = await world.call(
        "POST", "/auth/passkey/login/finish", json={"challenge_token": "jwt.nope", "credential": {}}
    )
    assert response.status_code == 429


# --- the list ----------------------------------------------------------------------------------------


async def test_the_list_rename_and_delete_are_the_account_s_own(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    await add_passkey(world, token, SoftAuthenticator(ORIGIN), name="Laptop")
    await add_passkey(world, token, SoftAuthenticator(ORIGIN), name="Phone")
    listed = (await world.call("GET", "/auth/passkeys", token=token)).json()
    assert [row["name"] for row in listed] == ["Laptop", "Phone"]
    response = await world.call("PATCH", f"/auth/passkeys/{listed[1]['id']}", token=token, json={"name": " Work "})
    assert response.json()["name"] == "Work"
    other = world.account(email="bob@example.com")
    theirs = await world.token_of(other)
    for method, json in (("PATCH", {"name": "Mine"}), ("DELETE", None)):
        response = await world.call(method, f"/auth/passkeys/{listed[0]['id']}", token=theirs, json=json)
        assert response.status_code == 404 and code(response) == "passkey_not_found"
    removing: list[int] = []

    async def passkey_removing(account: MemoryUser, passkey: Any) -> None:
        removing.append(passkey.id)
        if passkey.name == "Work":
            raise AuthError(AuthErrorCode.PASSKEY_REGISTRATION_REFUSED, "The last way to unlock")

    world.hooks = AuthHooks(passkey_removing=passkey_removing)
    assert (await world.call("DELETE", f"/auth/passkeys/{listed[1]['id']}", token=token)).status_code == 400
    assert (await world.call("DELETE", f"/auth/passkeys/{listed[0]['id']}", token=token)).status_code == 204
    assert removing == [listed[1]["id"], listed[0]["id"]] and len(world.passkey_store.rows) == 1
    world.hooks = AuthHooks()
    assert (await world.call("DELETE", f"/auth/passkeys/{listed[1]['id']}", token=token)).status_code == 204
    assert (await world.call("GET", "/auth/passkeys", token=token)).json() == [], "the last one goes too"


async def test_a_demo_adds_no_passkey(cutoff_world: World) -> None:
    world = cutoff_world
    demo = world.account(is_demo=True)
    token = await world.token_of(demo)
    response = await world.call(
        "POST", "/auth/passkey/register/begin", token=token, json={"current_password": PASSWORD}
    )
    assert code(response) == "demo_refused"
    started = {"challenge_token": "jwt.nope", "credential": {}}
    assert code(await world.call("POST", "/auth/passkey/register/finish", token=token, json=started)) == "demo_refused"
    assert code(await world.call("PATCH", "/auth/passkeys/1", token=token, json={"name": "x"})) == "demo_refused"
    assert code(await world.call("DELETE", "/auth/passkeys/1", token=token)) == "demo_refused"


# --- mounting -------------------------------------------------------------------------------------------


async def test_without_a_passkey_store_the_routes_are_not_mounted() -> None:
    world = World(passkeys=False)
    async with world.client:
        assert (await world.call("POST", "/auth/passkey/login/begin")).status_code == 404
    with pytest.raises(ValueError, match="relying_party"):
        World(passkeys=False, relying_party=RP)


def test_the_ceremonies_say_which_extra_they_need(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_webauthn(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "webauthn" or name.startswith("webauthn."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_webauthn)
    with pytest.raises(ImportError, match=r"eifi1-server-kit\[passkeys\]"):
        authentication_options(RP)
    monkeypatch.undo()
    module = importlib.import_module("eifi1_server_kit.auth.passkeys")
    assert module is sys.modules["eifi1_server_kit.auth.passkeys"]
