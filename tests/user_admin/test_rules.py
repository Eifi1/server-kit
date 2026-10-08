"""Never yourself, never the last admin, and the confirmation the server decides (§4.1–§4.2)."""

from __future__ import annotations

import pytest

from eifi1_server_kit.user_admin import (
    ACCOUNT_ERROR_DETAIL,
    ACCOUNT_ERROR_STATUS,
    CONFIRMATION_LEVELS,
    AccountError,
    AccountErrorCode,
    AdminAction,
    ConfirmationLevel,
    confirm_email_matches,
    confirmation_level,
    refuse_last_admin,
    refuse_self,
    require_confirmation,
)

NONE, ACKNOWLEDGE, TYPE_EMAIL = ConfirmationLevel.NONE, ConfirmationLevel.ACKNOWLEDGE, ConfirmationLevel.TYPE_EMAIL


def test_the_codes_are_the_contracts_and_every_one_but_the_password_is_a_409() -> None:
    assert [str(code) for code in AccountErrorCode] == [
        "last_admin",
        "self_action",
        "other_companies",
        "household_has_members",
        "confirmation_required",
        "confirmation_mismatch",
        "password_incorrect",
    ]
    assert set(ACCOUNT_ERROR_STATUS) == set(AccountErrorCode)
    # A wrong current password on a signed-in route is a 400: a 401 would sign the user out.
    assert ACCOUNT_ERROR_STATUS[AccountErrorCode.PASSWORD_INCORRECT] == 400
    assert {
        status for code, status in ACCOUNT_ERROR_STATUS.items() if code is not AccountErrorCode.PASSWORD_INCORRECT
    } == {409}
    assert set(ACCOUNT_ERROR_DETAIL) == set(AccountErrorCode)
    assert not any("@" in detail for detail in ACCOUNT_ERROR_DETAIL.values()), "no detail echoes an address"


def test_account_error_carries_its_code_status_and_extra_fields() -> None:
    error = AccountError(AccountErrorCode.OTHER_COMPANIES)
    assert (error.code, error.status_code, str(error)) == (
        AccountErrorCode.OTHER_COMPANIES,
        409,
        "This account belongs to other companies too",
    )
    assert error.extra == {} and isinstance(error, ValueError)
    named = AccountError("last_admin", "Last admin of Example AG", extra={"companies": ["Example AG"]})  # type: ignore[arg-type]
    assert named.code is AccountErrorCode.LAST_ADMIN and str(named) == "Last admin of Example AG"
    assert named.extra == {"companies": ["Example AG"]}
    with pytest.raises(ValueError, match="sets code, detail itself"):
        AccountError(AccountErrorCode.LAST_ADMIN, extra={"detail": "x", "code": "y"})
    with pytest.raises(ValueError, match="not a valid AccountErrorCode"):
        AccountError("nope")  # type: ignore[arg-type]


def test_the_levels_are_the_contracts_table() -> None:
    """§4.2: type_email for deactivate, erase and transfer (and the user's own deletion,
    whose body types the address, §6.4); acknowledge for a forced password change, a role
    and a plan (billing §6)."""
    assert set(CONFIRMATION_LEVELS) == set(AdminAction)
    typed = {action for action, level in CONFIRMATION_LEVELS.items() if level is TYPE_EMAIL}
    acknowledged = {action for action, level in CONFIRMATION_LEVELS.items() if level is ACKNOWLEDGE}
    assert typed == {AdminAction.DEACTIVATE, AdminAction.ERASE, AdminAction.TRANSFER, AdminAction.DELETION_REQUEST}
    assert acknowledged == {AdminAction.PASSWORD_CHANGE_REQUIRE, AdminAction.ROLE, AdminAction.PLAN}
    assert confirmation_level("mail_reset") is NONE
    # keksdose's own word since 0.30, so its stored rows read as the kit's action.
    assert AdminAction("plan") is AdminAction.PLAN and confirmation_level("plan") is ACKNOWLEDGE
    assert confirmation_level(AdminAction.DEACTIVATE) is TYPE_EMAIL
    with pytest.raises(ValueError):
        confirmation_level("delete_everything")


def test_an_app_may_raise_a_level_but_never_lower_it() -> None:
    """keksdose: the reset mail and the forced change are type_email where a password opens the key."""
    assert confirmation_level(AdminAction.MAIL_RESET, at_least=TYPE_EMAIL) is TYPE_EMAIL
    assert confirmation_level(AdminAction.PASSWORD_CHANGE_REQUIRE, at_least="type_email") is TYPE_EMAIL
    assert confirmation_level(AdminAction.DEACTIVATE, at_least=NONE) is TYPE_EMAIL
    assert confirmation_level(AdminAction.ROLE, at_least=ACKNOWLEDGE) is ACKNOWLEDGE
    assert [level.rank for level in ConfirmationLevel] == [0, 1, 2]
    with pytest.raises(ValueError):
        confirmation_level(AdminAction.ROLE, at_least="checkbox")


def test_refuse_self() -> None:
    with pytest.raises(AccountError) as raised:
        refuse_self(7, 7)
    assert raised.value.code is AccountErrorCode.SELF_ACTION
    with pytest.raises(AccountError):
        refuse_self(7, "7")  # a path parameter read as a string is still yourself
    refuse_self(7, 8)


@pytest.mark.parametrize(
    ("target_is_admin", "active_admins", "change_removes_admin", "refused"),
    [
        (True, 1, True, True),  # the last active admin, deactivated or demoted
        (True, 0, True, True),  # an inconsistent count still refuses
        (True, 2, True, False),  # another admin remains
        (True, 1, False, False),  # the change keeps them admin (a reviewer scope, a mail)
        (False, 1, True, False),  # a deactivated admin or a member: nobody is removed
    ],
)
def test_refuse_last_admin(
    target_is_admin: bool, active_admins: int, change_removes_admin: bool, refused: bool
) -> None:
    """Kurvenschmiede ``_refuse_if_last_admin``: deactivation, demotion and a deletion request alike."""
    if refused:
        with pytest.raises(AccountError) as raised:
            refuse_last_admin(
                target_is_admin=target_is_admin, active_admins=active_admins, change_removes_admin=change_removes_admin
            )
        assert raised.value.code is AccountErrorCode.LAST_ADMIN and raised.value.status_code == 409
    else:
        refuse_last_admin(
            target_is_admin=target_is_admin, active_admins=active_admins, change_removes_admin=change_removes_admin
        )


@pytest.mark.parametrize(
    ("typed", "matches"),
    [
        ("ada@example.com", True),
        ("  ADA@Example.COM ", True),
        ("ada+kastlan@example.com", False),  # the tag is part of the identity
        ("ada@example.org", False),
        ("", False),
        ("   ", False),
        (None, False),
    ],
)
def test_confirm_email_matches_is_caseless_and_normalised(typed: str | None, matches: bool) -> None:
    assert confirm_email_matches("Ada@Example.com", typed) is matches


def _code(level: ConfirmationLevel | str, **kwargs: object) -> AccountErrorCode | None:
    try:
        require_confirmation(level, target_email="ada@example.com", **kwargs)  # type: ignore[arg-type]
    except AccountError as exc:
        return exc.code
    return None


def test_require_confirmation() -> None:
    required, mismatch = AccountErrorCode.CONFIRMATION_REQUIRED, AccountErrorCode.CONFIRMATION_MISMATCH
    assert _code(NONE) is None
    assert _code("none", confirm_email="wrong@example.com") is None
    # acknowledge: the checkbox, or the stronger typed address
    assert _code(ACKNOWLEDGE) is required
    assert _code(ACKNOWLEDGE, acknowledged=True) is None
    assert _code(ACKNOWLEDGE, confirm_email="ADA@example.com") is None
    assert _code(ACKNOWLEDGE, confirm_email="bob@example.com") is mismatch
    assert _code(ACKNOWLEDGE, confirm_email="  ") is required
    # type_email: only the address, never the checkbox alone
    assert _code(TYPE_EMAIL, acknowledged=True) is required
    assert _code(TYPE_EMAIL, confirm_email="") is required
    assert _code(TYPE_EMAIL, confirm_email="bob@example.com") is mismatch
    assert _code("type_email", confirm_email=" Ada@Example.com") is None
    with pytest.raises(ValueError):
        require_confirmation("maybe", target_email="ada@example.com")
