"""Invitations over HTTP: the public check and accept, and the admin's routes (auth §4.4,
user-admin §5; §12.5 item 11)."""

from datetime import timedelta
from typing import Any

from eifi1_server_kit.auth import InvitationCheck, mint
from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.service import AuthHooks
from eifi1_server_kit.user_admin import InvitationRow
from tests.auth._world import World
from tests.auth_fakes import MemoryInvitation, MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


def invite(world: World, email: str, **values: Any) -> tuple[str, MemoryInvitation]:
    raw, digest = mint()
    return raw, world.invitations.seed(email=email, token_hash=digest, **values)


# --- the public routes ---------------------------------------------------------------------------


async def test_a_check_says_what_an_invitation_is_for_without_spending_it(world: World) -> None:
    raw, invitation = invite(world, "bob@example.com", role="REVIEWER")
    response = await world.call("POST", "/auth/invitations/check", json={"token": raw})
    assert response.status_code == 200
    assert response.json() == {
        "email": "bob@example.com",
        "expires_at": "2026-11-27T12:00:00Z",
        "account_exists": False,
        "role": "REVIEWER",
    }
    world.account(email="bob@example.com")
    assert (await world.call("POST", "/auth/invitations/check", json={"token": raw})).json()["account_exists"] is True
    assert invitation.accepted_at is None


async def test_a_check_answers_the_app_s_words_for_the_scope() -> None:
    class Check(InvitationCheck):
        team_name: str | None = None

    world = World(invitation_check_model=Check)
    async with world.client:

        async def describe_invitation(invitation: MemoryInvitation) -> dict[str, Any]:
            return {"team_name": "Rig 4"}

        world.hooks = AuthHooks(describe_invitation=describe_invitation)
        raw, _ = invite(world, "bob@example.com", scope={"team": 4})
        assert (await world.call("POST", "/auth/invitations/check", json={"token": raw})).json()["team_name"] == "Rig 4"


async def test_a_dead_invitation_is_refused_with_its_code(world: World) -> None:
    expired, _ = invite(world, "bob@example.com", expires_at=world.clock())
    revoked, _ = invite(world, "bob@example.com", revoked_at=world.clock())
    for token, expected in (
        (expired, "invitation_expired"),
        (revoked, "invitation_invalid"),
        ("nope", "invitation_invalid"),
    ):
        response = await world.call("POST", "/auth/invitations/check", json={"token": token})
        assert response.status_code == 400 and code(response) == expected


async def test_an_existing_account_takes_up_an_invitation_for_its_address(world: World) -> None:
    user = world.account(email="bob+staunlabor@example.com")
    token = await world.token_of(user)
    redeemed: list[int] = []

    async def on_invitation_redeemed(account: MemoryUser, invitation: MemoryInvitation) -> None:
        redeemed.append(invitation.id)

    world.hooks = AuthHooks(on_invitation_redeemed=on_invitation_redeemed)
    raw, invitation = invite(world, "bob@example.com", role="MEMBER")
    response = await world.call("POST", "/auth/invitations/accept", token=token, json={"token": raw})
    assert response.status_code == 200 and response.json() == {"email": "bob@example.com", "role": "MEMBER"}
    assert invitation.accepted_by_user_id == user.id and redeemed == [invitation.id]
    again = await world.call("POST", "/auth/invitations/accept", token=token, json={"token": raw})
    assert code(again) == "invitation_invalid"
    foreign, _ = invite(world, "carol@example.com")
    response = await world.call("POST", "/auth/invitations/accept", token=token, json={"token": foreign})
    assert response.status_code == 400 and code(response) == "invitation_invalid"


async def test_a_demo_takes_up_no_invitation(cutoff_world: World) -> None:
    world = cutoff_world
    demo = world.account(is_demo=True)
    raw, _ = invite(world, demo.email)
    response = await world.call(
        "POST", "/auth/invitations/accept", token=await world.token_of(demo), json={"token": raw}
    )
    assert response.status_code == 403 and code(response) == "demo_refused"


# --- the admin's routes --------------------------------------------------------------------------------


async def admin_token(world: World) -> tuple[MemoryUser, str]:
    admin = world.account(email="root@example.com", role="ADMIN", first_name="Grace", last_name="Hopper")
    return admin, await world.token_of(admin)


async def test_only_an_admin_reaches_the_invitations(world: World) -> None:
    member = world.account()
    response = await world.call("GET", "/admin/invitations", token=await world.token_of(member))
    assert response.status_code == 403 and code(response) == "admin_required"
    assert (await world.call("GET", "/admin/invitations")).status_code == 401


async def test_an_admin_invites_and_the_mail_goes_after_the_commit(world: World) -> None:
    admin, token = await admin_token(world)
    commits = world.after.commits
    response = await world.call(
        "POST",
        "/admin/invitations",
        token=token,
        json={"email": "Bob@Example.com", "role": "MEMBER", "locale": "fr", "note": " new hire "},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "open" and body["sent"] is True and body["mail_backend"] == "resend"
    assert body["link"] is None, "never a link with a real transport"
    assert body["invited_by"] == {
        "id": admin.id,
        "email": "root@example.com",
        "first_name": "Grace",
        "last_name": "Hopper",
    }
    (mail,) = world.mailer.of("invitation")
    assert mail.to == "bob@example.com" and mail.after_commits == commits + 1
    assert mail.message.subject == "Invitation à Staunlabor" and "/register?invite=" in world.mailer.link("invitation")
    (row,) = world.log.rows
    assert (row.action, row.actor_id, row.target_email) == ("invite", admin.id, "bob@example.com")
    assert row.detail == {"invitation_id": body["id"], "role": "MEMBER"}
    raw = world.mailer.token("invitation")
    check = await world.call("POST", "/auth/invitations/check", json={"token": raw})
    assert check.json()["email"] == "bob@example.com"


async def test_with_mail_on_the_console_the_answer_shows_the_link_once() -> None:
    world = World(mail_backend="console")
    async with world.client:
        _, token = await admin_token(world)
        response = await world.call(
            "POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER"}
        )
        body = response.json()
        assert body["link"] == world.mailer.link("invitation")
        listed = (await world.call("GET", "/admin/invitations", token=token)).json()
        assert [row["link"] for row in listed] == [None]


async def test_the_list_names_the_inviter_and_the_status(world: World) -> None:
    admin, token = await admin_token(world)
    world.invitations.seed(email="old@example.com", invited_by_user_id=99, expires_at=None)
    world.clock.advance(minutes=1)
    world.invitations.seed(email="gone@example.com", invited_by_user_id=admin.id, revoked_at=world.clock())
    world.clock.advance(minutes=1)
    world.invitations.seed(email="new@example.com", invited_by_user_id=admin.id, role="MEMBER")
    world.invitations.seed(email="in@example.com", accepted_at=world.clock())
    rows = (await world.call("GET", "/admin/invitations", token=token)).json()
    assert [(row["email"], row["status"]) for row in rows] == [
        ("new@example.com", "open"),
        ("gone@example.com", "revoked"),
        ("old@example.com", "expired"),
    ]
    assert rows[0]["invited_by"]["first_name"] == "Grace" and rows[2]["invited_by"] is None
    assert rows[2]["expires_at"] == rows[2]["created_at"], "a row never sent reads as expired"


async def test_a_scope_needs_the_app_s_hook() -> None:
    class Row(InvitationRow):
        team: int | None = None

    world = World(invitation_row_model=Row)
    async with world.client:
        _, token = await admin_token(world)
        body = {"email": "bob@example.com", "role": "MEMBER", "scope": 4}
        response = await world.call("POST", "/admin/invitations", token=token, json=body)
        assert response.status_code == 400 and code(response) == "invitation_scope_invalid"

        async def invitation_scope(admin: MemoryUser, scope: int | str) -> dict[str, Any]:
            if scope != 4:
                raise AuthError(AuthErrorCode.INVITATION_SCOPE_INVALID, "Not your team")
            return {"team": scope}

        async def describe_invitation(invitation: MemoryInvitation) -> dict[str, Any]:
            return {"team": (invitation.scope or {}).get("team")}

        world.hooks = AuthHooks(invitation_scope=invitation_scope, describe_invitation=describe_invitation)
        assert (
            await world.call("POST", "/admin/invitations", token=token, json={**body, "scope": 5})
        ).status_code == 400
        response = await world.call("POST", "/admin/invitations", token=token, json=body)
        assert response.status_code == 201 and world.invitations.rows[1].scope == {"team": 4}
        assert (await world.call("GET", "/admin/invitations", token=token)).json()[0]["team"] == 4


async def test_a_re_send_mints_a_new_link_and_14_more_days(world: World) -> None:
    _, token = await admin_token(world)
    first, invitation = invite(world, "bob@example.com", expires_at=world.clock() - timedelta(days=1))
    response = await world.call("POST", f"/admin/invitations/{invitation.id}/resend", token=token)
    assert response.status_code == 200 and response.json()["status"] == "open"
    assert invitation.expires_at == world.clock() + timedelta(days=14)
    assert code(await world.call("POST", "/auth/invitations/check", json={"token": first})) == "invitation_invalid"
    second = world.mailer.token("invitation")
    assert (await world.call("POST", "/auth/invitations/check", json={"token": second})).status_code == 200
    assert world.log.actions() == ["invite_resend"]
    for missing, status, expected in ((999, 404, "invitation_not_found"),):
        response = await world.call("POST", f"/admin/invitations/{missing}/resend", token=token)
        assert response.status_code == status and code(response) == expected
    _, accepted = invite(world, "in@example.com", accepted_at=world.clock())
    response = await world.call("POST", f"/admin/invitations/{accepted.id}/resend", token=token)
    assert response.status_code == 409 and code(response) == "invitation_closed"


async def test_a_revoke_withdraws_an_open_or_expired_one_once(world: World) -> None:
    _, token = await admin_token(world)
    _, invitation = invite(world, "bob@example.com", expires_at=world.clock() - timedelta(days=1))
    for _ in range(2):
        assert (await world.call("DELETE", f"/admin/invitations/{invitation.id}", token=token)).status_code == 204
    assert invitation.revoked_at == world.clock() and world.log.actions() == ["invite_revoke"]
    _, accepted = invite(world, "in@example.com", accepted_at=world.clock())
    response = await world.call("DELETE", f"/admin/invitations/{accepted.id}", token=token)
    assert response.status_code == 409 and code(response) == "invitation_closed"
    assert (await world.call("DELETE", "/admin/invitations/999", token=token)).status_code == 404


async def test_invitation_mails_are_limited_per_recipient(world: World) -> None:
    _, token = await admin_token(world)
    for _ in range(10):
        await world.call("POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER"})
    response = await world.call(
        "POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER"}
    )
    assert response.status_code == 429


# --- 0.31: the app's say when an invitation is made, and an open one sent again (G7) ---------------


async def test_the_app_may_refuse_an_invitation_when_it_is_made() -> None:
    """Kurvenschmiede's G7: no invitation to an address that already has an account."""
    world = World()
    async with world.client:
        _, token = await admin_token(world)
        world.account(email="taken@example.com")
        seen: list[tuple[str, bool, Any]] = []

        async def invitation_creating(admin: MemoryUser, body: Any, existing: Any, scope: Any) -> None:
            seen.append((body.email, existing is not None, scope))
            if existing is not None:
                raise AuthError(AuthErrorCode.EMAIL_TAKEN)

        world.hooks = AuthHooks(invitation_creating=invitation_creating)
        body = {"email": "Taken@Example.com", "role": "MEMBER"}
        refused = await world.call("POST", "/admin/invitations", token=token, json=body)
        assert (refused.status_code, code(refused)) == (409, "email_taken")
        assert not world.invitations.rows and world.log.actions() == [] and not world.mailer.of("invitation")
        made = await world.call("POST", "/admin/invitations", token=token, json={**body, "email": "new@example.com"})
        assert made.status_code == 201, made.text
        assert seen == [("taken@example.com", True, None), ("new@example.com", False, None)]


async def test_with_reuse_an_open_invitation_to_the_same_address_and_scope_is_sent_again() -> None:
    from dataclasses import replace

    from tests.auth._world import POLICY

    async def invitation_scope(admin: MemoryUser, scope: int | str) -> dict[str, Any]:
        return {"team": scope}

    world = World(policy=replace(POLICY, reuse_open_invitations=True))
    async with world.client:
        _, token = await admin_token(world)
        world.hooks = AuthHooks(invitation_scope=invitation_scope)
        body = {"email": "bob@example.com", "role": "MEMBER", "scope": 4}
        first = (await world.call("POST", "/admin/invitations", token=token, json=body)).json()
        dead = world.mailer.token("invitation")
        again = await world.call("POST", "/admin/invitations", token=token, json={**body, "email": "Bob@example.com"})
        assert again.status_code == 201 and again.json()["id"] == first["id"]
        assert len(world.invitations.rows) == 1 and world.log.actions() == ["invite", "invite_resend"]
        assert code(await world.call("POST", "/auth/invitations/check", json={"token": dead})) == "invitation_invalid"
        other_team = (await world.call("POST", "/admin/invitations", token=token, json={**body, "scope": 5})).json()
        other_role = (
            await world.call("POST", "/admin/invitations", token=token, json={**body, "role": "ADMIN"})
        ).json()
        assert len({first["id"], other_team["id"], other_role["id"]}) == 3

    plain = World()
    async with plain.client:
        _, token = await admin_token(plain)
        for _ in range(2):
            await plain.call(
                "POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER"}
            )
        assert len(plain.invitations.rows) == 2, "off by default: 0.29's behaviour"


async def test_an_invitation_names_its_group_in_the_mail_and_the_log() -> None:
    """Kurvenschmiede's G5/G6: "you are invited to the team Rig 4", and the team on every row."""
    from eifi1_server_kit.mail import MailText
    from eifi1_server_kit.user_admin.schemas import GroupRef

    own = {
        "en": MailText(
            subject="Join {group} on {app}",
            intro="Hello,",
            body="you have been invited to the team {group}.",
            cta="Accept",
            outro="",
        )
    }

    async def invitation_scope(admin: MemoryUser, scope: int | str) -> dict[str, Any]:
        return {"team": scope}

    async def invitation_group(invitation: MemoryInvitation) -> GroupRef | None:
        team = (invitation.scope or {}).get("team")
        return None if team is None else GroupRef(id=team, name=f"Rig {team}")

    world = World(mail_texts={"invitation": own})
    async with world.client:
        _, token = await admin_token(world)
        world.hooks = AuthHooks(invitation_scope=invitation_scope, invitation_group=invitation_group)
        made = await world.call(
            "POST", "/admin/invitations", token=token, json={"email": "bob@example.com", "role": "MEMBER", "scope": 4}
        )
        invitation_id = made.json()["id"]
        await world.call("POST", f"/admin/invitations/{invitation_id}/resend", token=token)
        await world.call("DELETE", f"/admin/invitations/{invitation_id}", token=token)
        mails = world.mailer.of("invitation")
        assert [mail.message.subject for mail in mails] == ["Join Rig 4 on Staunlabor"] * 2
        assert all("the team Rig 4." in mail.message.text for mail in mails)
        assert [(row.action, row.target_group_id) for row in world.log.rows] == [
            ("invite", 4),
            ("invite_resend", 4),
            ("invite_revoke", 4),
        ]
        plain = await world.call(
            "POST", "/admin/invitations", token=token, json={"email": "cy@example.com", "role": "MEMBER"}
        )
        assert plain.status_code == 201 and world.log.rows[-1].target_group_id is None
        assert world.mailer.of("invitation")[-1].message.subject == "Join  on Staunlabor"
