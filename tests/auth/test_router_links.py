"""The mailed links over HTTP: the address change, the verification, the password reset
(user-admin §6.2, auth §4.5, §6.3, §7; §12.5 items 2, 6, 9)."""

from datetime import timedelta
from typing import Any

from eifi1_server_kit.auth import OneTimeTokenKind, PasswordResetState, hash_token, mint
from eifi1_server_kit.auth.ports import NewSession
from eifi1_server_kit.auth.service import AuthHooks
from tests.auth._world import PASSWORD, World
from tests.auth_fakes import MemoryInvitation, MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


async def link(
    world: World, kind: OneTimeTokenKind, user: MemoryUser, *, email: str | None = None, **when: float
) -> str:
    raw, digest = mint()
    await world.tokens.put(
        kind,
        user_id=user.id,
        token_hash=digest,
        email=email or user.email,
        expires_at=world.clock() + timedelta(**(when or {"hours": 1})),
    )
    return raw


# --- the address change ---------------------------------------------------------------------------


async def test_an_address_change_mails_the_new_address_after_the_commit_and_tells_the_old(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    assert (await world.call("GET", "/auth/me/email", token=token)).json() == {
        "email": "ada@example.com",
        "pending_email": None,
        "pending_expires_at": None,
        "sent": None,
    }
    commits = world.after.commits
    response = await world.call(
        "POST", "/auth/me/email", token=token, json={"new_email": "Ada@New.example", "password": PASSWORD}
    )
    assert response.status_code == 202, response.text
    assert response.json()["pending_email"] == "ada@new.example" and response.json()["sent"] is True
    (mail,) = world.mailer.of("email_change")
    assert mail.to == "ada@new.example" and mail.after_commits == commits + 1, "committed before it was sent"
    (notice,) = world.mailer.of("email_change_notice")
    assert notice.to == "ada@example.com" and "ada@new.example" in notice.message.text
    assert user.email == "ada@example.com", "nothing changes until the link is opened"
    pending = (await world.call("GET", "/auth/me/email", token=token)).json()
    assert pending["pending_email"] == "ada@new.example" and pending["sent"] is None


async def test_an_address_change_is_refused_with_codes(world: World) -> None:
    user = world.account()
    world.account(email="taken@example.com")
    token = await world.token_of(user)
    for body, status, expected in (
        ({"new_email": "new@example.com", "password": "wrong"}, 400, "password_incorrect"),
        ({"new_email": "ada@example.com", "password": PASSWORD}, 400, "email_unchanged"),
        ({"new_email": "taken@example.com", "password": PASSWORD}, 409, "email_taken"),
    ):
        response = await world.call("POST", "/auth/me/email", token=token, json=body)
        assert response.status_code == status and code(response) == expected
    world.mailer.works = False
    response = await world.call(
        "POST", "/auth/me/email", token=token, json={"new_email": "new@example.com", "password": PASSWORD}
    )
    assert response.json()["sent"] is False
    for _ in range(2):
        await world.call("POST", "/auth/me/email", token=token, json={"new_email": "x@example.com", "password": "x"})
    response = await world.call(
        "POST", "/auth/me/email", token=token, json={"new_email": "new@example.com", "password": PASSWORD}
    )
    assert response.status_code == 429, "five changes an hour per account"


async def test_a_re_send_mints_a_new_link_and_a_cancel_drops_it(world: World) -> None:
    user = world.account()
    token = await world.token_of(user)
    response = await world.call("POST", "/auth/me/email/resend", token=token)
    assert response.status_code == 404 and code(response) == "email_change_not_pending"
    await world.call("POST", "/auth/me/email", token=token, json={"new_email": "new@example.com", "password": PASSWORD})
    first = world.mailer.token("email_change")
    response = await world.call("POST", "/auth/me/email/resend", token=token)
    assert response.status_code == 200 and response.json()["sent"] is True
    second = world.mailer.token("email_change")
    assert first != second
    dead = await world.call("POST", "/auth/me/email/confirm", json={"token": first})
    assert code(dead) == "token_invalid"
    assert (await world.call("DELETE", "/auth/me/email", token=token)).status_code == 204
    assert code(await world.call("POST", "/auth/me/email/confirm", json={"token": second})) == "token_invalid"


async def test_confirming_switches_the_address_and_attaches_only_open_invitations(world: World) -> None:
    user = world.account()
    raw = await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com")
    await link(world, OneTimeTokenKind.VERIFICATION, user)
    redeemed: list[int] = []

    async def on_invitation_redeemed(account: MemoryUser, invitation: MemoryInvitation) -> None:
        redeemed.append(invitation.id)

    world.hooks = AuthHooks(on_invitation_redeemed=on_invitation_redeemed)
    open_one = world.invitations.seed(email="new@example.com")
    world.invitations.seed(email="new@example.com", expires_at=world.clock() - timedelta(days=1))
    world.invitations.seed(email="new@example.com", expires_at=None)
    response = await world.call("POST", "/auth/me/email/confirm", json={"token": raw})
    assert response.status_code == 200
    assert response.json() == {"email": "new@example.com", "email_verified": True, "invitations_attached": 1}
    assert user.email == "new@example.com" and user.email_verified_at == world.clock()
    assert redeemed == [open_one.id] and open_one.accepted_by_user_id == user.id
    assert world.tokens.rows == [], "the old address's links die with it"


async def test_a_confirmation_is_refused_with_codes(world: World) -> None:
    user = world.account()
    expired = await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com", seconds=-1)
    response = await world.call("POST", "/auth/me/email/confirm", json={"token": expired})
    assert response.status_code == 400 and code(response) == "token_expired"
    taken = await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="bob@example.com")
    world.account(email="bob@example.com")
    assert code(await world.call("POST", "/auth/me/email/confirm", json={"token": taken})) == "email_taken"
    user.is_active = False
    off = await link(world, OneTimeTokenKind.EMAIL_CHANGE, user, email="new@example.com")
    assert code(await world.call("POST", "/auth/me/email/confirm", json={"token": off})) == "token_invalid"
    gone = world.account(email="gone@example.com")
    orphan = await link(world, OneTimeTokenKind.EMAIL_CHANGE, gone, email="new@example.com")
    del world.users.rows[gone.id]
    assert code(await world.call("POST", "/auth/me/email/confirm", json={"token": orphan})) == "token_invalid"


async def test_the_redemptions_share_a_window_per_caller(world: World) -> None:
    for _ in range(20):
        await world.call("POST", "/auth/verify-email", json={"token": "nope"})
    response = await world.call("POST", "/auth/password-reset/check", json={"token": "nope"})
    assert response.status_code == 429


# --- verification ----------------------------------------------------------------------------------


async def test_a_verification_link_verifies_its_address_once(world: World) -> None:
    user = world.account()
    raw = await link(world, OneTimeTokenKind.VERIFICATION, user)
    response = await world.call("POST", "/auth/verify-email", json={"token": raw})
    assert response.json() == {"email": "ada@example.com", "email_verified": True}
    assert user.email_verified_at == world.clock()
    assert code(await world.call("POST", "/auth/verify-email", json={"token": raw})) == "token_invalid"
    verified = world.account(email="bob@example.com", email_verified_at=world.clock() - timedelta(days=1))
    again = await link(world, OneTimeTokenKind.VERIFICATION, verified)
    assert (await world.call("POST", "/auth/verify-email", json={"token": again})).status_code == 200
    assert verified.email_verified_at == world.clock() - timedelta(days=1)


async def test_a_verification_link_for_an_old_address_or_run_out_is_refused(world: World) -> None:
    user = world.account()
    old = await link(world, OneTimeTokenKind.VERIFICATION, user, email="old@example.com")
    assert code(await world.call("POST", "/auth/verify-email", json={"token": old})) == "token_invalid"
    expired = await link(world, OneTimeTokenKind.VERIFICATION, user, seconds=0)
    assert code(await world.call("POST", "/auth/verify-email", json={"token": expired})) == "token_expired"


async def test_send_again_mails_after_the_commit_or_says_it_could_not(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    token = await world.token_of(user)
    response = await world.call("POST", "/auth/resend-verification", token=token)
    assert response.json() == {"email": "ada@example.com", "email_verified": False}
    (mail,) = world.mailer.of("verification")
    assert mail.after_commits >= 1 and "/verify-email?token=" in world.mailer.link("verification")
    world.mailer.works = False
    response = await world.call("POST", "/auth/resend-verification", token=token)
    assert response.status_code == 503 and code(response) == "mail_not_sent"
    user.email_verified_at = world.clock()
    sent = len(world.mailer.sent)
    response = await world.call("POST", "/auth/resend-verification", token=token)
    assert response.json()["email_verified"] is True and len(world.mailer.sent) == sent
    demo = world.account(email="demo@example.com", is_demo=True)
    response = await world.call("POST", "/auth/resend-verification", token=await world.token_of(demo))
    assert response.json()["email_verified"] is False and len(world.mailer.sent) == sent


# --- the reset -------------------------------------------------------------------------------------


async def test_a_reset_request_is_always_204_and_mails_after_the_commit(world: World) -> None:
    world.account(email="ada+staunlabor@example.com", locale="de-CH")
    world.account(email="off@example.com", is_active=False)
    for email in ("nobody@example.com", "off@example.com"):
        response = await world.call("POST", "/auth/password-reset/request", json={"email": email})
        assert response.status_code == 204 and response.content == b""
    assert world.mailer.sent == []
    commits = world.after.commits
    response = await world.call("POST", "/auth/password-reset/request", json={"email": "Ada@Example.com"})
    assert response.status_code == 204
    (mail,) = world.mailer.of("password_reset")
    assert mail.to == "ada+staunlabor@example.com", "the tagged account, at its own address"
    assert mail.after_commits == commits + 1 and mail.message.subject == "Passwort für Staunlabor zurücksetzen"


async def test_a_reset_request_is_limited_per_caller_and_per_address(world: World) -> None:
    for _ in range(3):
        await world.call("POST", "/auth/password-reset/request", json={"email": "ada@example.com"})
    response = await world.call("POST", "/auth/password-reset/request", json={"email": "ada@example.com"})
    assert response.status_code == 429
    response = await world.call("POST", "/auth/password-reset/request", json={"email": "bob@example.com"})
    assert response.status_code == 204


async def test_a_demo_is_sent_no_reset(cutoff_world: World) -> None:
    world = cutoff_world
    world.account(is_demo=True)
    await world.call("POST", "/auth/password-reset/request", json={"email": "ada@example.com"})
    assert world.mailer.sent == []


async def test_a_reset_ends_the_forced_change_every_session_and_verifies_the_address(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account(password_change_required_at=world.clock())
    await world.tokens.put(
        OneTimeTokenKind.EMAIL_CHANGE, user_id=user.id, token_hash="h", email="n@example.com", expires_at=world.clock()
    )
    session = await world.sessions.start(
        NewSession(sid="s1", user_id=user.id, started_at=world.clock(), expires_at=world.clock() + timedelta(days=1))
    )
    stages: list[tuple[int, bool]] = []
    ended: list[str] = []

    async def reset_extras(account: MemoryUser, confirmed: bool) -> dict[str, Any]:
        stages.append((account.id, confirmed))
        return {"encrypted": confirmed}

    async def sessions_ended(account: MemoryUser, reason: str) -> None:
        ended.append(reason)

    world.hooks = AuthHooks(reset_extras=reset_extras, sessions_ended=sessions_ended)
    raw = await link(world, OneTimeTokenKind.PASSWORD_RESET, user)
    check = await world.call("POST", "/auth/password-reset/check", json={"token": raw})
    assert check.json() == {"email": "ada@example.com"}
    response = await world.call(
        "POST", "/auth/password-reset/confirm", json={"token": raw, "new_password": "a new password"}
    )
    assert response.status_code == 200 and response.json() == {"email": "ada@example.com"}
    assert "access_token" not in response.json(), "a reset is not a sign-in"
    assert stages == [(user.id, False), (user.id, True)] and ended == ["password_reset"]
    assert user.password_hash == "hashed:a new password" and user.password_change_required_at is None
    assert user.email_verified_at == world.clock() and user.sessions_invalid_before == world.clock()  # type: ignore[attr-defined]
    assert not session.is_active and world.tokens.rows == []
    assert code(await world.call("POST", "/auth/password-reset/check", json={"token": raw})) == "token_invalid"


async def test_a_reset_keeps_an_earlier_verification_and_answers_the_app_s_fields() -> None:
    class State(PasswordResetState):
        encrypted: bool = False

    world = World(reset_state_model=State)
    async with world.client:
        verified_at = world.clock() - timedelta(days=3)
        user = world.account(email_verified_at=verified_at)

        async def reset_extras(account: MemoryUser, confirmed: bool) -> dict[str, Any]:
            return {"encrypted": True}

        world.hooks = AuthHooks(reset_extras=reset_extras)
        raw = await link(world, OneTimeTokenKind.PASSWORD_RESET, user)
        assert (await world.call("POST", "/auth/password-reset/check", json={"token": raw})).json()["encrypted"] is True
        response = await world.call(
            "POST", "/auth/password-reset/confirm", json={"token": raw, "new_password": "a new password"}
        )
        assert response.json() == {"email": "ada@example.com", "encrypted": True}
        assert user.email_verified_at == verified_at


async def test_a_reset_link_is_refused_with_codes(world: World) -> None:
    user = world.account()
    expired = await link(world, OneTimeTokenKind.PASSWORD_RESET, user, seconds=-5)
    assert code(await world.call("POST", "/auth/password-reset/check", json={"token": expired})) == "token_expired"
    old = await link(world, OneTimeTokenKind.PASSWORD_RESET, user, email="old@example.com")
    assert code(await world.call("POST", "/auth/password-reset/check", json={"token": old})) == "token_invalid"
    off = world.account(email="off@example.com", is_active=False)
    raw = await link(world, OneTimeTokenKind.PASSWORD_RESET, off)
    response = await world.call(
        "POST", "/auth/password-reset/confirm", json={"token": raw, "new_password": "a new one!"}
    )
    assert response.status_code == 400 and code(response) == "token_invalid"
    assert hash_token(raw) in {row.token_hash for row in world.tokens.rows}
