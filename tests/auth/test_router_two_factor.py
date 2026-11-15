"""Two-factor sign-in (0.32, keksdose's KG-1): the setup, its confirmation, the code step at
sign-in, a code accepted once, switching it off, and the sealed secret a server cannot open.
keksdose's rules exactly (its ``auth_router`` ``/2fa/*`` and ``/login/2fa``).

No ``from __future__ import annotations``: the hooks below are read at run time.
"""

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Depends

from eifi1_server_kit.auth import AuthLimiters
from eifi1_server_kit.auth.limits import Budget
from eifi1_server_kit.auth.ports import TwoFactorState
from eifi1_server_kit.auth.router import AuthDeps, auth_router, current_user
from eifi1_server_kit.auth.schemas import TwoFactorLogin
from eifi1_server_kit.auth.seal import SecretBox, totp_pending_secret_aad, totp_secret_aad
from eifi1_server_kit.auth.service import AuthHooks
from eifi1_server_kit.auth.testing import InMemoryTwoFactorStore, totp_code
from eifi1_server_kit.auth.totp import TOTP_STEP_SECONDS
from eifi1_server_kit.demo import refuse_demo
from tests.auth._world import ORIGIN, PASSWORD, World
from tests.auth.soft_webauthn import SoftAuthenticator
from tests.auth.test_router_passkeys import add_passkey
from tests.auth.test_router_passkeys import sign_in as passkey_sign_in
from tests.auth_fakes import MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


@pytest.fixture
async def tf() -> AsyncIterator[World]:
    built = World(two_factor=True)
    async with built.client:
        yield built


def next_step(world: World) -> None:
    """On to the next 30 seconds: the code just accepted is spent."""
    world.clock.advance(seconds=TOTP_STEP_SECONDS)


async def enrol_codes(world: World, user: MemoryUser) -> tuple[str, str, list[str]]:
    """2FA switched on for ``user``: its secret, a session token from before, its backup codes."""
    token = await world.token_of(user)
    response = await world.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    secret: str = response.json()["secret"]
    response = await world.call(
        "POST", "/auth/me/2fa/enable", token=token, json={"code": totp_code(secret, now=world.clock())}
    )
    assert response.status_code == 200, response.text
    codes: list[str] = response.json()["backup_codes"]
    next_step(world)
    return secret, token, codes


async def enrol(world: World, user: MemoryUser) -> tuple[str, str]:
    """2FA switched on for ``user``: its secret and a session token from before."""
    secret, token, _ = await enrol_codes(world, user)
    return secret, token


async def challenge(world: World, user: MemoryUser, **body: Any) -> str:
    answer = await world.sign_in(user, **body)
    assert answer == {"requires_2fa": True, "challenge_token": answer["challenge_token"]}
    token: str = answer["challenge_token"]
    return token


async def redeem(world: World, token: str, secret: str, **headers: str) -> Any:
    return await world.call(
        "POST",
        "/auth/login/2fa",
        json={"challenge_token": token, "code": totp_code(secret, now=world.clock())},
        headers=headers,
    )


# --- setting it up --------------------------------------------------------------------------------


async def test_the_setup_asks_the_password_and_changes_nothing_until_a_code_confirms_it(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    response = await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": "wrong"})
    assert response.status_code == 400 and code(response) == "password_incorrect"
    response = await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    secret = body["secret"]
    assert len(secret) == 32 and secret.isalnum() and secret.upper() == secret
    uri = urlsplit(body["otpauth_url"])
    assert (uri.scheme, uri.netloc, uri.path) == ("otpauth", "totp", "/Staunlabor:ada%40example.com")
    assert parse_qs(uri.query) == {"secret": [secret], "issuer": ["Staunlabor"]}
    state = tf.two_factor_store.states[user.id]
    assert state.enabled is False and state.secret is None and state.pending_secret is not None
    assert secret not in state.pending_secret
    assert tf.seal.unseal(state.pending_secret, aad=totp_pending_secret_aad(user.id)) == secret
    # Nothing signs in differently, and the account says 2FA is off.
    assert "access_token" in await tf.sign_in(user)
    assert (await tf.call("GET", "/auth/me", token=token)).json()["totp_enabled"] is False


async def test_a_code_from_the_pending_secret_switches_it_on(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "123456"})
    assert response.status_code == 404 and code(response) == "two_factor_not_pending"
    secret = (await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})).json()["secret"]
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "000000"})
    assert response.status_code == 400 and code(response) == "two_factor_invalid"
    assert tf.two_factor_store.states[user.id].enabled is False
    now = tf.clock()
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": totp_code(secret, now=now)})
    assert response.status_code == 200, response.text
    state = tf.two_factor_store.states[user.id]
    assert state.enabled is True and state.pending_secret is None
    assert state.secret is not None and tf.seal.unseal(state.secret, aad=totp_secret_aad(user.id)) == secret
    # The confirmation spends its step: the first sign-in waits for the next code.
    assert state.last_step == int(now.timestamp()) // TOTP_STEP_SECONDS
    me = (await tf.call("GET", "/auth/me", token=token)).json()
    assert me["totp_enabled"] is True


async def test_a_wrong_confirmation_is_charged_to_the_password_window() -> None:
    world = World(two_factor=True)
    async with world.client:
        world.app.state.auth_limiters = AuthLimiters(password_check=Budget(2, 60))
        user = world.account()
        token = await world.token_of(user)
        await world.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
        response = await world.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "000000"})
        assert code(response) == "two_factor_invalid"
        response = await world.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "000000"})
        assert response.status_code == 429


async def test_without_a_key_nothing_is_set_up() -> None:
    world = World(two_factor=True, secret_seal=SecretBox(""))
    async with world.client:
        token = await world.token_of(world.account())
        response = await world.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
        assert response.status_code == 503 and code(response) == "two_factor_unavailable"


# --- the code at sign-in --------------------------------------------------------------------------


async def test_the_sign_in_asks_the_code_and_only_its_answer_starts_the_session(tf: World) -> None:
    user = tf.account()
    secret, _ = await enrol(tf, user)
    sessions = len(tf.sessions.rows)
    before = user.last_login_at
    token = await challenge(tf, user)
    assert len(tf.sessions.rows) == sessions and user.last_login_at == before
    response = await redeem(tf, token, secret)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["id"] == user.id and body["user"]["totp_enabled"] is True
    assert len(tf.sessions.rows) == sessions + 1 and user.last_login_at == tf.clock()


async def test_a_code_signs_in_once_and_a_challenge_redeems_once(tf: World) -> None:
    user = tf.account()
    secret, _ = await enrol(tf, user)
    first = await challenge(tf, user)
    second = await challenge(tf, user)
    assert (await redeem(tf, first, secret)).status_code == 200
    # The same code with another challenge: the step is spent.
    response = await redeem(tf, second, secret)
    assert response.status_code == 400 and code(response) == "token_invalid"
    third = await challenge(tf, user)
    response = await redeem(tf, third, secret)
    assert response.status_code == 400 and code(response) == "two_factor_invalid"
    # The redeemed challenge with the next code: it is spent too.
    next_step(tf)
    response = await redeem(tf, first, secret)
    assert response.status_code == 400 and code(response) == "token_invalid"
    assert (await redeem(tf, third, secret)).status_code == 200


async def test_a_wrong_code_keeps_the_challenge(tf: World) -> None:
    user = tf.account()
    secret, _ = await enrol(tf, user)
    token = await challenge(tf, user)
    for typed in ("000000", "12345a", "١٢٣٤٥٦"):
        response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": token, "code": typed})
        assert response.status_code == 400 and code(response) == "two_factor_invalid"
    # A phone a step behind still signs in (the enrolment spent the step before it); spaces
    # and full-width digits read as digits.
    next_step(tf)
    typed = totp_code(secret, now=tf.clock() - timedelta(seconds=TOTP_STEP_SECONDS))
    wide = "".join(chr(ord(digit) + 0xFEE0) for digit in typed)
    response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": token, "code": f" {wide} "})
    assert response.status_code == 200, response.text


async def test_a_dead_challenge_is_token_invalid(tf: World) -> None:
    user = tf.account()
    secret, session = await enrol(tf, user)
    expired = await challenge(tf, user)
    tf.clock.advance(minutes=5, seconds=1)
    assert code(await redeem(tf, expired, secret)) == "token_invalid"
    # Not a 2FA challenge: an access token.
    assert code(await redeem(tf, session, secret)) == "token_invalid"
    assert code(await redeem(tf, "not a token", secret)) == "token_invalid"
    # Deactivated, or 2FA switched off, between the steps.
    token = await challenge(tf, user)
    user.is_active = False
    assert code(await redeem(tf, token, secret)) == "token_invalid"
    user.is_active = True
    tf.two_factor_store.states[user.id] = TwoFactorState()
    assert code(await redeem(tf, token, secret)) == "token_invalid"
    # Its account gone.
    other = tf.account(email="grace@example.com")
    other_secret, _ = await enrol(tf, other)
    token = await challenge(tf, other)
    del tf.users.rows[other.id]
    assert code(await redeem(tf, token, other_secret)) == "token_invalid"


async def test_after_the_code_the_forced_change_comes_and_the_shared_choice_rides_along(tf: World) -> None:
    """keksdose's order: active, 2FA on, spent, the code, THEN the forced change — a 2FA
    account clears its second factor before it may set a new password."""
    user = tf.account()
    secret, _ = await enrol(tf, user)
    user.password_change_required_at = tf.clock()
    events: list[str] = []

    async def bind_anonymous(account: MemoryUser) -> None:
        events.append("bind")

    async def challenge_extras(account: MemoryUser) -> dict[str, Any]:
        events.append("challenge_extras")
        return {}

    tf.hooks = AuthHooks(bind_anonymous=bind_anonymous, challenge_extras=challenge_extras)
    token = await challenge(tf, user, shared_device=True)
    assert events == ["bind"]
    response = await redeem(tf, token, secret)
    assert response.json() == {"requires_password_change": True, "challenge_token": response.json()["challenge_token"]}
    assert events == ["bind", "bind", "challenge_extras"]
    response = await tf.call(
        "POST",
        "/auth/login/set-password",
        json={"challenge_token": response.json()["challenge_token"], "new_password": "a new password"},
    )
    assert response.status_code == 200, response.text
    (session,) = [row for row in tf.sessions.rows.values() if row.user_id == user.id and row.shared_device]
    assert session is not None


async def test_the_shared_computer_s_session_after_the_code(tf: World) -> None:
    user = tf.account()
    secret, _ = await enrol(tf, user)
    token = await challenge(tf, user, shared_device=True)
    assert (await redeem(tf, token, secret)).status_code == 200
    newest = max(tf.sessions.rows.values(), key=lambda row: row.started_at)
    assert newest.shared_device is True


async def test_a_passkey_sign_in_counts_as_both_factors(tf: World) -> None:
    user = tf.account()
    _, token = await enrol(tf, user)
    device = SoftAuthenticator(ORIGIN)
    assert (await add_passkey(tf, token, device)).status_code == 201
    response = await passkey_sign_in(tf, device)
    assert response.status_code == 200 and "access_token" in response.json()


async def test_the_code_step_is_limited_per_address_and_per_account() -> None:
    world = World(two_factor=True)
    async with world.client:
        world.app.state.auth_limiters = AuthLimiters(challenge=Budget(2, 60), challenge_subject=Budget(3, 60))
        user = world.account()
        await enrol(world, user)
        token = await challenge(world, user)
        wrong = {"challenge_token": token, "code": "000000"}
        assert code(await world.call("POST", "/auth/login/2fa", json=wrong)) == "two_factor_invalid"
        assert code(await world.call("POST", "/auth/login/2fa", json=wrong)) == "two_factor_invalid"
        assert (await world.call("POST", "/auth/login/2fa", json=wrong)).status_code == 429
        # Another address: the account's own window is the one a header cannot re-key.
        moved = {"x-test-ip": "203.0.113.9"}
        response = await world.call("POST", "/auth/login/2fa", json=wrong, headers=moved)
        assert code(response) == "two_factor_invalid"
        assert (await world.call("POST", "/auth/login/2fa", json=wrong, headers=moved)).status_code == 429


# --- a secret this server cannot open ---------------------------------------------------------------


async def test_an_unreadable_secret_is_a_503_at_the_door_and_at_the_code(tf: World) -> None:
    user = tf.account()
    secret, _ = await enrol(tf, user)
    token = await challenge(tf, user)
    state = tf.two_factor_store.states[user.id]
    rotated = SecretBox("another-key").seal(secret, aad=totp_secret_aad(user.id))
    tf.two_factor_store.states[user.id] = TwoFactorState(enabled=True, secret=rotated, last_step=state.last_step)
    response = await tf.call("POST", "/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 503 and code(response) == "two_factor_unavailable"
    response = await redeem(tf, token, secret)
    assert response.status_code == 503 and code(response) == "two_factor_unavailable"
    # On with no secret at all is the same broken row.
    tf.two_factor_store.states[user.id] = TwoFactorState(enabled=True)
    response = await tf.call("POST", "/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 503 and code(response) == "two_factor_unavailable"


async def test_an_unreadable_pending_secret_is_a_503_at_its_confirmation(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
    pending = SecretBox("another-key").seal("JBSWY3DPEHPK3PXP", aad=totp_pending_secret_aad(user.id))
    tf.two_factor_store.states[user.id] = TwoFactorState(pending_secret=pending)
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "123456"})
    assert response.status_code == 503 and code(response) == "two_factor_unavailable"


# --- setting it up again, switching it off ----------------------------------------------------------


async def test_a_new_setup_keeps_the_old_factor_until_its_code_confirms(tf: World) -> None:
    """keksdose's F-4: a re-enrolment walked away from leaves the old factor working."""
    user = tf.account()
    old, token = await enrol(tf, user)
    new = (await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})).json()["secret"]
    assert (await redeem(tf, await challenge(tf, user), old)).status_code == 200
    next_step(tf)
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": totp_code(new, now=tf.clock())})
    assert response.status_code == 200
    next_step(tf)
    assert code(await redeem(tf, await challenge(tf, user), old)) == "two_factor_invalid"
    assert (await redeem(tf, await challenge(tf, user), new)).status_code == 200


async def test_switching_it_off_takes_the_password_and_a_live_code(tf: World) -> None:
    user = tf.account()
    secret, token = await enrol(tf, user)
    body = {"password": PASSWORD, "code": totp_code(secret, now=tf.clock())}
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json={**body, "password": "wrong"})
    assert response.status_code == 400 and code(response) == "password_incorrect"
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json={**body, "code": "000000"})
    assert response.status_code == 400 and code(response) == "two_factor_invalid"
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json=body)
    assert response.status_code == 204
    assert tf.two_factor_store.states[user.id] == TwoFactorState()
    assert "access_token" in await tf.sign_in(user)


async def test_a_spent_code_does_not_switch_it_off(tf: World) -> None:
    user = tf.account()
    secret, token = await enrol(tf, user)
    assert (await redeem(tf, await challenge(tf, user), secret)).status_code == 200
    body = {"password": PASSWORD, "code": totp_code(secret, now=tf.clock())}
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json=body)
    assert code(response) == "two_factor_invalid"


async def test_the_password_alone_switches_off_a_secret_this_server_cannot_open(tf: World) -> None:
    """keksdose: demanding a code this server cannot check would leave it on for good, and the
    password already replaces the secret through a new setup."""
    user = tf.account()
    _, token = await enrol(tf, user)
    unreadable = SecretBox("another-key").seal("JBSWY3DPEHPK3PXP", aad=totp_secret_aad(user.id))
    tf.two_factor_store.states[user.id] = TwoFactorState(enabled=True, secret=unreadable, last_step=1)
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json={"password": PASSWORD, "code": "000000"})
    assert response.status_code == 204
    assert tf.two_factor_store.states[user.id] == TwoFactorState()


async def test_switching_off_what_is_off_drops_a_pending_setup(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    await tf.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})
    response = await tf.call("POST", "/auth/me/2fa/disable", token=token, json={"password": PASSWORD, "code": "000000"})
    assert response.status_code == 204
    assert tf.two_factor_store.states[user.id] == TwoFactorState()
    response = await tf.call("POST", "/auth/me/2fa/enable", token=token, json={"code": "123456"})
    assert code(response) == "two_factor_not_pending"


# --- the demo, the mounting ---------------------------------------------------------------------------


async def test_a_demo_never_sets_up_two_factor() -> None:
    world: World

    def not_in_the_demo(what: str) -> Any:
        async def check(user: Annotated[Any, Depends(current_user(world.deps))]) -> None:
            refuse_demo(getattr(user, "is_demo", False), what)

        return check

    world = World(cutoff=True, two_factor=True, demo_guard=not_in_the_demo)
    async with world.client:
        token = await world.token_of(world.account(is_demo=True))
        for path, body in (
            ("/auth/me/2fa/setup", {"password": PASSWORD}),
            ("/auth/me/2fa/enable", {"code": "123456"}),
            ("/auth/me/2fa/disable", {"password": PASSWORD, "code": "123456"}),
            ("/auth/me/2fa/backup-codes", {"password": PASSWORD}),
        ):
            response = await world.call("POST", path, token=token, json=body)
            assert response.status_code == 403 and code(response) == "demo_refused", path
    plain = World(cutoff=True, two_factor=True)
    async with plain.client:
        token = await plain.token_of(plain.account(is_demo=True))
        for path, body in (
            ("/auth/me/2fa/setup", {"password": PASSWORD}),
            ("/auth/me/2fa/enable", {"code": "123456"}),
            ("/auth/me/2fa/disable", {"password": PASSWORD, "code": "123456"}),
        ):
            response = await plain.call("POST", path, token=token, json=body)
            assert response.status_code == 403 and code(response) == "demo_refused", path


async def test_without_a_store_there_are_no_two_factor_routes(world: World) -> None:
    token = await world.token_of(world.account())
    assert (await world.call("POST", "/auth/me/2fa/setup", token=token, json={"password": PASSWORD})).status_code == 404
    response = await world.call("POST", "/auth/login/2fa", json={"challenge_token": "x", "code": "123456"})
    assert response.status_code == 404
    assert (await world.call("GET", "/auth/me", token=token)).json()["totp_enabled"] is False
    with pytest.raises(RuntimeError, match="two_factor and secret_seal"):
        await world.service().login_two_factor(TwoFactorLogin(challenge_token="x", code="123456"), ip="198.51.100.1")


async def test_the_store_and_the_seal_come_together(world: World) -> None:
    values = {name: getattr(world.deps, name) for name in AuthDeps.__dataclass_fields__}
    for missing in ("two_factor", "secret_seal"):
        lone = {
            **values,
            "two_factor": lambda: InMemoryTwoFactorStore(),
            "secret_seal": SecretBox("k"),
            missing: None,
        }
        with pytest.raises(ValueError, match="two_factor and secret_seal come together"):
            auth_router(AuthDeps(**lone))


# --- backup codes (0.32, Marcel 2026-11-14: a lost phone with no passkey must not lock out) -------


async def test_switching_on_answers_ten_backup_codes_once_kept_as_digests(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    status = await tf.call("GET", "/auth/me/2fa", token=token)
    assert status.json() == {"enabled": False, "backup_codes_left": 0}
    _, token, codes = await enrol_codes(tf, user)
    assert len(codes) == 10 and len(set(codes)) == 10
    for code_ in codes:
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}", code_), code_
    state = tf.two_factor_store.states[user.id]
    assert len(state.backup_codes) == 10
    assert not any(
        code_ in stored or code_.replace("-", "") in stored for code_ in codes for stored in state.backup_codes
    )
    status = await tf.call("GET", "/auth/me/2fa", token=token)
    assert status.json() == {"enabled": True, "backup_codes_left": 10}


async def test_a_backup_code_signs_in_once_in_place_of_the_code(tf: World) -> None:
    user = tf.account()
    _, token, codes = await enrol_codes(tf, user)
    challenge_ = await challenge(tf, user)
    typed = f" {codes[0].lower().replace('-', ' ')} "
    response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": challenge_, "code": typed})
    assert response.status_code == 200, response.text
    # Spent, and its challenge with it.
    response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": challenge_, "code": codes[1]})
    assert code(response) == "token_invalid"
    again = await challenge(tf, user)
    response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": again, "code": codes[0]})
    assert code(response) == "two_factor_invalid"
    response = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": again, "code": "ABCD-EFGH-JKLM-NPQR"})
    assert code(response) == "two_factor_invalid"
    assert (
        await tf.call("POST", "/auth/login/2fa", json={"challenge_token": again, "code": codes[1]})
    ).status_code == 200
    assert (await tf.call("GET", "/auth/me/2fa", token=token)).json()["backup_codes_left"] == 8


async def test_new_backup_codes_take_the_password_and_end_the_old_ones(tf: World) -> None:
    user = tf.account()
    token = await tf.token_of(user)
    response = await tf.call("POST", "/auth/me/2fa/backup-codes", token=token, json={"password": PASSWORD})
    assert response.status_code == 409 and code(response) == "two_factor_not_enabled"
    _, token, old = await enrol_codes(tf, user)
    response = await tf.call("POST", "/auth/me/2fa/backup-codes", token=token, json={"password": "wrong"})
    assert code(response) == "password_incorrect"
    response = await tf.call("POST", "/auth/me/2fa/backup-codes", token=token, json={"password": PASSWORD})
    assert response.status_code == 200
    new = response.json()["backup_codes"]
    assert len(new) == 10 and not set(new) & set(old)
    response = await tf.call(
        "POST", "/auth/login/2fa", json={"challenge_token": await challenge(tf, user), "code": old[0]}
    )
    assert code(response) == "two_factor_invalid"
    response = await tf.call(
        "POST", "/auth/login/2fa", json={"challenge_token": await challenge(tf, user), "code": new[0]}
    )
    assert response.status_code == 200


async def test_switching_off_takes_the_backup_codes_with_it(tf: World) -> None:
    user = tf.account()
    secret, token, _ = await enrol_codes(tf, user)
    body = {"password": PASSWORD, "code": totp_code(secret, now=tf.clock())}
    assert (await tf.call("POST", "/auth/me/2fa/disable", token=token, json=body)).status_code == 204
    assert (await tf.call("GET", "/auth/me/2fa", token=token)).json() == {"enabled": False, "backup_codes_left": 0}


async def test_the_service_s_reset_of_an_account_with_2fa_off_changes_nothing(tf: World) -> None:
    user = tf.account()
    service = tf.service(two_factor=tf.two_factor_store, secret_seal=tf.seal)
    assert await service.reset_two_factor(user) is False
    assert tf.mailer.of("two_factor_reset_notice") == []
