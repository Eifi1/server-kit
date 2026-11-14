"""The account routes over HTTP: the sessions, signing out, the profile, the password change
(auth §6.1, §6.3; §12.5 item 1).

No ``from __future__ import annotations``: the hooks and guards below are read at run time.
"""

from typing import Annotated, Any

from fastapi import Depends

from eifi1_server_kit.auth import OneTimeTokenKind, access_claims
from eifi1_server_kit.auth.router import current_user
from eifi1_server_kit.auth.service import AuthHooks
from eifi1_server_kit.demo import refuse_demo
from tests.auth._world import PASSWORD, World
from tests.auth_fakes import MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


# --- the sessions ------------------------------------------------------------------------------


async def test_the_sessions_list_marks_the_caller_s_and_hides_ended_ones(world: World) -> None:
    user = world.account()
    first = await world.sign_in(user)
    world.clock.advance(minutes=1)
    second = await world.sign_in(user, shared_device=True)
    third = await world.sign_in(user)
    world.sessions.rows[world.signer.claims(third["access_token"])["sid"]].is_active = False
    response = await world.call("GET", "/auth/sessions", token=first["access_token"])
    assert response.status_code == 200
    rows = response.json()
    assert [row["current"] for row in rows] == [False, True]
    assert rows[0]["shared_device"] is True and rows[0]["id"] == world.signer.claims(second["access_token"])["sid"]
    world.clock.advance(minutes=15)
    rows = (await world.call("GET", "/auth/sessions", token=first["access_token"])).json()
    assert [row["current"] for row in rows] == [True], "the shared one ran past its idle limit"


async def test_ending_this_session_answers_204_also_once_it_ended(world: World) -> None:
    user = world.account()
    pair = await world.sign_in(user)
    for _ in range(2):
        response = await world.call("DELETE", "/auth/sessions/current", token=pair["access_token"])
        assert response.status_code == 204
    assert not world.sessions.of(user.id)[0].is_active
    assert code(await world.call("GET", "/auth/me", token=pair["access_token"])) == "session_ended"
    for token in (None, pair["refresh_token"]):
        response = await world.call("DELETE", "/auth/sessions/current", token=token)
        assert response.status_code == 401 and code(response) == "not_signed_in"


async def test_ending_this_session_ends_nobody_else_s(world: World) -> None:
    user = world.account()
    other = world.account(email="bob@example.com")
    await world.sign_in(user)
    theirs = world.sessions.of(user.id)[0]
    forged = world.signer.sign(access_claims(other.id, now=world.clock(), extra={"sid": theirs.sid}))
    no_sid = world.signer.sign(access_claims(other.id, now=world.clock()))
    for token in (forged, no_sid):
        assert (await world.call("DELETE", "/auth/sessions/current", token=token)).status_code == 204
    assert theirs.is_active


async def test_one_device_is_ended_from_another(world: World) -> None:
    user = world.account()
    mine = await world.sign_in(user)
    other = await world.sign_in(user)
    sid = world.signer.claims(other["access_token"])["sid"]
    response = await world.call("DELETE", f"/auth/sessions/{sid}", token=mine["access_token"])
    assert response.status_code == 204 and not world.sessions.rows[sid].is_active
    for unknown in (sid, "nope"):
        response = await world.call("DELETE", f"/auth/sessions/{unknown}", token=mine["access_token"])
        assert response.status_code == 404 and code(response) == "session_not_found"
    stranger = await world.sign_in(world.account(email="bob@example.com"))
    their_sid = world.signer.claims(stranger["access_token"])["sid"]
    response = await world.call("DELETE", f"/auth/sessions/{their_sid}", token=mine["access_token"])
    assert response.status_code == 404


async def test_signing_out_everywhere_ends_every_row_and_what_waited(world: World) -> None:
    user = world.account()
    first = await world.sign_in(user)
    await world.sign_in(user)
    await world.tokens.put(
        OneTimeTokenKind.EMAIL_CHANGE,
        user_id=user.id,
        token_hash="h",
        email="new@example.com",
        expires_at=world.clock(),
    )
    reasons: list[str] = []

    async def sessions_ended(account: MemoryUser, reason: str) -> None:
        reasons.append(reason)

    world.hooks = AuthHooks(sessions_ended=sessions_ended)
    response = await world.call("POST", "/auth/logout", token=first["access_token"])
    assert response.status_code == 204
    assert not any(row.is_active for row in world.sessions.of(user.id))
    assert reasons == ["logout_everywhere"] and world.tokens.rows == []
    assert all("sessions_invalid_before" not in changes for changes in world.users.applied), "a new app has no cut-off"


async def test_signing_out_everywhere_stamps_the_cut_off_where_the_row_has_one(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    pair = await world.sign_in(user)
    await world.call("POST", "/auth/logout", token=pair["access_token"])
    assert user.sessions_invalid_before == world.clock()  # type: ignore[attr-defined]


# --- the profile -------------------------------------------------------------------------------


async def test_the_profile_writes_what_the_body_sent(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    response = await world.call(
        "PATCH", "/auth/me", token=token, json={"last_name": "Byron", "locale": "fr", "text_size": "large"}
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["first_name"], body["last_name"], body["locale"], body["text_size"]) == ("Ada", "Byron", "fr", "large")
    applied = len(world.users.applied)
    assert (await world.call("PATCH", "/auth/me", token=token, json={})).status_code == 200
    assert len(world.users.applied) == applied, "an empty body writes nothing"


async def test_a_demo_s_profile_keeps_its_picks_on_the_device(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account(is_demo=True)
    token = await world.token_of(user)
    await world.call("PATCH", "/auth/me", token=token, json={"first_name": "Grace", "locale": "fr"})
    assert (user.first_name, user.locale) == ("Grace", "en")


# --- the password change -----------------------------------------------------------------------


async def test_a_password_change_ends_every_other_session_and_answers_a_fresh_pair(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account(password_change_required_at=None)
    other = await world.sign_in(user)
    mine = await world.sign_in(user)
    ended: list[str] = []

    async def sessions_ended(account: MemoryUser, reason: str) -> None:
        ended.append(reason)

    world.hooks = AuthHooks(sessions_ended=sessions_ended)
    response = await world.call(
        "POST",
        "/auth/me/password",
        token=mine["access_token"],
        json={"current_password": PASSWORD, "new_password": "a new password"},
    )
    assert response.status_code == 200, response.text
    fresh = response.json()
    my_sid = world.signer.claims(mine["access_token"])["sid"]
    assert world.signer.claims(fresh["access_token"])["sid"] == my_sid
    assert [row.sid for row in world.sessions.of(user.id) if row.is_active] == [my_sid]
    assert ended == ["password_changed"] and user.password_hash == "hashed:a new password"
    assert (await world.call("GET", "/auth/me", token=fresh["access_token"])).status_code == 200
    for stale in (mine["access_token"], other["access_token"]):
        assert code(await world.call("GET", "/auth/me", token=stale)) == "session_ended"


async def test_a_wrong_or_unchanged_password_is_a_400_never_a_401(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    for body, expected in (
        ({"current_password": "wrong", "new_password": "a new password"}, "password_incorrect"),
        ({"current_password": PASSWORD, "new_password": PASSWORD}, "password_unchanged"),
    ):
        response = await world.call("POST", "/auth/me/password", token=token, json=body)
        assert response.status_code == 400 and code(response) == expected


async def test_password_checks_are_limited_per_account(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    for _ in range(10):
        await world.call(
            "POST", "/auth/me/password", token=token, json={"current_password": "wrong", "new_password": "a new one!"}
        )
    response = await world.call(
        "POST", "/auth/me/password", token=token, json={"current_password": PASSWORD, "new_password": "a new one!"}
    )
    assert response.status_code == 429


async def test_password_changing_may_refuse_or_ride_on_the_answer() -> None:
    from eifi1_server_kit.auth import TokenResponse, UserResponse
    from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode

    class Answer(TokenResponse[UserResponse]):
        key_wrap_unchanged: bool | None = None

    world = World(token_model=Answer)
    async with world.client:
        user = world.account()
        token = await world.token_of(user)
        refuse = True

        async def password_changing(account: MemoryUser, body: Any) -> dict[str, Any]:
            if refuse:
                raise AuthError(AuthErrorCode.PASSWORD_UNCHANGED, "Re-seal the key first")
            return {"key_wrap_unchanged": True}

        world.hooks = AuthHooks(password_changing=password_changing)
        body = {"current_password": PASSWORD, "new_password": "a new password"}
        response = await world.call("POST", "/auth/me/password", token=token, json=body)
        assert response.status_code == 400 and user.password_hash == f"hashed:{PASSWORD}"
        refuse = False
        response = await world.call("POST", "/auth/me/password", token=token, json=body)
        assert response.json()["key_wrap_unchanged"] is True


async def test_the_never_list_takes_the_app_s_demo_guard_and_the_service_s_own() -> None:
    """The app's guard runs first (its route walk finds it); the service refuses a demo too."""
    world: World

    def not_in_the_demo(what: str) -> Any:
        async def check(user: Annotated[Any, Depends(current_user(world.deps))]) -> None:
            refuse_demo(getattr(user, "is_demo", False), what)

        return check

    world = World(cutoff=True, demo_guard=not_in_the_demo)
    async with world.client:
        demo = world.account(is_demo=True)
        token = await world.token_of(demo)
        body = {"current_password": PASSWORD, "new_password": "a new password"}
        response = await world.call("POST", "/auth/me/password", token=token, json=body)
        assert response.status_code == 403 and code(response) == "demo_refused"
    plain = World(cutoff=True)
    async with plain.client:
        demo = plain.account(is_demo=True)
        token = await plain.token_of(demo)
        response = await plain.call("POST", "/auth/me/password", token=token, json=body)
        assert response.status_code == 403 and code(response) == "demo_refused"


async def test_every_account_and_admin_route_shares_one_window_per_address() -> None:
    """0.31 (Staunlabor's pilot): /me, the sessions, the passkey sign-in's begin and the
    admin's lists had no window of their own; one per address now bounds them all."""
    from eifi1_server_kit.auth import AuthLimiters, Budget
    from tests.auth._world import World

    world = World()
    async with world.client:
        admin = world.account(email="root@example.com", role="ADMIN")
        token = await world.token_of(admin)
        world.app.state.auth_limiters = AuthLimiters(requests=Budget(3, 60))
        for path in ("/auth/me", "/auth/sessions", "/admin/invitations"):
            assert (await world.call("GET", path, token=token)).status_code == 200, path
        refused = await world.call("GET", "/auth/me", token=token)
        assert refused.status_code == 429 and refused.headers["Retry-After"]
        world.app.state.auth_limiters = AuthLimiters(requests=None)
        for _ in range(5):
            assert (await world.call("GET", "/auth/me", token=token)).status_code == 200
