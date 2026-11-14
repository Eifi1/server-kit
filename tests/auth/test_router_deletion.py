"""Deleting one's account and exporting it, over HTTP (user-admin §6.4, §6.5)."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.auth import DeletionResult, DeletionTerms
from eifi1_server_kit.auth.ports import NewPasskey
from eifi1_server_kit.auth.service import AuthHooks
from eifi1_server_kit.user_admin import AuditDetailError, DeletionMode, assert_no_secrets
from tests.auth._world import PASSWORD, POLICY, World
from tests.auth_fakes import MemoryUser


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


async def test_the_terms_say_the_mode_and_the_days(world: World) -> None:
    token = await world.token_of(world.account())
    assert (await world.call("GET", "/auth/me/deletion", token=token)).json() == {"mode": "after_days", "days": 30}


async def test_an_operator_app_promises_no_day_and_says_its_counts() -> None:
    class Terms(DeletionTerms):
        hand_over_count: int = 0

    world = World(policy=replace(POLICY, deletion_mode=DeletionMode.OPERATOR), deletion_terms_model=Terms)
    async with world.client:

        async def deletion_terms(account: MemoryUser) -> dict[str, Any]:
            return {"hand_over_count": 3}

        world.hooks = AuthHooks(deletion_terms=deletion_terms)
        token = await world.token_of(world.account())
        response = await world.call("GET", "/auth/me/deletion", token=token)
        assert response.json() == {"mode": "operator", "days": None, "hand_over_count": 3}


async def test_a_deletion_request_deactivates_ends_every_session_logs_and_mails(world: World) -> None:
    user = world.account(locale="de-CH")
    other = await world.sign_in(user)
    token = await world.token_of(user)
    events: list[str] = []

    async def sessions_ended(account: MemoryUser, reason: str) -> None:
        events.append(reason)

    async def on_deletion_requested(account: MemoryUser) -> dict[str, Any]:
        events.append("requested")
        return {"guests_told": 2}

    world.hooks = AuthHooks(sessions_ended=sessions_ended, on_deletion_requested=on_deletion_requested)
    commits = world.after.commits
    response = await world.call(
        "POST", "/auth/me/deletion", token=token, json={"password": PASSWORD, "confirm_email": "ADA@example.com"}
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"deletion_scheduled_at": "2026-12-13T12:00:00Z", "mail_sent": True}
    assert not user.is_active and user.deletion_requested_at == world.clock()
    assert user.deletion_scheduled_at == world.clock() + timedelta(days=30)
    assert not any(row.is_active for row in world.sessions.of(user.id))
    assert events == ["deletion_requested", "requested"]
    (row,) = world.log.rows
    assert (row.action, row.actor_id, row.target_user_id) == ("deletion_request", user.id, user.id)
    assert row.detail == {"guests_told": 2}, "the hook's answer is the row's detail too (0.31, G3)"
    (mail,) = world.mailer.of("account_deletion")
    assert mail.after_commits == commits + 1 and "13.12.2026" in mail.message.text
    assert mail.message.subject == "Ihr Staunlabor-Konto wird gelöscht"
    assert code(await world.call("GET", "/auth/me", token=other["access_token"])) == "session_ended"


async def test_an_operator_app_s_deletion_mail_names_no_day_and_the_answer_carries_extras() -> None:
    class Result(DeletionResult):
        guests_told: int = 0

    world = World(policy=replace(POLICY, deletion_mode=DeletionMode.OPERATOR), deletion_result_model=Result)
    async with world.client:

        async def on_deletion_requested(account: MemoryUser) -> dict[str, Any]:
            return {"guests_told": 2}

        world.hooks = AuthHooks(on_deletion_requested=on_deletion_requested)
        user = world.account()
        world.mailer.works = False
        response = await world.call(
            "POST",
            "/auth/me/deletion",
            token=await world.token_of(user),
            json={"password": PASSWORD, "confirm_email": user.email},
        )
        assert response.json() == {"deletion_scheduled_at": None, "mail_sent": False, "guests_told": 2}
        assert world.log.rows[-1].detail == {"guests_told": 2}
        assert "An administrator will erase it" in world.mailer.of("account_deletion")[0].message.text


async def test_a_deletion_request_without_a_hook_or_its_answer_logs_no_detail(world: World) -> None:
    async def on_deletion_requested(account: MemoryUser) -> None:
        return None

    for hooks in (AuthHooks(), AuthHooks(on_deletion_requested=on_deletion_requested)):
        world.hooks = hooks
        user = world.account(email=f"{len(world.log.rows)}@example.com")
        response = await world.call(
            "POST",
            "/auth/me/deletion",
            token=await world.token_of(user),
            json={"password": PASSWORD, "confirm_email": user.email},
        )
        assert response.status_code == 202 and world.log.rows[-1].detail == {}


async def test_a_deletion_hook_s_answer_is_held_to_the_log_s_rule() -> None:
    """The detail never holds content (``audit_detail``): a sentence is the app's bug, a 500
    in its tests, and the request rolls back."""
    world = World()
    async with world.client:

        async def on_deletion_requested(account: MemoryUser) -> dict[str, Any]:
            return {"note": "Ada asked us to keep her drawings for her sister"}

        world.hooks = AuthHooks(on_deletion_requested=on_deletion_requested)
        user = world.account()
        token = await world.token_of(user)
        with pytest.raises(AuditDetailError):
            await world.call(
                "POST", "/auth/me/deletion", token=token, json={"password": PASSWORD, "confirm_email": user.email}
            )


async def test_a_deletion_request_is_refused_with_codes(world: World) -> None:
    admin = world.account(role="ADMIN")
    token = await world.token_of(admin)
    for body, status, expected in (
        ({"password": "wrong", "confirm_email": admin.email}, 400, "password_incorrect"),
        ({"password": PASSWORD, "confirm_email": ""}, 409, "confirmation_required"),
        ({"password": PASSWORD, "confirm_email": "bob@example.com"}, 409, "confirmation_mismatch"),
        ({"password": PASSWORD, "confirm_email": admin.email}, 409, "last_admin"),
    ):
        response = await world.call("POST", "/auth/me/deletion", token=token, json=body)
        assert response.status_code == status and code(response) == expected
    assert admin.is_active
    world.account(email="second@example.com", role="ADMIN")
    response = await world.call(
        "POST", "/auth/me/deletion", token=token, json={"password": PASSWORD, "confirm_email": admin.email}
    )
    assert response.status_code == 202


async def test_the_export_is_the_kit_s_envelope_with_the_app_s_data(world: World) -> None:
    user = world.account(email_verified_at=world.clock())
    token = await world.token_of(user)
    store = world.passkey_store
    await store.add(
        NewPasskey(user_id=user.id, credential_id=b"c" * 16, public_key=b"k" * 64, sign_count=0, name="Laptop")
    )

    async def export_data(account: MemoryUser) -> dict[str, Any]:
        return {"notes": ["one"]}

    world.hooks = AuthHooks(export_data=export_data)
    response = await world.call("GET", "/auth/me/export", token=token)
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="staunlabor-account-2026-11-13.json"'
    assert response.headers["cache-control"] == "no-store"
    envelope = json.loads(response.content)
    assert (envelope["format"], envelope["app"], envelope["data"]) == (
        "eifi1-account-export",
        "staunlabor",
        {"notes": ["one"]},
    )
    account = envelope["account"]
    assert account["email_verified"] is True and account["passkeys"] == [
        {"name": "Laptop", "created_at": "2026-11-13T12:00:00+00:00", "last_used_at": None}
    ]
    assert len(account["sessions"]) == 1 and "ip_address" not in account["sessions"][0]
    assert_no_secrets(envelope)
    again = await world.call("GET", "/auth/me/export", token=token)
    assert again.status_code == 429, "once a minute"


async def test_an_app_without_passkeys_exports_none() -> None:
    world = World(passkeys=False)
    async with world.client:
        token = await world.token_of(world.account())
        envelope = (await world.call("GET", "/auth/me/export", token=token)).json()
        assert envelope["account"]["passkeys"] == [] and envelope["data"] == {}


async def test_a_demo_neither_deletes_nor_exports(cutoff_world: World) -> None:
    world = cutoff_world
    demo = world.account(is_demo=True)
    token = await world.token_of(demo)
    assert code(await world.call("GET", "/auth/me/export", token=token)) == "demo_refused"
    response = await world.call(
        "POST", "/auth/me/deletion", token=token, json={"password": PASSWORD, "confirm_email": demo.email}
    )
    assert code(response) == "demo_refused"
