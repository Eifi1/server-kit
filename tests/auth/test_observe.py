"""``AuthHooks.observe`` (0.32, keksdose's review of 0.32.0.dev4): every decision of the
signed-out flows and the refresh as a stable ``(event, outcome)`` pair, so an app counts its
sign-in funnel (keksdose's ``keksdose_auth_events_total(event, outcome)``) although the
answers fold several reasons into one code. Each pair the README's table names is recorded
here; an observer that raises changes no answer.

No ``from __future__ import annotations``: the hooks below are read at run time.
"""

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import AuthEvent, AuthLimiters, ChallengeKind, OneTimeTokenKind, challenge_claims
from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.limits import Budget
from eifi1_server_kit.auth.ports import TwoFactorState
from eifi1_server_kit.auth.schemas import RegisterRequest
from eifi1_server_kit.auth.seal import SecretBox, totp_secret_aad
from eifi1_server_kit.auth.service import AuthHooks
from tests.auth._world import ORIGIN, PASSWORD, World
from tests.auth.soft_webauthn import SoftAuthenticator
from tests.auth.test_reset_timing import FLOOR, TimedWorld, Timer
from tests.auth.test_router_invitations import invite
from tests.auth.test_router_links import link
from tests.auth.test_router_passkeys import add_passkey
from tests.auth.test_router_passkeys import sign_in as passkey_sign_in
from tests.auth.test_router_signin import REGISTER
from tests.auth.test_router_two_factor import challenge, enrol_codes, redeem

type Seen = list[tuple[Any, str]]


def observed(world: World, **hooks: Any) -> Seen:
    """The pairs the app's observer is handed, in order, with ``hooks`` beside it."""
    seen: Seen = []
    world.hooks = AuthHooks(observe=lambda event, outcome: seen.append((event, outcome)), **hooks)
    return seen


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


@pytest.fixture
async def tf() -> AsyncIterator[World]:
    built = World(two_factor=True)
    async with built.client:
        yield built


def test_the_events_are_stable_strings() -> None:
    assert [(event.name, event.value) for event in AuthEvent] == [
        ("REGISTER", "register"),
        ("LOGIN", "login"),
        ("LOGIN_2FA", "login_2fa"),
        ("LOGIN_SET_PASSWORD", "login_set_password"),
        ("PASSKEY_LOGIN", "passkey_login"),
        ("REFRESH", "refresh"),
        ("PASSWORD_RESET_REQUEST", "password_reset_request"),
        ("PASSWORD_RESET_CHECK", "password_reset_check"),
        ("PASSWORD_RESET_CONFIRM", "password_reset_confirm"),
        ("EMAIL_VERIFY", "email_verify"),
        ("EMAIL_CHANGE_CONFIRM", "email_change_confirm"),
        ("INVITATION_CHECK", "invitation_check"),
        ("INVITATION_ACCEPT", "invitation_accept"),
    ]
    assert all(isinstance(event, str) and str(event) == event.value for event in AuthEvent)


async def test_the_observer_is_handed_the_event_and_a_plain_string(world: World) -> None:
    seen = observed(world)
    await world.sign_in(world.account())
    ((event, outcome),) = seen
    assert type(event) is AuthEvent and event is AuthEvent.LOGIN
    assert type(outcome) is str and outcome == "ok"


# --- signing in ------------------------------------------------------------------------------------


async def test_a_password_sign_in_tells_the_reasons_its_one_refusal_folds(world: World) -> None:
    user = world.account()
    world.account(email="off@example.com", is_active=False)
    seen = observed(world)
    for email, password in (
        ("ada@example.com", "wrong"),
        ("nobody@example.com", PASSWORD),
        ("off@example.com", PASSWORD),
        ("off@example.com", "wrong"),
    ):
        response = await world.call("POST", "/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401 and code(response) == "invalid_credentials"
    await world.sign_in(user)
    user.password_change_required_at = world.clock()
    assert (await world.sign_in(user))["requires_password_change"] is True
    assert seen == [
        ("login", "bad_password"),
        ("login", "unknown_email"),
        ("login", "inactive_user"),
        ("login", "bad_password"),
        ("login", "ok"),
        ("login", "password_change_required"),
    ]


async def test_the_code_step_tells_each_outcome(tf: World) -> None:
    user = tf.account()
    secret, _, codes = await enrol_codes(tf, user)
    seen = observed(tf)
    first = await challenge(tf, user)
    wrong = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": first, "code": "000000"})
    assert code(wrong) == "two_factor_invalid"
    assert (await redeem(tf, first, secret)).status_code == 200
    spent = await redeem(tf, first, secret)
    assert code(spent) == "token_invalid"
    replayed = await redeem(tf, await challenge(tf, user), secret)
    assert code(replayed) == "two_factor_invalid", "the same step's code again"
    by_backup = await tf.call(
        "POST", "/auth/login/2fa", json={"challenge_token": await challenge(tf, user), "code": codes[0]}
    )
    assert by_backup.status_code == 200
    unknown = await tf.call("POST", "/auth/login/2fa", json={"challenge_token": "jwt.nope", "code": "123456"})
    assert code(unknown) == "token_invalid"
    late = await challenge(tf, user)
    user.is_active = False
    assert code(await redeem(tf, late, secret)) == "token_invalid"
    user.is_active = True
    late = await challenge(tf, user)
    state = tf.two_factor_store.states[user.id]
    tf.two_factor_store.states[user.id] = TwoFactorState(enabled=False)
    assert code(await redeem(tf, late, secret)) == "token_invalid"
    tf.two_factor_store.states[user.id] = state
    late = await challenge(tf, user)
    rotated = SecretBox("another-key").seal(secret, aad=totp_secret_aad(user.id))
    tf.two_factor_store.states[user.id] = TwoFactorState(enabled=True, secret=rotated, last_step=state.last_step)
    assert code(await redeem(tf, late, secret)) == "two_factor_unavailable"
    response = await tf.call("POST", "/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 503
    login = [pair for pair in seen if pair[0] == "login"]
    assert set(login) == {("login", "totp_required"), ("login", "secret_unreadable")}
    assert [pair for pair in seen if pair[0] != "login"] == [
        ("login_2fa", "invalid_code"),
        ("login_2fa", "ok"),
        ("login_2fa", "challenge_spent"),
        ("login_2fa", "replayed_code"),
        ("login_2fa", "backup_code"),
        ("login_2fa", "challenge_invalid"),
        ("login_2fa", "inactive_user"),
        ("login_2fa", "two_factor_off"),
        ("login_2fa", "secret_unreadable"),
    ]


async def test_the_code_step_passed_is_told_so_when_a_forced_change_follows(tf: World) -> None:
    user = tf.account()
    secret, _, codes = await enrol_codes(tf, user)
    user.password_change_required_at = tf.clock()
    seen = observed(tf)
    assert (await redeem(tf, await challenge(tf, user), secret)).json()["requires_password_change"] is True
    body = {"challenge_token": await challenge(tf, user), "code": codes[0]}
    assert (await tf.call("POST", "/auth/login/2fa", json=body)).json()["requires_password_change"] is True
    assert seen == [
        ("login", "totp_required"),
        ("login_2fa", "ok"),
        ("login", "totp_required"),
        ("login_2fa", "backup_code"),
    ]


async def test_the_forced_change_tells_each_outcome(world: World) -> None:
    user = world.account(password_change_required_at=world.clock())
    off = world.account(email="off@example.com", is_active=False, password_change_required_at=world.clock())
    seen = observed(world)
    token = (await world.sign_in(user))["challenge_token"]

    async def set_password(challenge_token: str, new_password: str = "a new one") -> Any:
        body = {"challenge_token": challenge_token, "new_password": new_password}
        return await world.call("POST", "/auth/login/set-password", json=body)

    assert code(await set_password("jwt.nope")) == "token_invalid"
    dead = world.signer.sign(challenge_claims(off.id, ChallengeKind.PASSWORD_CHANGE, now=world.clock()))
    assert code(await set_password(dead)) == "token_invalid"
    assert code(await set_password(token, PASSWORD)) == "password_unchanged"
    assert (await set_password(token)).status_code == 200
    assert code(await set_password(token, "a third one")) == "password_change_not_required"
    assert seen == [
        ("login", "password_change_required"),
        ("login_set_password", "challenge_invalid"),
        ("login_set_password", "inactive_user"),
        ("login_set_password", "password_unchanged"),
        ("login_set_password", "ok"),
        ("login_set_password", "password_change_not_required"),
    ]


async def test_a_passkey_sign_in_tells_the_reasons_its_one_refusal_folds(world: World) -> None:
    user = world.account()
    device = SoftAuthenticator(ORIGIN, counts=False)
    await add_passkey(world, await world.token_of(user), device)
    seen = observed(world)
    started = (await world.call("POST", "/auth/passkey/login/begin")).json()
    body = {"challenge_token": started["challenge_token"], "credential": device.get(started["options"])}
    assert (await world.call("POST", "/auth/passkey/login/finish", json=body)).status_code == 200
    assert code(await world.call("POST", "/auth/passkey/login/finish", json=body)) == "passkey_refused"
    bad = {**body, "challenge_token": "jwt.nope"}
    assert code(await world.call("POST", "/auth/passkey/login/finish", json=bad)) == "passkey_refused"
    stranger = SoftAuthenticator(ORIGIN)
    stranger.rp_id, stranger.user_handle = device.rp_id, device.user_handle
    assert code(await passkey_sign_in(world, stranger)) == "passkey_refused"
    other = SoftAuthenticator.handle_of(99)
    assert code(await passkey_sign_in(world, device, user_handle=other)) == "passkey_refused"
    device.verifies = False
    assert code(await passkey_sign_in(world, device)) == "passkey_refused"
    device.verifies = True
    user.is_active = False
    assert code(await passkey_sign_in(world, device)) == "passkey_refused"
    user.is_active = True
    fresh = (await world.call("POST", "/auth/passkey/login/begin")).json()
    broken = {
        "challenge_token": fresh["challenge_token"],
        "credential": {**device.get(fresh["options"]), "rawId": "***"},
    }
    assert code(await world.call("POST", "/auth/passkey/login/finish", json=broken)) == "passkey_refused"
    user.password_change_required_at = world.clock()
    assert (await passkey_sign_in(world, device)).json()["requires_password_change"] is True
    assert seen == [
        ("passkey_login", "ok"),
        ("passkey_login", "challenge_spent"),
        ("passkey_login", "challenge_invalid"),
        ("passkey_login", "unknown_passkey"),
        ("passkey_login", "user_handle_mismatch"),
        ("passkey_login", "not_verified"),
        ("passkey_login", "inactive_user"),
        ("passkey_login", "unknown_passkey"),
        ("passkey_login", "password_change_required"),
    ]


async def test_a_refresh_tells_the_reasons_its_one_refusal_folds(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    other = world.account(email="bob@example.com")
    pair = await world.sign_in(user)
    theirs = await world.sign_in(other)
    lapsed = await world.sign_in(world.account(email="carol@example.com"))
    world.sessions.rows[world.signer.claims(lapsed["refresh_token"])["sid"]].expires_at = world.clock() - timedelta(
        seconds=1
    )
    from eifi1_server_kit.auth import refresh_claims

    forged = world.signer.sign(
        refresh_claims(other.id, now=world.clock(), extra={"sid": world.sessions.of(user.id)[0].sid})
    )
    refused = False

    async def claims(account: Any, session: Any) -> dict[str, Any]:
        if refused:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "No company left")
        return {}

    seen = observed(world, claims=claims)

    async def refresh(token: str) -> Any:
        return await world.call("POST", "/auth/refresh", json={"refresh_token": token})

    assert (await refresh(pair["refresh_token"])).status_code == 200
    for token in ("jwt.unknown", pair["access_token"]):
        assert code(await refresh(token)) == "session_ended"
    assert code(await refresh(forged)) == "session_ended"
    assert code(await refresh(lapsed["refresh_token"])) == "session_ended"
    other.password_change_required_at = world.clock()
    assert code(await refresh(theirs["refresh_token"])) == "session_ended"
    refused = True
    assert code(await refresh(pair["refresh_token"])) == "session_ended"
    refused = False
    user.is_active = False
    assert code(await refresh(pair["refresh_token"])) == "session_ended"
    user.is_active = True
    user.sessions_invalid_before = world.clock()  # type: ignore[attr-defined]
    assert code(await refresh(pair["refresh_token"])) == "session_ended"
    assert seen == [
        ("refresh", "ok"),
        ("refresh", "invalid_token"),
        ("refresh", "invalid_token"),
        ("refresh", "session_ended"),
        ("refresh", "session_expired"),
        ("refresh", "password_change_required"),
        ("refresh", "session_ended"),
        ("refresh", "inactive_user"),
        ("refresh", "revoked"),
    ]


# --- the reset ---------------------------------------------------------------------------------------


async def test_a_reset_request_tells_what_its_one_answer_hides(cutoff_world: World) -> None:
    world = cutoff_world
    world.account()
    world.account(email="off@example.com", is_active=False)
    world.account(email="demo@example.com", is_demo=True)
    seen = observed(world)
    for email in ("ada@example.com", "nobody@example.com", "off@example.com", "demo@example.com"):
        response = await world.call("POST", "/auth/password-reset/request", json={"email": email})
        assert response.status_code == 204
    world.mailer.works = False
    response = await world.call("POST", "/auth/password-reset/request", json={"email": "ada@example.com"})
    assert response.status_code == 204, "the mail failed: still 204, so only the observer says so"
    assert seen == [
        ("password_reset_request", "sent"),
        ("password_reset_request", "unknown_email"),
        ("password_reset_request", "inactive_user"),
        ("password_reset_request", "demo_user"),
        ("password_reset_request", "send_failed"),
    ]


async def test_the_reset_request_is_observed_at_its_floor_account_or_not() -> None:
    """The observer runs after the floor on every path, so it cannot tell an account by time."""
    timer = Timer()
    world = TimedWorld(timer)
    world.account()
    calls: list[tuple[str, float]] = []
    world.hooks = AuthHooks(observe=lambda event, outcome: calls.append((outcome, timer())))
    async with world.client:
        answers = [await world.timed(email) for email in ("ada@example.com", "nobody@example.com")]
    assert answers == [(204, FLOOR), (204, FLOOR)]
    assert calls == [("sent", 100.0 + FLOOR), ("unknown_email", 100.0 + 2 * FLOOR)]


async def test_a_reset_send_cut_at_the_floor_is_send_failed() -> None:
    timer = Timer()
    world = TimedWorld(timer)
    world.account()
    seen = observed(world)

    async def hangs(*args: Any, **kwargs: Any) -> bool:
        timer.at += FLOOR
        raise TimeoutError

    world.mailer.send = hangs  # type: ignore[method-assign]
    async with world.client:
        assert await world.timed("ada@example.com") == (204, FLOOR + 0.0625)
    assert seen == [("password_reset_request", "send_failed")]


async def test_a_reset_link_tells_each_outcome_at_the_check_and_the_confirm(world: World) -> None:
    user = world.account()
    off = world.account(email="off@example.com", is_active=False)
    seen = observed(world)

    async def check_and_confirm(token: str) -> tuple[int, int]:
        check = await world.call("POST", "/auth/password-reset/check", json={"token": token})
        body = {"token": token, "new_password": "a new one!"}
        return check.status_code, (await world.call("POST", "/auth/password-reset/confirm", json=body)).status_code

    assert await check_and_confirm("nope") == (400, 400)
    assert await check_and_confirm(await link(world, OneTimeTokenKind.PASSWORD_RESET, user, seconds=-5)) == (400, 400)
    assert await check_and_confirm(await link(world, OneTimeTokenKind.PASSWORD_RESET, off)) == (400, 400)
    old = await link(world, OneTimeTokenKind.PASSWORD_RESET, user, email="old@example.com")
    assert await check_and_confirm(old) == (400, 400)
    assert await check_and_confirm(await link(world, OneTimeTokenKind.PASSWORD_RESET, user)) == (200, 200)
    expected = []
    for outcome in ("invalid", "expired", "inactive_user", "email_changed", "ok"):
        expected += [("password_reset_check", outcome), ("password_reset_confirm", outcome)]
    assert seen == expected


# --- the other links ---------------------------------------------------------------------------------


async def test_a_verification_link_tells_each_outcome(world: World) -> None:
    user = world.account()
    seen = observed(world)

    async def verify(token: str) -> int:
        return (await world.call("POST", "/auth/verify-email", json={"token": token})).status_code

    assert await verify("nope") == 400
    assert await verify(await link(world, OneTimeTokenKind.VERIFICATION, user, seconds=-1)) == 400
    assert await verify(await link(world, OneTimeTokenKind.VERIFICATION, user, email="old@example.com")) == 400
    assert await verify(await link(world, OneTimeTokenKind.VERIFICATION, user)) == 200
    assert seen == [
        ("email_verify", "invalid"),
        ("email_verify", "expired"),
        ("email_verify", "email_changed"),
        ("email_verify", "ok"),
    ]


async def test_an_address_confirmation_tells_each_outcome(world: World) -> None:
    user = world.account()
    world.account(email="bob@example.com")
    gone = world.account(email="gone@example.com")
    orphan = await link(world, OneTimeTokenKind.EMAIL_CHANGE, gone, email="g@example.com")
    del world.users.rows[gone.id]
    seen = observed(world)

    async def confirm(token: str) -> str | None:
        response = await world.call("POST", "/auth/me/email/confirm", json={"token": token})
        return None if response.status_code == 200 else code(response)

    assert await confirm("nope") == "token_invalid"
    assert await confirm(orphan) == "token_invalid"
    expired = await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com", seconds=-1)
    assert await confirm(expired) == "token_expired"
    assert await confirm(await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="bob@example.com")) == (
        "email_taken"
    )
    user.is_active = False
    assert await confirm(await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com")) == (
        "token_invalid"
    )
    user.is_active = True
    assert await confirm(await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com")) is None
    assert seen == [
        ("email_change_confirm", "invalid"),
        ("email_change_confirm", "invalid"),
        ("email_change_confirm", "expired"),
        ("email_change_confirm", "email_taken"),
        ("email_change_confirm", "inactive_user"),
        ("email_change_confirm", "ok"),
    ]


# --- invitations and the sign-up ---------------------------------------------------------------------


async def test_an_invitation_s_check_and_accept_tell_each_outcome(world: World) -> None:
    user = world.account(email="bob@example.com")
    token = await world.token_of(user)
    refusing = False

    async def invitation_accepting(account: Any, invitation: Any) -> None:
        if refusing:
            raise AuthError(AuthErrorCode.EMAIL_TAKEN)

    seen = observed(world, invitation_accepting=invitation_accepting)
    raw, _ = invite(world, "bob@example.com")
    expired, _ = invite(world, "bob@example.com", expires_at=world.clock())
    foreign, _ = invite(world, "carol@example.com")
    for checked in ("nope", expired, raw):
        await world.call("POST", "/auth/invitations/check", json={"token": checked})
    for accepted in ("nope", expired, foreign, raw):
        await world.call("POST", "/auth/invitations/accept", token=token, json={"token": accepted})
    refusing = True
    other, _ = invite(world, "bob@example.com")
    response = await world.call("POST", "/auth/invitations/accept", token=token, json={"token": other})
    assert code(response) == "email_taken"
    assert seen == [
        ("invitation_check", "invalid"),
        ("invitation_check", "expired"),
        ("invitation_check", "ok"),
        ("invitation_accept", "invalid"),
        ("invitation_accept", "expired"),
        ("invitation_accept", "email_mismatch"),
        ("invitation_accept", "ok"),
        ("invitation_accept", "email_taken"),
    ], "a hook's refusal is told by its own code"


async def test_a_demo_s_accept_is_told_by_its_code(cutoff_world: World) -> None:
    world = cutoff_world
    demo = world.account(is_demo=True)
    raw, _ = invite(world, demo.email)
    token = await world.token_of(demo)
    seen = observed(world)
    await world.call("POST", "/auth/invitations/accept", token=token, json={"token": raw})
    assert seen == [("invitation_accept", "demo_refused")]


async def test_a_sign_up_tells_each_outcome(world: World) -> None:
    seen = observed(world)

    callers = iter(range(1, 100))

    async def register(email: str, invite_token: str | None = None) -> Any:
        body = {"email": email, **REGISTER, **({"invite_token": invite_token} if invite_token else {})}
        headers = {"x-test-ip": f"203.0.113.{next(callers)}"}
        return await world.call("POST", "/auth/register", json=body, headers=headers)

    assert (await register("ada@example.com")).status_code == 201
    assert code(await register("bob@example.com")) == "registration_closed"
    assert code(await register("bob@example.com", "nope")) == "invitation_invalid"
    expired, _ = invite(world, "bob@example.com", expires_at=world.clock())
    assert code(await register("bob@example.com", expired)) == "invitation_expired"
    foreign, _ = invite(world, "carol@example.com")
    assert code(await register("bob@example.com", foreign)) == "invitation_invalid"
    taken, _ = invite(world, "ada@example.com")
    assert code(await register("ada@example.com", taken)) == "email_taken"
    assert seen == [
        ("register", "ok"),
        ("register", "registration_closed"),
        ("register", "invalid"),
        ("register", "expired"),
        ("register", "email_mismatch"),
        ("register", "email_taken"),
    ]


# --- every event, throttled ----------------------------------------------------------------------------


async def test_every_event_tells_a_429_as_throttled(tf: World) -> None:
    world = tf
    token = await world.token_of(world.account())
    one = Budget(1, 3600)
    world.app.state.auth_limiters = AuthLimiters(
        login=one,
        challenge=one,
        challenge_subject=one,
        register=one,
        reset_ip=one,
        reset_address=one,
        redeem=one,
        refresh=one,
        requests=None,
    )
    seen = observed(world)
    routes: list[tuple[str, dict[str, Any]]] = [
        ("/auth/register", {"email": "x@example.com", **REGISTER}),
        ("/auth/login", {"email": "x@example.com", "password": "wrong"}),
        ("/auth/login/2fa", {"challenge_token": "jwt.nope", "code": "123456"}),
        ("/auth/login/set-password", {"challenge_token": "jwt.nope", "new_password": "a new one"}),
        ("/auth/passkey/login/finish", {"challenge_token": "jwt.nope", "credential": {}}),
        ("/auth/refresh", {"refresh_token": "jwt.nope"}),
        ("/auth/password-reset/request", {"email": "x@example.com"}),
        ("/auth/password-reset/check", {"token": "nope"}),
        ("/auth/password-reset/confirm", {"token": "nope", "new_password": "a new one!"}),
        ("/auth/verify-email", {"token": "nope"}),
        ("/auth/me/email/confirm", {"token": "nope"}),
        ("/auth/invitations/check", {"token": "nope"}),
        ("/auth/invitations/accept", {"token": "nope"}),
    ]
    for path, body in routes:
        for _ in range(2):
            response = await world.call("POST", path, token=token, json=body)
        assert response.status_code == 429, path
    assert {event for event, outcome in seen if outcome == "throttled"} == set(AuthEvent)


# --- an observer that fails, and none --------------------------------------------------------------------


async def test_an_observer_that_raises_changes_no_answer_and_is_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    user = world.account()

    def observe(event: AuthEvent, outcome: str) -> None:
        raise RuntimeError("the metrics backend is down")

    world.hooks = AuthHooks(observe=observe)
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.auth"):
        response = await world.call("POST", "/auth/login", json={"email": user.email, "password": "wrong"})
        assert response.status_code == 401 and code(response) == "invalid_credentials"
        assert "access_token" in await world.sign_in(user)
        response = await world.call("POST", "/auth/password-reset/request", json={"email": user.email})
        assert response.status_code == 204
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.name for record in warnings] == ["eifi1_server_kit.auth"] * 3
    assert all("observe" in record.getMessage() for record in warnings)
    assert [record.args[0] for record in warnings if isinstance(record.args, tuple)] == [
        "login",
        "login",
        "password_reset_request",
    ]


async def test_a_fault_that_is_no_refusal_is_not_observed(world: World) -> None:
    """A store's fault or the app's own exception is no decision: nothing is counted."""
    seen: Seen = []

    async def on_register(*args: Any) -> None:
        raise RuntimeError("the database went away")

    service = world.service(AuthHooks(observe=lambda *pair: seen.append(pair), on_register=on_register))
    body = RegisterRequest(email="ada@example.com", **REGISTER)
    with pytest.raises(RuntimeError):
        await service.register(body, ip="198.51.100.1")
    assert seen == []


async def test_without_an_observer_the_answers_are_the_same(world: World) -> None:
    assert AuthHooks().observe is None
    user = world.account()
    response = await world.call("POST", "/auth/login", json={"email": user.email, "password": "wrong"})
    assert response.status_code == 401 and code(response) == "invalid_credentials"
    assert "access_token" in await world.sign_in(user)
    assert (
        await world.call("POST", "/auth/password-reset/request", json={"email": "x@example.com"})
    ).status_code == 204
