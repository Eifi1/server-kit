"""The account service's parts that no route reaches: its policy objects, the admin's
withdrawal of a deletion (0.30's route), and the guards a caller builds without a router."""

from dataclasses import replace
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI

from eifi1_server_kit.auth import AuthLimiters
from eifi1_server_kit.auth.router import AuthDeps, account_service, admin_guard, auth_router, peer_address
from eifi1_server_kit.auth.service import AccountLinks, AccountService, AuthHooks
from eifi1_server_kit.errors import install_contract_error_handlers
from tests.auth._world import POLICY, World
from tests.auth_fakes import MemoryUser


def test_the_links_are_the_app_s_pages_on_its_origin() -> None:
    links = AccountLinks(base_url="https://app.example/")
    assert links.url("/reset-password", token="a b") == "https://app.example/reset-password?token=a+b"
    assert links.url(links.home) == "https://app.example/"
    with pytest.raises(ValueError, match="http"):
        AccountLinks(base_url="app.example")


@pytest.mark.parametrize(
    ("changes", "message"), [({"app": "Staun Labor"}, "lower case"), ({"deletion_days": -1}, "zero")]
)
def test_a_policy_that_cannot_work_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(POLICY, **changes)


async def test_an_admin_withdraws_a_deletion_before_the_erasure(world: World) -> None:
    user = world.account(is_active=False, deletion_requested_at=world.clock(), deletion_scheduled_at=world.clock())
    admin = world.account(email="root@example.com", role="ADMIN")
    told: list[int] = []

    async def on_deletion_withdrawn(account: MemoryUser) -> dict[str, Any] | None:
        told.append(account.id)
        return {"plan_restored": True} if len(told) == 1 else None

    service = service_of(world, AuthHooks(on_deletion_withdrawn=on_deletion_withdrawn))
    await service.withdraw_deletion(user, actor_id=admin.id)
    assert user.is_active and user.deletion_requested_at is None and user.deletion_scheduled_at is None
    assert told == [user.id] and world.log.actions() == ["deletion_cancel"]
    assert world.log.rows[-1].detail == {"plan_restored": True}, "the hook's answer is the row's detail (G3)"
    await service.withdraw_deletion(user, actor_id=admin.id)
    assert world.log.rows[-1].detail == {}, "None: no detail"
    await service_of(world).withdraw_deletion(user, actor_id=admin.id)
    assert told == [user.id, user.id] and world.log.rows[-1].detail == {}


async def test_without_a_passkey_store_the_ceremonies_refuse_to_run(world: World) -> None:
    service = service_of(world)
    with pytest.raises(RuntimeError, match="PasskeyStore"):
        await service.begin_passkey_login()


async def test_the_hooks_are_optional_at_every_step(world: World) -> None:
    """An invitation attaches with no hook to tell; the default absent check is built."""
    from eifi1_server_kit.auth import OneTimeTokenKind, mint

    user = world.account()
    raw, digest = mint()
    await world.tokens.put(
        OneTimeTokenKind.EMAIL_CHANGE,
        user_id=user.id,
        token_hash=digest,
        email="new@example.com",
        expires_at=world.clock().replace(year=2027),
    )
    world.invitations.seed(email="new@example.com")
    service = service_of(world)
    assert (await service.confirm_email_change(raw, ip="ip")).invitations_attached == 1
    invite_raw, invite_digest = mint()
    world.invitations.seed(email=user.email, token_hash=invite_digest)
    invitation, extras = await service.accept_invitation(user, invite_raw, ip="ip")
    assert invitation.accepted_by_user_id == user.id and extras == {}


def service_of(world: World, hooks: AuthHooks[Any, Any] | None = None) -> AccountService[Any, Any, Any]:
    return AccountService(
        users=world.users,
        sessions=world.sessions,
        tokens=world.tokens,
        invitations=world.invitations,
        log=world.log,
        after_commit=world.after,
        signer=world.signer,
        hasher=world.hasher,
        mailer=world.mailer,
        policy=POLICY,
        limiters=AuthLimiters(),
        hooks=hooks,
        now=world.clock,
    )


async def test_an_app_s_own_routes_reuse_the_service_and_the_guard() -> None:
    """The defaults too: no hooks, the TCP peer as the caller's address."""
    world = World()
    deps = replace_deps(world.deps)
    app = FastAPI()
    install_contract_error_handlers(app)
    app.include_router(auth_router(deps), prefix="/api/v1")
    app.state.auth_limiters = AuthLimiters()

    Accounts = Annotated[Any, Depends(account_service(deps))]  # noqa: N806

    @app.get("/api/v1/admin/ping", dependencies=[Depends(admin_guard(deps))])
    async def ping(accounts: Accounts) -> dict[str, bool]:
        return {"service": isinstance(accounts, AccountService)}

    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin = world.account(role="ADMIN")
        member = world.account(email="bob@example.com")
        for person, status in ((admin, 200), (member, 403)):
            response = await client.post(
                "/api/v1/auth/login", json={"email": person.email, "password": "correct horse"}
            )
            token = response.json()["access_token"]
            answer = await client.get("/api/v1/admin/ping", headers={"Authorization": f"Bearer {token}"})
            assert answer.status_code == status
        assert world.sessions.of(admin.id)[0].ip_address == "127.0.0.1"


def replace_deps(deps: AuthDeps) -> AuthDeps:
    values = {name: getattr(deps, name) for name in AuthDeps.__dataclass_fields__}
    values.update(hooks=AuthDeps.__dataclass_fields__["hooks"].default, client_address=peer_address)
    return AuthDeps(**values)


def test_peer_address_answers_unknown_without_a_peer() -> None:
    from starlette.requests import Request

    assert peer_address(Request({"type": "http", "headers": [], "client": None})) == "unknown"
