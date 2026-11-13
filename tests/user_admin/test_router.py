"""``user_admin_router`` in the global scope (keksdose, Kurvenschmiede, Staunlabor): the
roster, the four actions, the reviewer scope, the log, the overview, the mail routes and the
operator's erasure, over HTTP (user-admin §3–§6, admin §2.4, §9.2)."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from eifi1_server_kit.auth import OneTimeTokenKind
from eifi1_server_kit.auth.router import invitations_admin_router
from eifi1_server_kit.auth.service import AuthHooks, SessionsEndedReason
from eifi1_server_kit.user_admin import (
    AdminAction,
    AdminUserRow,
    ConfirmationLevel,
    ErasureResult,
    admin_action_record,
)
from eifi1_server_kit.user_admin.service import AdminPolicy, UserAdminHooks
from tests.auth._world import PASSWORD
from tests.auth_fakes import MemoryUser
from tests.user_admin._admin_world import AdminWorld

TYPED = {"confirm_email": "ada@example.com"}


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


# ── the guard ───────────────────────────────────────────────────────────────


async def test_every_admin_route_refuses_a_member_and_a_caller_without_a_session(world: AdminWorld) -> None:
    member = await world.token_of(world.account(email="bob@example.com"))
    routers = (world.admin_router, invitations_admin_router(world.deps))
    routes = [route for router in routers for route in router.routes if isinstance(route, APIRoute)]
    assert len(routes) == 15
    for route in routes:
        path = route.path.replace("{user_id}", "1").replace("{invitation_id}", "1")
        for method in route.methods:
            refused = await world.call(method, path, token=member, json={})
            assert (refused.status_code, code(refused)) == (403, "admin_required"), (method, path)
            anonymous = await world.call(method, path, json={})
            assert (anonymous.status_code, code(anonymous)) == (401, "not_signed_in"), (method, path)


# ── the roster ──────────────────────────────────────────────────────────────


async def test_the_roster_pages_people_with_the_active_admins_and_the_levels(cutoff_world: AdminWorld) -> None:
    world = cutoff_world
    _, token = await world.admin_token()
    world.account(email="ada@example.com", last_name="Byron", device_timezone="Europe/Zurich")
    world.account(email="bob@example.com", last_name="Babbage", is_active=False)
    world.account(email="demo@example.com", is_demo=True)
    response = await world.call("GET", "/admin/users?sort=name&state=active", token=token)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert [row["email"] for row in answer["users"]] == ["ada@example.com", "root@example.com"]
    assert answer["total"] == 2 and answer["mail_backend"] == "resend"
    assert answer["summary"] == {"active_admins": 1}
    assert answer["levels"]["deactivate"] == "type_email" and answer["levels"]["role"] == "acknowledge"
    assert "erase" not in answer["levels"], "a job erases here: no admin action"
    assert "plan" not in answer["levels"], "the app's own actions are the app's levels"
    assert "device_timezone" not in answer["users"][0], "where a device was is never the roster's"
    assert answer["users"][0]["email_verified"] is False
    searched = (await world.call("GET", "/admin/users?q=babbage", token=token)).json()
    assert [row["email"] for row in searched["users"]] == ["bob@example.com"]
    refused = await world.call("GET", "/admin/users?state=invited", token=token)
    assert refused.status_code == 422, "an app's user rows are never invited ones"
    assert (await world.call("GET", "/admin/users?role=OWNER", token=token)).status_code == 422
    assert (await world.call("GET", "/admin/users?limit=500", token=token)).status_code == 422


async def test_an_app_draws_its_own_row() -> None:
    class Row(AdminUserRow):
        plan: str | None = None

    world = AdminWorld(admin={"row_model": Row})
    async with world.client:

        async def to_row(user: MemoryUser) -> BaseModel:
            return Row.model_validate(user, from_attributes=True).model_copy(update={"plan": "pro"})

        world.admin_hooks = UserAdminHooks(to_row=to_row)
        _, token = await world.admin_token()
        answer = (await world.call("GET", "/admin/users", token=token)).json()
        assert answer["users"][0]["plan"] == "pro"


# ── deactivate and reactivate ───────────────────────────────────────────────


async def test_deactivating_asks_for_the_address_ends_every_session_and_logs(cutoff_world: AdminWorld) -> None:
    world = cutoff_world
    admin, token = await world.admin_token()
    user = world.account()
    theirs = await world.token_of(user)
    await world.tokens.put(
        OneTimeTokenKind.EMAIL_CHANGE,
        user_id=user.id,
        token_hash="h",
        email="new@example.com",
        expires_at=world.clock(),
    )
    told: list[str] = []

    async def sessions_ended(account: MemoryUser, reason: SessionsEndedReason) -> None:
        told.append(reason)

    world.hooks = AuthHooks(sessions_ended=sessions_ended)
    path = f"/admin/users/{user.id}/active"
    missing = await world.call("POST", path, token=token, json={"active": False})
    assert (missing.status_code, code(missing)) == (409, "confirmation_required")
    wrong = await world.call("POST", path, token=token, json={"active": False, "confirm_email": "bob@example.com"})
    assert code(wrong) == "confirmation_mismatch"
    response = await world.call("POST", path, token=token, json={"active": False, "confirm_email": "ADA@example.com "})
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False and not user.is_active
    assert user.sessions_invalid_before == world.clock()  # type: ignore[attr-defined]
    assert await world.tokens.pending(OneTimeTokenKind.EMAIL_CHANGE, user.id) is None
    assert told == ["deactivated"]
    assert code(await world.call("GET", "/auth/me", token=theirs)) == "session_ended"
    (row,) = world.log.rows
    assert (row.action, row.actor_id, row.target_user_id, row.detail) == (
        "deactivate",
        admin.id,
        user.id,
        {"sessions_ended": True},
    )
    again = await world.call("POST", path, token=token, json={"active": False, **TYPED})
    assert again.status_code == 200 and len(world.log.rows) == 1, "no change, no row"


async def test_an_admin_never_deactivates_themselves(world: AdminWorld) -> None:
    admin, token = await world.admin_token()
    response = await world.call(
        "POST", f"/admin/users/{admin.id}/active", token=token, json={"active": False, "confirm_email": admin.email}
    )
    assert (response.status_code, code(response)) == (409, "self_action")


async def test_reactivating_withdraws_a_pending_deletion(world: AdminWorld) -> None:
    _admin, token = await world.admin_token()
    user = world.account(is_active=False, deletion_requested_at=world.clock(), deletion_scheduled_at=world.clock())
    plain = world.account(email="bob@example.com", is_active=False)
    withdrawn: list[int] = []

    async def on_deletion_withdrawn(account: MemoryUser) -> None:
        withdrawn.append(account.id)

    world.hooks = AuthHooks(on_deletion_withdrawn=on_deletion_withdrawn)
    response = await world.call("POST", f"/admin/users/{user.id}/active", token=token, json={"active": True})
    assert response.status_code == 200 and response.json()["deletion_requested_at"] is None
    assert user.is_active and user.deletion_scheduled_at is None and withdrawn == [user.id]
    assert world.log.actions() == ["deletion_cancel", "reactivate"]
    await world.call("POST", f"/admin/users/{plain.id}/active", token=token, json={"active": True})
    assert plain.is_active and world.log.actions()[-1] == "reactivate" and withdrawn == [user.id]


async def test_an_account_not_found_and_a_demo_account_are_refused(cutoff_world: AdminWorld) -> None:
    world = cutoff_world
    _, token = await world.admin_token()
    demo = world.account(email="demo@example.com", is_demo=True)
    missing = await world.call("POST", "/admin/users/999/active", token=token, json={"active": True})
    assert (missing.status_code, code(missing)) == (404, "user_not_found")
    refused = await world.call("POST", f"/admin/users/{demo.id}/active", token=token, json={"active": True})
    assert (refused.status_code, code(refused)) == (403, "demo_refused")


async def test_the_last_active_admin_stays_an_admin_whoever_asks() -> None:
    """An operator who holds no admin role (a custom guard) meets the rule the roster states."""

    async def operator_guard() -> None:
        return None

    world = AdminWorld(admin={"guard": operator_guard})
    async with world.client:
        operator = await world.token_of(world.account(email="op@example.com"))
        root = world.account(email="root@example.com", role="ADMIN")
        response = await world.call(
            "POST",
            f"/admin/users/{root.id}/active",
            token=operator,
            json={"active": False, "confirm_email": root.email},
        )
        assert (response.status_code, code(response)) == (409, "last_admin")
        demoted = await world.call(
            "POST", f"/admin/users/{root.id}/role", token=operator, json={"role": "MEMBER", "acknowledged": True}
        )
        assert code(demoted) == "last_admin"


# ── the role ────────────────────────────────────────────────────────────────


async def test_a_role_change_is_acknowledged_logged_and_takes_the_review_scope_with_it(world: AdminWorld) -> None:
    _admin, token = await world.admin_token()
    user = world.account(role="REVIEWER", translation_review_locales=["fr"], translation_review_areas=["auth"])
    path = f"/admin/users/{user.id}/role"
    missing = await world.call("POST", path, token=token, json={"role": "ADMIN"})
    assert code(missing) == "confirmation_required"
    response = await world.call("POST", path, token=token, json={"role": "ADMIN", "acknowledged": True})
    assert response.status_code == 200 and response.json()["role"] == "ADMIN"
    assert user.translation_review_locales is None and user.translation_review_areas is None
    (row,) = world.log.rows
    assert (row.action, row.detail) == ("role", {"from": "REVIEWER", "to": "ADMIN"})
    same = await world.call("POST", path, token=token, json={"role": "ADMIN"})
    assert same.status_code == 200 and len(world.log.rows) == 1, "no change: nothing to confirm, no row"


async def test_the_reviewer_role_comes_only_with_its_scope(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    user = world.account()
    response = await world.call(
        "POST", f"/admin/users/{user.id}/role", token=token, json={"role": "REVIEWER", "acknowledged": True}
    )
    assert (response.status_code, code(response)) == (422, "role_not_grantable")


async def test_an_admin_never_takes_their_own_admin_role_away(world: AdminWorld) -> None:
    admin, token = await world.admin_token()
    world.account(email="second@example.com", role="ADMIN")
    response = await world.call(
        "POST", f"/admin/users/{admin.id}/role", token=token, json={"role": "MEMBER", "acknowledged": True}
    )
    assert (response.status_code, code(response)) == (409, "self_action")


async def test_the_app_refuses_a_role_change_or_a_reactivation_through_its_hooks(world: AdminWorld) -> None:
    """kastlan's seats: a staff role, or switching a staff member on again, costs a seat."""
    from eifi1_server_kit.user_admin import AccountError, AccountErrorCode

    _, token = await world.admin_token()
    user = world.account(is_active=False)
    seen: list[tuple[frozenset[str], frozenset[str]]] = []

    async def role_changing(admin: MemoryUser, target: MemoryUser, before: Any, after: Any) -> None:
        seen.append((before, after))
        raise AccountError(AccountErrorCode.OTHER_COMPANIES)

    async def activating(admin: MemoryUser, target: MemoryUser) -> None:
        raise AccountError(AccountErrorCode.OTHER_COMPANIES)

    world.admin_hooks = UserAdminHooks(role_changing=role_changing, activating=activating)
    refused = await world.call(
        "POST", f"/admin/users/{user.id}/role", token=token, json={"role": "ADMIN", "acknowledged": True}
    )
    assert code(refused) == "other_companies" and user.role == "MEMBER"
    assert seen == [(frozenset({"MEMBER"}), frozenset({"ADMIN"}))]
    refused = await world.call("POST", f"/admin/users/{user.id}/active", token=token, json={"active": True})
    assert code(refused) == "other_companies" and not user.is_active


# ── the forced password change ──────────────────────────────────────────────


async def test_a_forced_password_change_ends_every_session_and_is_withdrawn(world: AdminWorld) -> None:
    _admin, token = await world.admin_token()
    user = world.account()
    theirs = await world.token_of(user)
    told: list[str] = []

    async def sessions_ended(account: MemoryUser, reason: SessionsEndedReason) -> None:
        told.append(reason)

    world.hooks = AuthHooks(sessions_ended=sessions_ended)
    path = f"/admin/users/{user.id}/password-change"
    dropped = await world.call("POST", path, token=token)
    assert (dropped.status_code, code(dropped)) == (409, "confirmation_required"), "a body a proxy dropped fails closed"
    response = await world.call("POST", path, token=token, json={"acknowledged": True})
    assert response.status_code == 200 and response.json()["password_change_required_at"] is not None
    assert told == ["password_change_required"]
    assert code(await world.call("GET", "/auth/me", token=theirs)) == "session_ended"
    challenge = await world.call("POST", "/auth/login", json={"email": user.email, "password": PASSWORD})
    assert "challenge_token" in challenge.json()
    await world.call("POST", path, token=token, json={"acknowledged": True})
    assert world.log.actions() == ["password_change_require"], "no change, no row"
    assert world.log.rows[0].detail == {"sessions_ended": True}
    withdrawn = await world.call("DELETE", path, token=token)
    assert withdrawn.status_code == 200 and withdrawn.json()["password_change_required_at"] is None
    await world.call("DELETE", path, token=token)
    assert world.log.actions() == ["password_change_require", "password_change_withdraw"]


async def test_a_deactivated_account_may_be_required_a_new_password_for_its_return(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    user = world.account(is_active=False)
    response = await world.call(
        "POST", f"/admin/users/{user.id}/password-change", token=token, json={"acknowledged": True}
    )
    assert response.status_code == 200 and user.password_change_required_at is not None


async def test_an_app_raises_a_level_for_an_account(world: AdminWorld) -> None:
    """keksdose: a password that opens a key makes the forced change ``type_email``."""
    _, token = await world.admin_token()
    user = world.account()

    async def confirmation(target: MemoryUser, action: AdminAction) -> ConfirmationLevel | None:
        return ConfirmationLevel.TYPE_EMAIL if action is AdminAction.PASSWORD_CHANGE_REQUIRE else None

    world.admin_hooks = UserAdminHooks(confirmation=confirmation)
    path = f"/admin/users/{user.id}/password-change"
    assert code(await world.call("POST", path, token=token, json={"acknowledged": True})) == "confirmation_required"
    assert (await world.call("POST", path, token=token, json=TYPED)).status_code == 200


# ── the mails ───────────────────────────────────────────────────────────────


async def test_an_admin_re_sends_the_verification_mail_and_is_told_whether_it_went(world: AdminWorld) -> None:
    admin, token = await world.admin_token()
    user = world.account()
    response = await world.call("POST", f"/admin/users/{user.id}/mail", token=token, json={"kind": "verification"})
    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "mail_backend": "resend", "link": None}
    (mail,) = world.mailer.of("verification")
    assert mail.to == user.email
    (row,) = world.log.rows
    assert (row.action, row.actor_id, row.target_user_id, row.detail) == (
        "mail_verification",
        admin.id,
        user.id,
        {"sent": True},
    )


async def test_on_the_console_the_admin_is_shown_the_reset_link_once(operator_world: AdminWorld) -> None:
    world = operator_world
    _, token = await world.admin_token()
    user = world.account()
    response = await world.call("POST", f"/admin/users/{user.id}/mail", token=token, json={"kind": "password_reset"})
    answer = response.json()
    assert answer["mail_backend"] == "console" and answer["link"] == world.mailer.link("password_reset")
    assert world.log.actions() == ["mail_reset"]


async def test_no_mail_for_a_deactivated_account_or_a_verified_address(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    asleep = world.account(email="z@example.com", is_active=False)
    verified = world.account(email_verified_at=world.clock())
    refused = await world.call("POST", f"/admin/users/{asleep.id}/mail", token=token, json={"kind": "password_reset"})
    assert (refused.status_code, code(refused)) == (409, "account_inactive")
    refused = await world.call("POST", f"/admin/users/{verified.id}/mail", token=token, json={"kind": "verification"})
    assert (refused.status_code, code(refused)) == (409, "email_already_verified")
    sent = await world.call("POST", f"/admin/users/{verified.id}/mail", token=token, json={"kind": "password_reset"})
    assert sent.status_code == 200 and world.mailer.of("verification") == []


async def test_the_admin_s_mails_share_the_recipient_s_window(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    user = world.account()
    for _ in range(3):
        await world.call("POST", f"/admin/users/{user.id}/mail", token=token, json={"kind": "password_reset"})
    throttled = await world.call("POST", f"/admin/users/{user.id}/mail", token=token, json={"kind": "password_reset"})
    assert throttled.status_code == 429 and "Retry-After" in throttled.headers


async def test_the_transport_is_read_before_any_mail(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    assert (await world.call("GET", "/admin/mail", token=token)).json() == {"mail_backend": "resend"}


async def test_a_test_mail_goes_to_the_admin_alone_three_times_in_ten_minutes(world: AdminWorld) -> None:
    admin, token = await world.admin_token(locale="de-CH")
    for _ in range(3):
        response = await world.call("POST", "/admin/mail/test", token=token)
        assert response.json() == {"sent": True, "mail_backend": "resend"}
    assert {mail.to for mail in world.mailer.of("mail_test")} == {admin.email}
    assert "Testmail" in world.mailer.of("mail_test")[0].message.subject
    assert world.log.rows == [], "nobody is acted on"
    assert (await world.call("POST", "/admin/mail/test", token=token)).status_code == 429


async def test_a_demo_admin_sends_no_test_mail(cutoff_world: AdminWorld) -> None:
    world = cutoff_world
    _, token = await world.admin_token(is_demo=True)
    refused = await world.call("POST", "/admin/mail/test", token=token)
    assert (refused.status_code, code(refused)) == (403, "demo_refused")


# ── the reviewer scope ──────────────────────────────────────────────────────


async def test_the_reviewer_scope_sets_the_role_with_its_locales_and_takes_it_away(world: AdminWorld) -> None:
    _admin, token = await world.admin_token()
    user = world.account()
    path = f"/admin/users/{user.id}/reviewer"
    response = await world.call("PUT", path, token=token, json={"locales": ["FR", "de-ch"], "areas": ["feedback"]})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": user.id,
        "role": "REVIEWER",
        "locales": ["de-CH", "fr"],
        "areas": ["feedback"],
    }
    assert (user.role, user.translation_review_locales) == ("REVIEWER", ["de-CH", "fr"])
    (row,) = world.log.rows
    assert (row.action, row.detail) == ("reviewer", {"locales": ["de-CH", "fr"], "areas": ["feedback"]})
    await world.call("PUT", path, token=token, json={"locales": ["fr", "de-CH"], "areas": ["feedback"]})
    assert len(world.log.rows) == 1, "no change, no row"
    moved = await world.call("PUT", path, token=token, json={"locales": ["it"]})
    assert moved.json()["role"] == "REVIEWER" and moved.json()["areas"] is None and len(world.log.rows) == 2
    gone = await world.call("PUT", path, token=token, json={"locales": [], "areas": ["auth"]})
    assert gone.json() == {"user_id": user.id, "role": "MEMBER", "locales": [], "areas": None}
    assert (user.translation_review_locales, user.translation_review_areas) == (None, None)


async def test_a_member_given_no_locales_stays_a_member_and_nothing_is_logged(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    user = world.account()
    answer = await world.call("PUT", f"/admin/users/{user.id}/reviewer", token=token, json={"locales": []})
    assert answer.json()["role"] == "MEMBER" and world.log.rows == []


async def test_an_admin_is_no_reviewer_and_an_unknown_locale_is_refused(world: AdminWorld) -> None:
    admin, token = await world.admin_token()
    refused = await world.call("PUT", f"/admin/users/{admin.id}/reviewer", token=token, json={"locales": ["fr"]})
    assert (refused.status_code, code(refused)) == (409, "reviewer_is_admin")
    user = world.account()
    unknown = await world.call("PUT", f"/admin/users/{user.id}/reviewer", token=token, json={"locales": ["xx"]})
    assert unknown.status_code == 422


async def test_without_review_locales_there_is_no_reviewer_route() -> None:
    world = AdminWorld(admin={"policy": AdminPolicy()})
    async with world.client:
        _, token = await world.admin_token()
        user = world.account()
        missing = await world.call("PUT", f"/admin/users/{user.id}/reviewer", token=token, json={"locales": []})
        assert missing.status_code == 404


async def test_a_reviewer_without_a_role_is_its_locales_alone() -> None:
    """kastlan's platform: no REVIEWER role, the scope's columns only."""
    world = AdminWorld(admin={"policy": replace(_review(), reviewer_role=None, roles=("ADMIN", "MEMBER"))})
    async with world.client:
        _, token = await world.admin_token()
        user = world.account()
        answer = await world.call("PUT", f"/admin/users/{user.id}/reviewer", token=token, json={"locales": ["it"]})
        assert answer.json()["role"] == "MEMBER" and user.translation_review_locales == ["it"]
        listed = (await world.call("GET", "/admin/users?state=reviewer", token=token)).json()
        assert [row["id"] for row in listed["users"]] == [user.id]


def _review() -> AdminPolicy:
    from tests.user_admin._admin_world import REVIEW

    return REVIEW


# ── the log ─────────────────────────────────────────────────────────────────


async def test_the_log_pages_newest_first_naming_the_people_and_the_groups(world: AdminWorld) -> None:
    admin, token = await world.admin_token()
    user = world.account()
    for minutes, action in ((1, "deactivate"), (2, "reactivate")):
        await world.log.add(
            admin_action_record(
                action,
                actor_id=admin.id,
                target_user_id=user.id,
                target_email=user.email,
                now=world.clock() + timedelta(minutes=minutes),
            )
        )
    gone = await world.log.add(
        admin_action_record("erase", actor_id=None, target_user_id=None, target_email=None, now=world.clock())
    )
    gone.target_group_id = 7  # type: ignore[attr-defined]

    async def group_names(ids: Any) -> dict[int, str | None]:
        return {7: "Fahrwerk"}

    world.admin_hooks = UserAdminHooks(group_names=group_names)
    answer = (await world.call("GET", "/admin/actions?limit=2", token=token)).json()
    assert answer["total"] == 3
    assert [row["action"] for row in answer["actions"]] == ["erase", "reactivate"]
    assert answer["actions"][0]["actor"] is None and answer["actions"][0]["target"] is None
    assert answer["actions"][0]["target_group"] == {"id": 7, "name": "Fahrwerk"}
    assert answer["actions"][1]["actor"]["email"] == admin.email
    assert answer["actions"][1]["target"] == {
        "id": user.id,
        "email": user.email,
        "first_name": "Ada",
        "last_name": "Lovelace",
    }
    mine = (await world.call("GET", f"/admin/actions?target={user.id}&offset=1", token=token)).json()
    assert mine["total"] == 2 and [row["action"] for row in mine["actions"]] == ["deactivate"]
    assert (await world.call("GET", "/admin/actions?limit=0", token=token)).status_code == 422


# ── the overview ────────────────────────────────────────────────────────────


async def test_the_overview_counts_people_and_invitations_and_the_app_adds_its_own(cutoff_world: AdminWorld) -> None:
    world = cutoff_world
    _admin, token = await world.admin_token(created_at=world.clock() - timedelta(days=30))
    world.account(email="new@example.com")
    world.account(email="asked@example.com", is_active=False, deletion_requested_at=world.clock())
    world.account(email="demo@example.com", is_demo=True)
    world.invitations.seed(email="open@example.com")
    world.invitations.seed(email="old@example.com", expires_at=world.clock())
    world.invitations.seed(email="gone@example.com", revoked_at=world.clock())

    async def attention(viewer: MemoryUser) -> dict[str, Any]:
        return {"feedback_waiting": 4, "extra": {"support_waiting": 2}}

    world.admin_hooks = UserAdminHooks(attention=attention)
    answer = (await world.call("GET", "/admin/attention", token=token)).json()
    assert answer == {
        "feedback_waiting": 4,
        "deletion_requests": None,
        "invitations_pending": 1,
        "users_total": 3,
        "users_new_7d": 2,
        "extra": {"support_waiting": 2},
    }


async def test_an_operator_counts_the_deletion_requests_and_a_demo_admin_sees_no_count(
    operator_world: AdminWorld,
) -> None:
    world = operator_world
    _, token = await world.admin_token()
    world.account(email="asked@example.com", is_active=False, deletion_requested_at=world.clock())
    assert (await world.call("GET", "/admin/attention", token=token)).json()["deletion_requests"] == 1
    demo_world = AdminWorld(cutoff=True)
    async with demo_world.client:

        async def attention(viewer: MemoryUser) -> dict[str, Any]:
            return {"extra": {"awaiting_verdict": 3}}

        demo_world.admin_hooks = UserAdminHooks(attention=attention)
        _, demo = await demo_world.admin_token(is_demo=True)
        answer = (await demo_world.call("GET", "/admin/attention", token=demo)).json()
        assert answer["users_total"] is None and answer["extra"] == {"awaiting_verdict": None}


# ── the operator's erasure ──────────────────────────────────────────────────


async def test_an_operator_erases_an_account_that_asked(operator_world: AdminWorld) -> None:
    class Erased(ErasureResult):
        handed_over: int = 0

    world = AdminWorld(policy=operator_world.deps.policy, admin={"erasure_result_model": Erased})
    async with world.client:
        admin, token = await world.admin_token()
        user = world.account(is_active=False, deletion_requested_at=world.clock())
        purged: list[Any] = []

        async def erase(account: MemoryUser) -> dict[str, Any]:
            return {"handed_over": 2}

        async def purge_files(keys: Any) -> None:
            purged.append(list(keys))

        async def erase_author(user_id: int, identifiers: Any) -> Any:
            return SimpleNamespace(reports=1, keys=("feedback/x.png",))

        world.hooks = AuthHooks(erase=erase, purge_files=purge_files)
        world.feedback = SimpleNamespace(erase_author=erase_author)
        path = f"/admin/users/{user.id}/erase"
        assert code(await world.call("POST", path, token=token)) == "confirmation_required"
        response = await world.call("POST", path, token=token, json=TYPED)
        assert response.status_code == 200, response.text
        assert response.json() == {"feedback_anonymised": 1, "handed_over": 2}
        assert await world.users.get(user.id) is None and purged == [["feedback/x.png"]]
        levels = (await world.call("GET", "/admin/users", token=token)).json()["levels"]
        assert levels["erase"] == "type_email"
        erased = world.log.rows[-1]
        assert (erased.action, erased.actor_id, erased.target_user_id) == ("erase", admin.id, None)


async def test_only_an_account_that_asked_is_erased(operator_world: AdminWorld) -> None:
    world = operator_world
    admin, token = await world.admin_token()
    active = world.account(email="ada@example.com")
    asleep = world.account(email="bob@example.com", is_active=False)
    for user in (active, asleep):
        refused = await world.call(
            "POST", f"/admin/users/{user.id}/erase", token=token, json={"confirm_email": user.email}
        )
        assert (refused.status_code, code(refused)) == (409, "deletion_not_requested")
    mine = await world.call("POST", f"/admin/users/{admin.id}/erase", token=token, json={"confirm_email": admin.email})
    assert code(mine) == "self_action"
    assert world.users.rows.keys() >= {active.id, asleep.id}


async def test_where_a_job_erases_there_is_no_erase_route(world: AdminWorld) -> None:
    _, token = await world.admin_token()
    user = world.account(is_active=False, deletion_requested_at=world.clock())
    assert (await world.call("POST", f"/admin/users/{user.id}/erase", token=token, json=TYPED)).status_code == 404


# ── building it ─────────────────────────────────────────────────────────────


def test_the_global_scope_needs_a_roster(world: AdminWorld) -> None:
    from eifi1_server_kit.user_admin.router import UserAdminDeps, user_admin_router

    with pytest.raises(ValueError, match="roster"):
        user_admin_router(UserAdminDeps(auth=world.deps))


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (AdminPolicy(roles=("MEMBER", "REVIEWER")), "ADMIN"),
        (AdminPolicy(roles=("ADMIN", "MEMBER"), reviewer_role="REVIEWER"), "REVIEWER"),
        (AdminPolicy(grantable=("ADMIN", "OWNER")), "OWNER"),
    ],
)
def test_a_policy_that_names_a_role_the_app_has_not_is_refused(
    world: AdminWorld, policy: AdminPolicy, message: str
) -> None:
    from eifi1_server_kit.user_admin.router import UserAdminDeps, user_admin_router

    with pytest.raises(ValueError, match=message):
        user_admin_router(UserAdminDeps(auth=world.deps, roster=lambda: world.roster, policy=policy))


# ── the roles an admin gives are the roles an invitation names (0.30) ──────


async def test_an_app_s_invitable_roles_are_the_roles_the_role_change_gives() -> None:
    world = AdminWorld(policy=replace(_auth_policy(), invitable_roles=("MEMBER",)))
    async with world.client:
        _, token = await world.admin_token()
        user = world.account()
        refused = await world.call(
            "POST", f"/admin/users/{user.id}/role", token=token, json={"role": "ADMIN", "acknowledged": True}
        )
        assert (refused.status_code, code(refused)) == (422, "role_not_grantable")


def test_grantable_and_invitable_roles_that_disagree_are_refused() -> None:
    from eifi1_server_kit.user_admin.router import UserAdminDeps, user_admin_router
    from tests.auth._world import World

    auth = World(policy=replace(_auth_policy(), invitable_roles=("MEMBER",))).deps
    with pytest.raises(ValueError, match="invitable_roles"):
        user_admin_router(
            UserAdminDeps(auth=auth, roster=lambda: None, policy=AdminPolicy(grantable=("ADMIN", "MEMBER")))
        )
    with pytest.raises(ValueError, match="OWNER"):
        bad = World(policy=replace(_auth_policy(), invitable_roles=("MEMBER", "OWNER"))).deps
        user_admin_router(UserAdminDeps(auth=bad, roster=lambda: None))
    user_admin_router(UserAdminDeps(auth=auth, roster=lambda: None, policy=AdminPolicy(grantable=("MEMBER",))))


def _auth_policy() -> Any:
    from tests.auth._world import POLICY

    return POLICY
