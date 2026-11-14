"""``AccountService.start_session``: a session for an account an app just made, with a fixed
end (auth §12.6; Kurvenschmiede's ``demo_router``, keksdose's 60-minute demo), and the row's
``expires_at`` as the session's end on the server."""

from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import AuthError, AuthErrorCode, ProfileUpdate, RefreshRequest, UserResponse
from eifi1_server_kit.auth.ports import SessionRow
from eifi1_server_kit.auth.service import AuthHooks, StartedSession
from eifi1_server_kit.auth.sessions import SessionPolicy
from tests.auth._world import World
from tests.auth_fakes import MemorySessionStore, MemoryUser


async def test_a_demo_s_session_ends_exactly_when_the_demo_does_and_has_no_refresh_token(world: World) -> None:
    user = world.account(email="demo@example.com")
    end = world.clock() + timedelta(minutes=60)
    started = await world.service().start_session(user, expires_at=end, ip="198.51.100.9", device="Firefox")
    assert isinstance(started, StartedSession)
    assert started.refresh_token is None and started.expires_at == end
    claims = world.signer.claims(started.access_token)
    assert claims["type"] == "access" and claims["sid"] == started.session.sid
    assert claims["exp"] == int(end.timestamp())
    (row,) = world.sessions.of(user.id)
    assert (row.expires_at, row.started_at, row.shared_device) == (end, world.clock(), False)
    assert (row.ip_address, row.device) == ("198.51.100.9", "Firefox")
    assert user.last_login_at is None, "the app's own sign-in stamp, not the kit's"
    world.clock.advance(minutes=59, seconds=59)
    assert (await world.service().authenticate(started.access_token)).session.sid == row.sid
    world.clock.advance(seconds=1)
    with pytest.raises(AuthError) as refused:
        await world.service().authenticate(started.access_token)
    assert refused.value.code is AuthErrorCode.NOT_SIGNED_IN


async def test_nothing_but_a_refresh_moves_a_session_s_end(world: World) -> None:
    """The pin: ``touch`` is the only write to ``expires_at``, and only ``/auth/refresh``
    calls it, so a session without a refresh token keeps the end it started with."""

    class Watched(MemorySessionStore):
        touched = 0

        async def touch(self, session: Any, *, last_active: Any, expires_at: Any) -> None:
            Watched.touched += 1
            await super().touch(session, last_active=last_active, expires_at=expires_at)

    world.sessions = Watched()
    user = world.account(email="demo@example.com")
    end = world.clock() + timedelta(minutes=60)
    service = world.service()
    started = await service.start_session(user, expires_at=end)
    signed = await service.authenticate(started.access_token)
    await service.sessions_of(signed)
    await service.update_profile(user, ProfileUpdate(first_name="Demo"))
    await service.me(user, UserResponse)
    world.clock.advance(minutes=30)
    await service.authenticate(started.access_token)
    assert Watched.touched == 0
    assert world.sessions.rows[started.session.sid].expires_at == end


async def test_a_session_whose_row_has_ended_is_over_whatever_the_policy_says(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    (row,) = world.sessions.of(user.id)
    row.expires_at = world.clock()
    with pytest.raises(AuthError) as refused:
        await world.service().authenticate(token)
    assert refused.value.code is AuthErrorCode.SESSION_ENDED


async def test_the_sessions_list_leaves_out_a_session_whose_row_has_ended(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    other = await world.service().start_session(user, expires_at=world.clock() + timedelta(minutes=5))
    signed = await world.service().authenticate(token)
    listed = {info.id for info in await world.service().sessions_of(signed)}
    assert other.session.sid in listed
    world.clock.advance(minutes=5)
    listed = {info.id for info in await world.service().sessions_of(signed)}
    assert other.session.sid not in listed and len(listed) == 1


async def test_with_a_refresh_token_the_session_still_never_outlives_its_end(world: World) -> None:
    user = world.account()
    end = world.clock() + timedelta(hours=2)
    service = world.service()
    started = await service.start_session(user, expires_at=end, refresh=True)
    assert started.refresh_token is not None and started.expires_at == end
    access = world.signer.claims(started.access_token)
    assert access["exp"] == int(end.timestamp()), "the policy's 24 hours, cut to the session's end"
    world.clock.advance(minutes=90)
    renewed = await service.refresh(RefreshRequest(refresh_token=started.refresh_token), ip="198.51.100.1")
    row = world.sessions.rows[started.session.sid]
    assert row.expires_at == end, "a refresh moves the end on, never past the fixed one"
    assert world.signer.claims(renewed.refresh_token)["exp"] == int(end.timestamp())
    assert world.signer.claims(renewed.access_token)["exp"] == int(end.timestamp())
    world.clock.advance(minutes=30)
    with pytest.raises(AuthError) as refused:
        await service.refresh(RefreshRequest(refresh_token=renewed.refresh_token), ip="198.51.100.1")
    assert refused.value.code is AuthErrorCode.SESSION_ENDED


async def test_an_ordinary_refresh_still_slides(world: World) -> None:
    user = world.account()
    answer = await world.sign_in(user)
    world.clock.advance(days=1)
    renewed = await world.service().refresh(RefreshRequest(refresh_token=answer["refresh_token"]), ip="ip")
    (row,) = world.sessions.of(user.id)
    assert row.expires_at == world.clock() + timedelta(days=30)
    assert "end" not in world.signer.claims(renewed.refresh_token)


async def test_a_shared_session_without_a_refresh_token_ends_at_the_shared_idle_limit(world: World) -> None:
    """Nothing moves a shared session's idle limit but a refresh, so one started without a
    refresh token ends there, before a later ``expires_at``; its token says so too."""
    user = world.account()
    started = await world.service().start_session(
        user, expires_at=world.clock() + timedelta(hours=1), shared_device=True
    )
    idle_end = world.clock() + SessionPolicy().shared_idle
    assert started.expires_at == idle_end
    assert world.sessions.rows[started.session.sid].expires_at == idle_end
    assert world.signer.claims(started.access_token)["exp"] == int(idle_end.timestamp())


async def test_the_app_s_claims_ride_on_a_started_session_s_token(world: World) -> None:
    async def claims(account: MemoryUser, session: SessionRow) -> dict[str, Any]:
        return {"company_id": 7}

    started = await world.service(AuthHooks(claims=claims)).start_session(
        world.account(), expires_at=world.clock() + timedelta(minutes=10)
    )
    assert world.signer.claims(started.access_token)["company_id"] == 7


async def test_an_end_that_has_passed_is_refused(world: World) -> None:
    with pytest.raises(ValueError, match="ahead"):
        await world.service().start_session(world.account(), expires_at=world.clock())


async def test_a_naive_end_is_utc(world: World) -> None:
    end = world.clock() + timedelta(minutes=10)
    started = await world.service().start_session(world.account(), expires_at=end.replace(tzinfo=None))
    assert started.expires_at == end
