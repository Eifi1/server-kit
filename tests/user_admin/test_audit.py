"""The ``admin_actions`` row (§4.3): ids, roles, flags and counts — never content, never a secret."""

from __future__ import annotations

import enum
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from eifi1_server_kit.user_admin import (
    DETAIL_MAX_BYTES,
    DETAIL_TOKEN_MAX_LENGTH,
    AdminAction,
    AdminActionRow,
    AuditDetailError,
    GroupRef,
    admin_action_record,
    audit_detail,
)

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


class Role(enum.StrEnum):
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class Level(enum.IntEnum):
    TWO = 2


def test_the_actions_are_the_contracts() -> None:
    assert [str(action) for action in AdminAction] == [
        "deactivate",
        "reactivate",
        "role",
        "membership_remove",
        "password_change_require",
        "password_change_withdraw",
        "mail_verification",
        "mail_reset",
        "reviewer",
        "invite",
        "invite_resend",
        "invite_revoke",
        "transfer",
        "deletion_request",
        "deletion_cancel",
        "erase",
        "plan",
        # 0.8 (§11.3): what is done to a group, and to who is in it.
        "group_create",
        "group_rename",
        "group_delete",
        "membership_add",
        "membership_role",
        "group_deactivate",
        "group_reactivate",
    ]


def test_the_record_is_the_rows_fields() -> None:
    record = admin_action_record(
        AdminAction.ROLE,
        actor_id=1,
        target_user_id=2,
        target_email="  Ada@Example.com ",
        detail={"from": Role.MEMBER, "to": Role.ADMIN},
        now=NOW,
    )
    assert record == {
        "at": NOW,
        "actor_id": 1,
        "action": "role",
        "target_user_id": 2,
        "target_email": "ada@example.com",
        "detail": {"from": "MEMBER", "to": "ADMIN"},
    }
    assert type(record["action"]) is str, "the plain value, for any driver"
    json.dumps(record["detail"])


def test_kastlans_company_rides_along_only_when_given() -> None:
    company = admin_action_record("deactivate", actor_id=1, target_user_id=2, target_email=None, company_id=5, now=NOW)
    assert company["company_id"] == 5 and company["detail"] == {} and company["target_email"] is None
    platform = admin_action_record("erase", actor_id=None, target_user_id=2, target_email="ada@example.com", now=NOW)
    assert "company_id" not in platform and platform["actor_id"] is None


def test_the_group_columns_ride_along_only_when_given() -> None:
    """§11.8: a table without the columns (kastlan's, keksdose's) takes the record as it is."""
    plain = admin_action_record("membership_remove", actor_id=1, target_user_id=2, target_email=None, now=NOW)
    assert "target_group_id" not in plain and "target_group_name" not in plain
    added = admin_action_record(
        AdminAction.MEMBERSHIP_ADD,
        actor_id=2,  # an accepted invitation: the invitee is the actor
        target_user_id=2,
        target_email="ben@example.com",
        detail={"role": "MEMBER"},
        target_group_id=7,
        now=NOW,
    )
    assert added["action"] == "membership_add" and added["target_group_id"] == 7
    assert "target_group_name" not in added, "a name is stamped on a group_delete row only"
    invite = admin_action_record(
        "invite", actor_id=1, target_user_id=None, target_email="c@example.com", target_group_id=7, now=NOW
    )
    assert invite["target_group_id"] == 7 and invite["detail"] == {}


def test_a_deleted_group_keeps_its_name_on_its_group_delete_row() -> None:
    """Decision 6 (KS 16): the stamp, beside the counts of what went with it."""
    deleted = admin_action_record(
        AdminAction.GROUP_DELETE,
        actor_id=1,
        target_user_id=None,
        target_email=None,
        detail={"members": 3, "grants": 2, "invitations": 1},
        target_group_id=7,
        target_group_name="Paint shop",
        now=NOW,
    )
    assert deleted["target_group_id"] == 7 and deleted["target_group_name"] == "Paint shop"
    assert deleted["detail"] == {"members": 3, "grants": 2, "invitations": 1}
    with pytest.raises(ValueError, match="group_delete row only"):
        admin_action_record(
            "group_rename",
            actor_id=1,
            target_user_id=None,
            target_email=None,
            target_group_id=7,
            target_group_name="New",
            now=NOW,
        )
    with pytest.raises(ValueError, match="beside its target_group_id"):
        admin_action_record(
            "group_delete", actor_id=1, target_user_id=None, target_email=None, target_group_name="Paint shop", now=NOW
        )


def test_the_row_reads_the_group() -> None:
    """The column as written, and the group the app joined: live, else the stamp (§11.8)."""
    record = admin_action_record(
        "group_delete",
        actor_id=1,
        target_user_id=None,
        target_email=None,
        target_group_id=7,
        target_group_name="Paint shop",
        now=NOW,
    )
    row = AdminActionRow.model_validate(
        {"id": 3, **record, "target_group": {"id": 7, "name": record["target_group_name"]}}
    )
    assert row.action is AdminAction.GROUP_DELETE and row.target_group_id == 7
    assert row.target_group == GroupRef(id=7, name="Paint shop")
    assert "target_group_name" not in row.model_dump(), "read back through target_group"
    gone = AdminActionRow.model_validate(
        {"id": 4, "at": NOW, "action": "membership_add", "target_group_id": 8, "target_group": {"id": 8}}
    )
    assert gone.target_group == GroupRef(id=8, name=None), "no name left: the page reads 'a deleted team'"
    person = AdminActionRow.model_validate({"id": 5, "at": NOW, "action": "deactivate"})
    assert person.target_group_id is None and person.target_group is None


def test_now_is_stored_as_given_for_a_naive_column() -> None:
    """kastlan's auth timestamps are naive UTC."""
    naive = datetime(2026, 10, 7, 9, 0)
    assert (
        admin_action_record("invite", actor_id=1, target_user_id=None, target_email="b@example.com", now=naive)["at"]
        is naive
    )


def test_an_apps_own_action_is_kept_as_written() -> None:
    """keksdose found it after 0.5.1: ``AdminActionRow`` read an app's own action back, but
    the record refused to write one. ``"plan"`` is the kit's own since 0.6 (billing §6)."""
    plan = admin_action_record("plan", actor_id=1, target_user_id=2, target_email=None, now=NOW)
    assert plan["action"] == "plan" and type(plan["action"]) is str
    own = admin_action_record("reseed_demo", actor_id=1, target_user_id=None, target_email=None, now=NOW)
    assert own["action"] == "reseed_demo" and type(own["action"]) is str
    assert AdminActionRow.model_validate({"id": 1, **own}).action == "reseed_demo"


@pytest.mark.parametrize("action", ["", "   ", None])
def test_an_empty_action_is_refused(action: str | None) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        admin_action_record(action, actor_id=1, target_user_id=2, target_email=None, now=NOW)  # type: ignore[arg-type]


def test_the_detail_holds_ids_roles_flags_counts_and_dates() -> None:
    detail = audit_detail(
        {
            "roles": ("ADMIN", "PROPERTY_MANAGER"),
            "locales": ["de-CH", "zh-Hans"],
            "areas": None,
            "sessions_ended": True,
            "counts": {"projects": 3, "setpoint_sessions": 0},
            "before": {"roles": ["MEMBER"]},
            "ratio": 0.5,
            "level": Level.TWO,
            "scheduled_at": datetime(2026, 11, 6, 9, 0, 0, 123456, tzinfo=UTC),
            "on": date(2026, 11, 6),
            "kind": "password_reset",
            "password_change_required": True,
            "totp_enabled": False,
            "key_epoch": 3,
            "empty": "",
        }
    )
    assert detail["roles"] == ["ADMIN", "PROPERTY_MANAGER"]
    assert detail["scheduled_at"] == "2026-11-06T09:00:00.123456+00:00" and detail["on"] == "2026-11-06"
    assert detail["level"] == 2 and detail["counts"] == {"projects": 3, "setpoint_sessions": 0}
    assert audit_detail(None) == {}


@pytest.mark.parametrize(
    ("detail", "message"),
    [
        # Content: Kurvenschmiede encrypts notes, titles and bodies at rest (§9.6).
        ({"note": "Please welcome Ada"}, "never content"),
        ({"title": "Gear-ratio"}, None),  # a single word is a token and passes — see below
        ({"email": "ada@example.com"}, "never content"),
        ({"x": "y" * (DETAIL_TOKEN_MAX_LENGTH + 1)}, "never content"),
        ({"x": "line\nbreak"}, "never content"),
        ({"x": ["fine", "not fine"]}, r"detail\.x\[1\]"),
        ({"a sentence": 1}, "never content"),
        # Secrets, by name or by shape.
        ({"token": "abc"}, "looks like a secret"),
        ({"password": 123456}, "looks like a secret"),
        ({"backup_codes": ["abcd-efgh"]}, r"detail\.backup_codes\[0\]: looks like a secret"),
        ({"ref": "a" * 64}, "looks like a secret"),
        ({"x": "$2b$12$abcdefghijklmnop"}, "never content"),
        # Shape.
        ({"x": object()}, "not object"),
        ({"x": b"bytes"}, "not bytes"),
        ({"x": float("inf")}, "not a finite number"),
        ({1: "x"}, "keys are strings"),
        ({"a": {"b": {"c": {"d": 1}}}}, r"detail\.a\.b\.c\.d: nested deeper than 3"),
        ({"many": ["x" * 60] * 40}, f"larger than {DETAIL_MAX_BYTES} bytes"),
    ],
)
def test_the_detail_refuses_what_could_carry_content_or_a_secret(detail: dict[Any, Any], message: str | None) -> None:
    if message is None:
        # A one-word title is indistinguishable from a role; the rule bounds what slips
        # through to a token — no sentence, no note, no address.
        assert audit_detail(detail) == detail
        return
    with pytest.raises(AuditDetailError, match=message):
        audit_detail(detail)


def test_the_detail_is_an_object() -> None:
    with pytest.raises(AuditDetailError, match="detail is an object, not list"):
        audit_detail(["ADMIN"])  # type: ignore[arg-type]
    with pytest.raises(AuditDetailError):
        admin_action_record(
            "role", actor_id=1, target_user_id=2, target_email=None, detail={"note": "two words"}, now=NOW
        )


def test_a_company_rename_logs_both_names() -> None:
    # K8 (round 047): kastlan's company rename logs {from, to}, the names as written.
    record = admin_action_record(
        AdminAction.GROUP_RENAME,
        actor_id=1,
        target_user_id=None,
        target_email=None,
        detail={"from": " Test AG ", "to": "Müller & Söhne Zürich AG", "by_operator": True},
        company_id=7,
        now=NOW,
    )
    assert record["detail"] == {"from": "Test AG", "to": "Müller & Söhne Zürich AG", "by_operator": True}
    row = AdminActionRow.model_validate({"id": 1, **record})
    assert row.action == AdminAction.GROUP_RENAME and row.detail["to"] == "Müller & Söhne Zürich AG"
    # The keyword is the seam; a team's rename keeps {} and a 200-character name fits.
    assert audit_detail({}, action="group_rename") == {}
    assert audit_detail({"to": "x" * 200}, action=AdminAction.GROUP_RENAME) == {"to": "x" * 200}


@pytest.mark.parametrize(
    ("action", "detail", "message"),
    [
        ("group_rename", {"from": "Test AG", "to": "x" * 201}, r"detail\.to: a group's name is 1 to 200"),
        ("group_rename", {"from": "   ", "to": "Test AG"}, r"detail\.from: a group's name is 1 to 200"),
        ("group_rename", {"from": "Test\nAG", "to": "Test AG"}, r"detail\.from: .*control characters"),
        ("group_rename", {"from": "Test AG", "to": "Test​AG"}, r"detail\.to: .*control characters"),
        ("group_rename", {"from": "Test\u2028AG", "to": "Test AG"}, r"detail\.from: .*control characters"),
        ("group_rename", {"from": "Test AG", "to": 3}, r"detail\.to: a group's name is a string"),
        ("group_rename", {"from": "Test AG", "to": "a" * 64}, "looks like a secret"),
        # Only the names are free text: everything else in the detail is still a token.
        ("group_rename", {"from": "A", "to": "B", "note": "two words"}, r"detail\.note: .*never content"),
        ("group_rename", {"before": {"from": "Test AG"}}, r"detail\.before\.from: .*never content"),
        # Any other action: a name is content.
        ("membership_role", {"from": "Test AG", "to": "Test AG Bern"}, r"detail\.from: .*never content"),
        ("group_delete", {"from": "Test AG"}, "never content"),
        (None, {"from": "Test AG", "to": "Test AG Bern"}, "never content"),
    ],
)
def test_only_a_renames_names_are_free_text(action: str | None, detail: dict[str, Any], message: str) -> None:
    with pytest.raises(AuditDetailError, match=message):
        audit_detail(detail, action=action)
    if action is not None:
        with pytest.raises(AuditDetailError, match=message):
            admin_action_record(action, actor_id=1, target_user_id=None, target_email=None, detail=detail, now=NOW)
