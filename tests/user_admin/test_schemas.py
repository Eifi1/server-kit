"""The wire shapes of §3–§6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from eifi1_server_kit.user_admin import (
    MAIL_BACKEND_CONSOLE,
    SUMMARY_ACTIVE_ADMINS,
    ActionConfirmation,
    ActiveChange,
    AdminAction,
    AdminActionRow,
    AdminUserRow,
    ConfirmationLevel,
    DeletionRequest,
    EmailChangeConfirm,
    EmailChangeRequest,
    InvitationCreate,
    InvitationRow,
    InvitationStatus,
    MailKind,
    MailRequest,
    MailResult,
    PersonRef,
    ReviewerUpdate,
    RoleChange,
    RolesChange,
    UserListResponse,
    invitation_status,
)

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def _user(**changes: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "email": "ada@example.com",
        "first_name": "Ada",
        "last_name": "Example",
        "locale": "hu",
        "role": "ADMIN",
        "is_active": True,
        "created_at": NOW,
        **changes,
    }


def test_the_admin_row_is_the_core_plus_the_admin_columns() -> None:
    row = AdminUserRow(**_user(last_login_at=NOW, extra={"budgets_owned": 2}))
    dumped = row.model_dump()
    assert dumped["display_name"] == "Example Ada", "the core's derived name, in the user's order"
    assert dumped["name_incomplete"] is False
    assert dumped["last_login_at"] == NOW and dumped["extra"] == {"budgets_owned": 2}
    assert dumped["password_change_required_at"] is None and dumped["deletion_scheduled_at"] is None
    assert dumped["translation_review_locales"] == [] and dumped["translation_review_areas"] is None


def test_the_admin_row_reads_an_orm_row() -> None:
    """keksdose's columns: a NULL locale list reads as none."""
    orm = SimpleNamespace(
        **_user(
            translation_review_locales=None,
            translation_review_areas=["legal"],
            deletion_requested_at=NOW,
            deletion_scheduled_at=NOW + timedelta(days=30),
        )
    )
    row = AdminUserRow.model_validate(orm)
    assert row.translation_review_locales == [] and row.translation_review_areas == ["legal"]
    assert row.deletion_scheduled_at == NOW + timedelta(days=30) and row.extra == {}


def test_the_list_answer_is_generic_over_the_apps_row() -> None:
    class Row(AdminUserRow):
        plan: str = "FREE"

    answer = UserListResponse[Row](users=[Row(**_user())], total=1, mail_backend="resend")
    assert answer.users[0].plan == "FREE" and answer.summary is None
    loose = UserListResponse.model_validate({"users": [_user()], "total": 1, "mail_backend": "console", "summary": {}})
    assert loose.levels is None
    leveled = UserListResponse.model_validate(
        {
            "users": [],
            "total": 0,
            "mail_backend": "resend",
            "summary": {SUMMARY_ACTIVE_ADMINS: 2},
            "levels": {"deactivate": "type_email", "role_change": "acknowledge"},
        }
    )
    assert leveled.levels == {"deactivate": ConfirmationLevel.TYPE_EMAIL, "role_change": ConfirmationLevel.ACKNOWLEDGE}
    assert leveled.model_dump(mode="json")["levels"]["deactivate"] == "type_email"
    assert isinstance(loose.users[0], AdminUserRow)
    with pytest.raises(ValidationError):
        UserListResponse(users=[], total=-1, mail_backend="console")


def test_every_action_body_carries_its_confirmation() -> None:
    assert ActionConfirmation().model_dump() == {"acknowledged": False, "confirm_email": None}
    assert ActiveChange(active=False, confirm_email="ADA@example.com").confirm_email == "ADA@example.com", (
        "typed as typed; require_confirmation normalises"
    )
    assert RoleChange(role=" ADMIN ", acknowledged=True).role == "ADMIN"
    assert MailRequest(kind="password_reset").kind is MailKind.PASSWORD_RESET
    with pytest.raises(ValidationError):
        ActiveChange(active=False, company_id=3)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RoleChange(role="")
    with pytest.raises(ValidationError):
        ActiveChange(active=False, confirm_email="a" * 255)


def test_kastlans_roles_are_one_or_more_without_repeats() -> None:
    assert RolesChange(roles=["ADMIN", "STAFF", "ADMIN"]).roles == ["ADMIN", "STAFF"]
    with pytest.raises(ValidationError):
        RolesChange(roles=[])


def test_a_mail_kind_names_its_audit_action() -> None:
    assert MailKind.VERIFICATION.action is AdminAction.MAIL_VERIFICATION
    assert MailKind.PASSWORD_RESET.action is AdminAction.MAIL_RESET
    with pytest.raises(ValidationError):
        MailRequest(kind="welcome")


def test_a_mail_answer_carries_a_link_only_on_the_console() -> None:
    """The shown-once rule: production never hands an admin somebody's reset link."""
    assert MailResult(sent=True, mail_backend=MAIL_BACKEND_CONSOLE, link="https://app.example/r?t=1").link
    assert MailResult(sent=False, mail_backend="resend").link is None
    with pytest.raises(ValidationError, match="only while mail goes to the console"):
        MailResult(sent=True, mail_backend="resend", link="https://app.example/r?t=1")


def test_reviewer_update() -> None:
    assert ReviewerUpdate(locales=["fr"]).areas is None
    assert ReviewerUpdate(locales=[], areas=["legal"]).locales == []
    with pytest.raises(ValidationError):
        ReviewerUpdate(locales=["x"] * 9)
    with pytest.raises(ValidationError):
        ReviewerUpdate(locales=["fr"], role="REVIEWER")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("columns", "status"),
    [
        ({}, InvitationStatus.OPEN),
        ({"expires_at": NOW}, InvitationStatus.EXPIRED),
        ({"accepted_at": NOW - timedelta(days=1), "revoked_at": NOW}, InvitationStatus.ACCEPTED),
        ({"revoked_at": NOW, "expires_at": NOW - timedelta(days=1)}, InvitationStatus.REVOKED),
        ({"expires_at": datetime(2026, 10, 7, 9, 0)}, InvitationStatus.EXPIRED),  # naive is UTC
    ],
)
def test_invitation_status(columns: dict[str, datetime], status: InvitationStatus) -> None:
    assert invitation_status(**{"expires_at": NOW + timedelta(days=14), "now": NOW, **columns}) is status


def test_invitation_create() -> None:
    created = InvitationCreate(email=" Bob@Example.com ", role="MEMBER", note="  ", locale="fr")
    assert (created.email, created.note, created.scope) == ("bob@example.com", None, None)
    assert InvitationCreate(email="bob@example.com", role="MEMBER").locale == "de-CH"
    team = InvitationCreate(email="bob@example.com", role="MEMBER", scope=7, note="  Welcome to the team ")
    assert (team.scope, team.note) == (7, "Welcome to the team")
    with pytest.raises(ValidationError):
        InvitationCreate(email="bob@example.com", role="MEMBER", note="x" * 256)
    with pytest.raises(ValidationError):
        InvitationCreate(email="bob@example.com", role="MEMBER", invited_by=1)  # type: ignore[call-arg]


def test_invitation_row() -> None:
    row = InvitationRow(
        id=3,
        email="bob@example.com",
        role=None,
        locale="de-CH",
        invited_by=SimpleNamespace(id=1, email="ada@example.com", first_name="Ada", last_name="Example"),
        created_at=NOW,
        expires_at=NOW + timedelta(days=14),
        status="open",
    )
    assert row.status is InvitationStatus.OPEN and row.link is None
    assert row.invited_by == PersonRef(id=1, email="ada@example.com", first_name="Ada", last_name="Example")
    assert PersonRef().model_dump() == {"id": None, "email": None, "first_name": "", "last_name": ""}


def test_admin_action_row() -> None:
    row = AdminActionRow.model_validate(
        SimpleNamespace(
            id=9,
            at=NOW,
            action="erase",
            actor_id=None,
            target_user_id=None,
            target_email=None,
            detail=None,
            company_id=None,
            actor=None,
        )
    )
    assert row.action is AdminAction.ERASE and row.detail == {} and row.actor is None
    # 0.5.1: an app's own action is kept as written, not refused.
    assert AdminActionRow(id=1, at=NOW, action="dance").action == "dance"


def test_email_change() -> None:
    request = EmailChangeRequest(new_email=" Ada+Keksdose@Example.com ", password="old one")
    assert request.new_email == "ada+keksdose@example.com", "normalised, the tag kept"
    with pytest.raises(ValidationError):
        EmailChangeRequest(new_email="not an address", password="x")
    with pytest.raises(ValidationError):
        EmailChangeRequest(new_email="ada@example.com", password="é" * 40)  # over bcrypt's 72 bytes
    assert EmailChangeConfirm(token="abc").token == "abc"
    with pytest.raises(ValidationError):
        EmailChangeConfirm(token="")


def test_deletion_request() -> None:
    request = DeletionRequest(password="secret", confirm_email="ADA@example.com ")
    assert request.confirm_email == "ADA@example.com ", "a typo is a mismatch later, not a 422 now"
    with pytest.raises(ValidationError):
        DeletionRequest(password="secret")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DeletionRequest(password="secret", confirm_email="ada@example.com", mode="now")  # type: ignore[call-arg]


def test_an_admin_action_row_takes_the_app_s_own_action() -> None:
    # 0.5.1: admin_action_record takes a str, so the row does too.
    row = AdminActionRow.model_validate({"id": 10, "at": NOW, "action": "message", "detail": {"kind": "announcement"}})
    assert row.action == "message" and not isinstance(row.action, AdminAction)
    assert AdminActionRow.model_validate({"id": 11, "at": NOW, "action": "erase"}).action is AdminAction.ERASE
    # 0.6: keksdose's stored "plan" rows read as the kit's action now (billing §6).
    plan = AdminActionRow.model_validate(
        {"id": 12, "at": NOW, "action": "plan", "detail": {"from": "free", "to": "pro"}}
    )
    assert plan.action is AdminAction.PLAN
