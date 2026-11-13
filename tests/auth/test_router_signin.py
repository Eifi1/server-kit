"""The account routes over HTTP: sign-up, sign-in, the forced change, the refresh (auth §4–§6,
§11, §12.5 items 1, 4, 5, 7).

No ``from __future__ import annotations``: the hooks below are read at run time.
"""

from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import SHARED_ACCESS_LIFETIME, AuthError, AuthErrorCode, mint
from eifi1_server_kit.auth.service import AuthHooks
from tests.auth._world import PASSWORD, World
from tests.auth_fakes import MemoryInvitation, MemoryUser

REGISTER = {"password": PASSWORD, "first_name": "Ada", "last_name": "Lovelace", "locale": "en"}


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


def invite(world: World, email: str, **values: Any) -> tuple[str, MemoryInvitation]:
    raw, digest = mint()
    return raw, world.invitations.seed(email=email, token_hash=digest, **values)


# --- sign-up ----------------------------------------------------------------------------------


async def test_the_first_account_is_the_admin_signed_in_with_a_session(world: World) -> None:
    response = await world.call("POST", "/auth/register", json={"email": "Ada@Example.com", **REGISTER})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == "ada@example.com" and body["user"]["role"] == "ADMIN"
    assert body["user"]["email_verified"] is False and body["token_type"] == "bearer"
    user = world.users.rows[1]
    assert user.last_login_at == world.clock() and user.password_hash == f"hashed:{PASSWORD}"
    (session,) = world.sessions.of(user.id)
    claims = world.signer.claims(body["access_token"])
    assert claims["sid"] == session.sid and claims["type"] == "access"
    assert world.signer.claims(body["refresh_token"])["sid"] == session.sid
    assert session.ip_address == "198.51.100.1" and session.device == "python-httpx/" + __import__("httpx").__version__
    (mail,) = world.mailer.of("verification")
    assert mail.to == "ada@example.com" and mail.after_commits == 1
    assert "/verify-email?token=" in world.mailer.link("verification")


async def test_after_the_first_a_sign_up_needs_an_invitation(world: World) -> None:
    world.account()
    response = await world.call("POST", "/auth/register", json={"email": "bob@example.com", **REGISTER})
    assert response.status_code == 403 and code(response) == "registration_closed"


async def test_the_invitation_is_checked_before_the_gate_and_the_address(world: World) -> None:
    """§12.5 item 5: an anonymous caller without an invitation learns nothing about an address."""
    world.account(email="taken@example.com")
    response = await world.call("POST", "/auth/register", json={"email": "taken@example.com", **REGISTER})
    assert response.status_code == 403 and code(response) == "registration_closed"
    for token, expected in (("unknown", "invitation_invalid"),):
        response = await world.call(
            "POST", "/auth/register", json={"email": "taken@example.com", "invite_token": token, **REGISTER}
        )
        assert code(response) == expected
    raw, _ = invite(world, "taken@example.com")
    response = await world.call(
        "POST", "/auth/register", json={"email": "taken@example.com", "invite_token": raw, **REGISTER}
    )
    assert response.status_code == 409 and code(response) == "email_taken"


async def test_a_dead_or_foreign_invitation_has_its_own_code(world: World) -> None:
    world.account()
    expired, _ = invite(world, "bob@example.com", expires_at=world.clock() - timedelta(seconds=1))
    spent, _ = invite(world, "bob@example.com", accepted_at=world.clock())
    revoked, _ = invite(world, "bob@example.com", revoked_at=world.clock())
    foreign, _ = invite(world, "carol@example.com")
    for token, expected in (
        (expired, "invitation_expired"),
        (spent, "invitation_invalid"),
        (revoked, "invitation_invalid"),
        (foreign, "invitation_invalid"),
    ):
        response = await world.call(
            "POST", "/auth/register", json={"email": "bob@example.com", "invite_token": token, **REGISTER}
        )
        assert response.status_code == 400 and code(response) == expected


async def test_an_invitation_names_the_role_and_proves_its_exact_address(world: World) -> None:
    world.account()
    seen: list[tuple[int, int | None, str]] = []

    async def on_register(user: MemoryUser, invitation: MemoryInvitation | None, body: Any) -> None:
        seen.append((user.id, invitation.id if invitation else None, body.email))

    world.hooks = AuthHooks(on_register=on_register)
    raw, invitation = invite(world, "bob@example.com", role="REVIEWER")
    response = await world.call(
        "POST", "/auth/register", json={"email": "bob@example.com", "invite_token": raw, **REGISTER}
    )
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "REVIEWER" and response.json()["user"]["email_verified"] is True
    assert invitation.accepted_by_user_id == 2 and seen == [(2, invitation.id, "bob@example.com")]
    assert world.mailer.of("verification") == []


async def test_a_tagged_sign_up_takes_the_invitation_but_is_sent_its_own_link(world: World) -> None:
    world.account()
    raw, _ = invite(world, "bob@example.com")
    response = await world.call(
        "POST", "/auth/register", json={"email": "bob+staunlabor@example.com", "invite_token": raw, **REGISTER}
    )
    assert response.status_code == 201
    user = response.json()["user"]
    assert user["role"] == "MEMBER" and user["email_verified"] is False
    assert world.mailer.of("verification")[0].to == "bob+staunlabor@example.com"


async def test_the_bootstrap_list_decides_who_claims_a_fresh_deployment() -> None:
    from dataclasses import replace

    from tests.auth._world import POLICY

    world = World(policy=replace(POLICY, bootstrap=frozenset({"ada@example.com"})))
    async with world.client:
        response = await world.call("POST", "/auth/register", json={"email": "eve@example.com", **REGISTER})
        assert code(response) == "registration_closed"
        response = await world.call("POST", "/auth/register", json={"email": "ada@example.com", **REGISTER})
        assert response.status_code == 201 and response.json()["user"]["role"] == "ADMIN"


async def test_a_sign_up_on_a_shared_computer_gets_the_short_session(world: World) -> None:
    response = await world.call(
        "POST", "/auth/register", json={"email": "ada@example.com", "shared_device": True, **REGISTER}
    )
    claims = world.signer.claims(response.json()["access_token"])
    assert claims["exp"] - int(world.clock().timestamp()) == SHARED_ACCESS_LIFETIME.total_seconds()
    assert world.sessions.of(1)[0].shared_device is True


async def test_registering_is_throttled_per_address_of_the_caller(world: World) -> None:
    world.account()
    for _ in range(5):
        response = await world.call("POST", "/auth/register", json={"email": "x@example.com", **REGISTER})
        assert code(response) == "registration_closed"
    response = await world.call("POST", "/auth/register", json={"email": "x@example.com", **REGISTER})
    assert response.status_code == 429 and int(response.headers["Retry-After"]) >= 1
    response = await world.call(
        "POST", "/auth/register", json={"email": "x@example.com", **REGISTER}, headers={"x-test-ip": "203.0.113.9"}
    )
    assert code(response) == "registration_closed"


# --- sign-in ---------------------------------------------------------------------------------------


async def test_a_sign_in_answers_a_session_and_stamps_the_sign_in(world: World) -> None:
    user = world.account(email_verified_at=world.clock())
    body = await world.sign_in(user)
    assert body["user"]["email_verified"] is True and user.last_login_at == world.clock()
    me = await world.call("GET", "/auth/me", token=body["access_token"])
    assert me.status_code == 200 and me.json()["display_name"] == "Ada Lovelace"
    assert world.waits == [0.0]


async def test_one_refusal_for_a_wrong_password_an_unknown_address_and_a_deactivated_account(world: World) -> None:
    world.account()
    world.account(email="off@example.com", is_active=False)
    checks = world.hasher.checks
    for email, password in (
        ("ada@example.com", "wrong"),
        ("nobody@example.com", PASSWORD),
        ("off@example.com", PASSWORD),
    ):
        response = await world.call("POST", "/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401 and code(response) == "invalid_credentials"
    assert world.hasher.checks - checks == 3, "the absent address costs a check too"
    assert world.sessions.rows == {}


async def test_failures_slow_an_address_down_and_a_success_forgets_them(world: World) -> None:
    user = world.account()
    for _ in range(6):
        await world.call("POST", "/auth/login", json={"email": user.email, "password": "wrong"})
    await world.sign_in(user)
    assert world.waits[-1] == 2.0
    await world.sign_in(user)
    assert world.waits[-1] == 0.0


async def test_a_forced_change_answers_its_challenge_and_set_password_ends_every_session(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    old = await world.sign_in(user)
    user.password_change_required_at = world.clock()
    changing: list[str] = []

    async def challenge_extras(account: MemoryUser) -> dict[str, Any]:
        return {"encrypted": True}

    async def password_changing(account: MemoryUser, body: Any) -> dict[str, Any]:
        changing.append(body.new_password)
        return {}

    async def sessions_ended(account: MemoryUser, reason: str) -> None:
        changing.append(reason)

    world.hooks = AuthHooks(
        challenge_extras=challenge_extras, password_changing=password_changing, sessions_ended=sessions_ended
    )
    response = await world.call(
        "POST", "/auth/login", json={"email": user.email, "password": PASSWORD, "shared_device": True}
    )
    challenge = response.json()
    assert challenge["requires_password_change"] is True and "encrypted" not in challenge
    same = await world.call(
        "POST",
        "/auth/login/set-password",
        json={"challenge_token": challenge["challenge_token"], "new_password": PASSWORD},
    )
    assert same.status_code == 400 and code(same) == "password_unchanged"
    response = await world.call(
        "POST",
        "/auth/login/set-password",
        json={"challenge_token": challenge["challenge_token"], "new_password": "a new one"},
    )
    assert response.status_code == 200, response.text
    assert user.password_change_required_at is None and user.password_hash == "hashed:a new one"
    assert user.sessions_invalid_before == world.clock()
    assert changing == ["a new one", "forced_change"]
    stale = await world.call("GET", "/auth/me", token=old["access_token"])
    assert stale.status_code == 401 and code(stale) == "session_ended"
    fresh = await world.call("GET", "/auth/me", token=response.json()["access_token"])
    assert fresh.status_code == 200
    live = [row for row in world.sessions.of(user.id) if row.is_active]
    assert len(live) == 1 and live[0].shared_device is True
    again = await world.call(
        "POST",
        "/auth/login/set-password",
        json={"challenge_token": challenge["challenge_token"], "new_password": "a third one"},
    )
    assert again.status_code == 409 and code(again) == "password_change_not_required"


async def test_set_password_refuses_a_challenge_that_is_not_one(world: World) -> None:
    user = world.account(password_change_required_at=world.clock())
    off = world.account(email="off@example.com", is_active=False, password_change_required_at=world.clock())
    from eifi1_server_kit.auth import ChallengeKind, challenge_claims

    tokens = {
        "unknown": "jwt.nope",
        "access": (await world.sign_in(world.account(email="bob@example.com")))["access_token"],
        "off": world.signer.sign(challenge_claims(off.id, ChallengeKind.PASSWORD_CHANGE, now=world.clock())),
        "nobody": world.signer.sign(challenge_claims("webauthn", ChallengeKind.PASSWORD_CHANGE, now=world.clock())),
        "gone": world.signer.sign(challenge_claims(99, ChallengeKind.PASSWORD_CHANGE, now=world.clock())),
    }
    for name, token in tokens.items():
        response = await world.call(
            "POST", "/auth/login/set-password", json={"challenge_token": token, "new_password": "a new one"}
        )
        assert response.status_code == 400 and code(response) == "token_invalid", name
    assert user.password_change_required_at is not None


# --- the refresh -------------------------------------------------------------------------------


async def test_a_refresh_renews_the_same_session(world: World) -> None:
    user = world.account()
    pair = await world.sign_in(user)
    world.clock.advance(hours=1)
    response = await world.call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 200
    (session,) = world.sessions.of(user.id)
    assert session.last_active == world.clock()
    assert world.signer.claims(response.json()["access_token"])["sid"] == session.sid


async def test_every_refusal_of_a_refresh_is_session_ended(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    other = world.account(email="bob@example.com")
    pair = await world.sign_in(user)
    theirs = await world.sign_in(other)
    from eifi1_server_kit.auth import refresh_claims

    forged = world.signer.sign(
        refresh_claims(other.id, now=world.clock(), extra={"sid": world.sessions.of(user.id)[0].sid})
    )
    for token in ("jwt.unknown", pair["access_token"], forged):
        response = await world.call("POST", "/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401 and code(response) == "session_ended"
    other.password_change_required_at = world.clock()
    response = await world.call("POST", "/auth/refresh", json={"refresh_token": theirs["refresh_token"]})
    assert code(response) == "session_ended"
    user.is_active = False
    assert code(await world.call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})) == (
        "session_ended"
    )
    user.is_active = True
    user.sessions_invalid_before = world.clock()  # type: ignore[attr-defined]
    assert code(await world.call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})) == (
        "session_ended"
    )


async def test_a_shared_session_ends_on_the_server_s_clock(world: World) -> None:
    user = world.account()
    pair = await world.sign_in(user, shared_device=True)
    world.clock.advance(minutes=15)
    response = await world.call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 401 and code(response) == "session_ended"


async def test_the_claims_hook_adds_to_the_access_token_and_may_end_a_refresh(world: World) -> None:
    user = world.account()
    refuse = False

    async def claims(account: MemoryUser, session: Any) -> dict[str, Any]:
        if refuse:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "No company left")
        return {"company_id": 7, "sid": "ignored"}

    world.hooks = AuthHooks(claims=claims)
    pair = await world.sign_in(user)
    access = world.signer.claims(pair["access_token"])
    assert access["company_id"] == 7 and access["sid"] == world.sessions.of(user.id)[0].sid
    refuse = True
    response = await world.call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 401 and code(response) == "session_ended"


async def test_the_refresh_is_throttled_per_caller(world: World) -> None:
    for _ in range(120):
        await world.call("POST", "/auth/refresh", json={"refresh_token": "jwt.nope"})
    response = await world.call("POST", "/auth/refresh", json={"refresh_token": "jwt.nope"})
    assert response.status_code == 429


# --- the bearer -------------------------------------------------------------------------------------


async def test_a_signed_in_route_refuses_with_a_code_and_never_for_a_wrong_input(world: World) -> None:
    user = world.account()
    pair = await world.sign_in(user)
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer jwt.nope"}):
        response = await world.call("GET", "/auth/me", headers=headers)
        assert response.status_code == 401 and code(response) == "not_signed_in"
        assert response.headers["WWW-Authenticate"] == "Bearer"
    response = await world.call("GET", "/auth/me", token=pair["refresh_token"])
    assert code(response) == "not_signed_in"
    world.sessions.of(user.id)[0].is_active = False
    response = await world.call("GET", "/auth/me", token=pair["access_token"])
    assert response.status_code == 401 and code(response) == "session_ended"


async def test_bind_runs_on_every_signed_in_request_and_may_refuse(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    bound: list[Any] = []

    async def bind(account: MemoryUser, claims: Any) -> None:
        bound.append(claims["sid"])
        if len(bound) > 1:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "Not a member any more")

    world.hooks = AuthHooks(bind=bind)
    assert (await world.call("GET", "/auth/me", token=token)).status_code == 200
    assert code(await world.call("GET", "/auth/me", token=token)) == "session_ended"


@pytest.mark.parametrize("plan", ["pro"])
async def test_the_app_s_me_model_and_to_me(plan: str) -> None:
    from eifi1_server_kit.auth import UserResponse

    class Me(UserResponse):
        plan: str = "free"

    async def to_me(account: MemoryUser) -> Me:
        return Me.model_validate(account, from_attributes=True).model_copy(update={"plan": plan})

    world = World(me_model=Me)
    async with world.client:
        user = world.account()
        body = await world.sign_in(user)
        assert body["user"]["plan"] == "free"
        world.hooks = AuthHooks(to_me=to_me)
        response = await world.call("GET", "/auth/me", token=body["access_token"])
        assert response.json()["plan"] == plan


async def test_an_empty_bootstrap_list_is_no_list() -> None:
    """An unset ``*_ADMIN_EMAILS`` parses to an empty set: the first registration stays open."""
    from dataclasses import replace

    from tests.auth._world import POLICY

    world = World(policy=replace(POLICY, bootstrap=frozenset()))
    async with world.client:
        response = await world.call("POST", "/auth/register", json={"email": "eve@example.com", **REGISTER})
        assert response.status_code == 201 and response.json()["user"]["role"] == "ADMIN"
