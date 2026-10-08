"""The wire shapes (§4.2, §5.1, §6.1) and the coded refusals (§5.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from eifi1_server_kit.auth import (
    AUTH_ERROR_STATUS,
    CONTRAST_MODES,
    MAX_NAME_LENGTH,
    PROFILE_NOT_NULLABLE,
    TEXT_SIZES,
    AuthError,
    AuthErrorCode,
    LoginRequest,
    PasswordChangeChallenge,
    ProfileUpdate,
    RegisterRequest,
    TokenResponse,
    TwoFactorChallenge,
    UserResponse,
)

ADA: dict[str, Any] = {
    "email": "ada@example.com",
    "password": "correct horse",
    "first_name": "Ada",
    "last_name": "Example",
}
CREATED = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def _user(**changes: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "email": "ada@example.com",
        "first_name": "Ada",
        "last_name": "Example",
        "locale": "en",
        "role": "MEMBER",
        "is_active": True,
        "created_at": CREATED,
        **changes,
    }


def test_register_normalises_and_trims() -> None:
    request = RegisterRequest(**{**ADA, "email": "  Ada+Kastlan@Example.COM ", "first_name": " Ada  "})
    assert request.email == "ada+kastlan@example.com", "lower-cased, the tag kept"
    assert request.first_name == "Ada"
    assert request.locale == "de-CH" and request.invite_token is None
    assert RegisterRequest(**ADA, locale="zh-Hans", invite_token="t").locale == "zh-Hans"


@pytest.mark.parametrize(
    "email",
    [
        "",
        "ada",
        "ada@",
        "@example.com",
        "ada@example",
        "ada@@example.com",
        "a da@example.com",
        "ada@example..com",
        "ada@.example.com",
        "ada@example.com.",
        "ada\x00@example.com",
        f"{'a' * 65}@example.com",
        f"ada@{'e' * 250}.com",
    ],
)
def test_register_refuses_an_implausible_address(email: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(**{**ADA, "email": email})


def test_unicode_and_tagged_addresses_are_plausible() -> None:
    assert RegisterRequest(**{**ADA, "email": "Zoë+kastlan@Bücher.example"}).email == "zoë+kastlan@bücher.example"


def test_the_password_rules_are_eight_characters_and_72_bytes() -> None:
    """§8 / rule 3: no rule of the kit's own beyond these."""
    with pytest.raises(ValidationError, match="at least 8"):
        RegisterRequest(**{**ADA, "password": "short"})
    assert RegisterRequest(**{**ADA, "password": "a" * 72}).password == "a" * 72
    with pytest.raises(ValidationError, match="at most 72 bytes"):
        RegisterRequest(**{**ADA, "password": "a" * 73})
    with pytest.raises(ValidationError, match="at most 72 bytes"):
        RegisterRequest(**{**ADA, "password": "ü" * 37})  # 74 bytes in 37 characters
    assert LoginRequest(email="ADA@example.com", password="x").email == "ada@example.com", "no minimum to sign in"
    with pytest.raises(ValidationError, match="at most 72 bytes"):
        LoginRequest(email="ada@example.com", password="a" * 73)


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_names_are_1_to_120_characters_after_trimming(field: str) -> None:
    for bad in ("", "   ", "x" * (MAX_NAME_LENGTH + 1)):
        with pytest.raises(ValidationError):
            RegisterRequest(**{**ADA, field: bad})
    assert getattr(RegisterRequest(**{**ADA, field: f"  {'x' * MAX_NAME_LENGTH}  "}), field) == "x" * MAX_NAME_LENGTH
    assert getattr(RegisterRequest(**{**ADA, field: "李"}), field) == "李", "any script"


def test_register_refuses_what_the_form_never_decides() -> None:
    """§4.3: role and associations come from the invitation or the default, never the form."""
    for extra in ({"role": "ADMIN"}, {"company_id": 3}, {"display_name": "Ada"}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            RegisterRequest(**{**ADA, **extra})
    with pytest.raises(ValidationError):
        RegisterRequest(**{**ADA, "locale": "German"})
    with pytest.raises(ValidationError):
        RegisterRequest(**{**ADA, "invite_token": ""})


def test_an_app_adds_its_fields_by_subclassing() -> None:
    class KastlanRegister(RegisterRequest):
        company_name: str | None = None

    assert KastlanRegister(**ADA, company_name="Example AG").company_name == "Example AG"
    with pytest.raises(ValidationError, match="Extra inputs"):
        KastlanRegister.model_validate({**ADA, "role": "ADMIN"})


def test_user_response_derives_display_name_and_name_incomplete() -> None:
    user = UserResponse(**_user())
    dumped = user.model_dump()
    assert dumped["display_name"] == "Ada Example" and dumped["name_incomplete"] is False
    assert UserResponse(**_user(locale="hu")).display_name == "Example Ada"
    migrated = UserResponse(**_user(first_name="Ada Lovelace", last_name=""))
    assert migrated.display_name == "Ada Lovelace" and migrated.name_incomplete
    assert dumped["email_verified"] is False and dumped["totp_enabled"] is False
    schema = UserResponse.model_json_schema(mode="serialization")
    assert schema["properties"]["display_name"]["readOnly"] is True
    assert "display_name" not in UserResponse.model_json_schema(mode="validation")["properties"]


def test_user_response_reads_an_orm_row_and_ignores_a_stale_display_name() -> None:
    class Row:
        id = 2
        email = "bob@example.com"
        first_name = "Bob"
        last_name = "Example"
        display_name = "Old Bob"
        locale = "zh"
        role = "MEMBER"
        is_active = True
        email_verified = True
        totp_enabled = True
        created_at = CREATED

    user = UserResponse.model_validate(Row())
    assert user.display_name == "Bob Example" and user.email_verified and user.totp_enabled


def test_a_demo_session_is_never_incomplete() -> None:
    """§3.3: keksdose overrides the hook with its ``is_demo``."""

    class KeksdoseUser(UserResponse):
        role: Literal["ADMIN", "MEMBER"]
        is_demo: bool = False

        def name_completion_exempt(self) -> bool:
            return self.is_demo

    assert KeksdoseUser(**_user(last_name="")).name_incomplete
    assert not KeksdoseUser(**_user(last_name="", is_demo=True)).name_incomplete
    with pytest.raises(ValidationError):
        KeksdoseUser(**_user(role="GUEST"))


def test_token_response_is_generic_over_the_apps_user() -> None:
    plain = TokenResponse(access_token="a", user={"anything": 1})
    assert plain.refresh_token is None and plain.token_type == "bearer" and plain.user == {"anything": 1}
    typed = TokenResponse[UserResponse](access_token="a", refresh_token="r", user=_user(last_name=""))
    assert isinstance(typed.user, UserResponse)
    body = typed.model_dump(mode="json")
    assert body["refresh_token"] == "r"
    assert body["user"]["name_incomplete"] is True, "the sign-in answer carries it too (§3.3)"
    with pytest.raises(ValidationError):
        TokenResponse[UserResponse](access_token="a", user={"id": "x"})


def test_a_demo_session_says_when_it_ends_in_utc() -> None:
    """landing-demo §5.3: ``expires_at`` and ``demo_expires_at``, ISO-8601 in UTC, so the
    client counts down to an instant and never reads a local time in its own zone."""
    ends = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    session = TokenResponse[UserResponse](access_token="a", user=_user(demo_expires_at=ends), expires_at=ends)
    body = session.model_dump(mode="json")
    assert body["refresh_token"] is None and body["expires_at"] == "2026-10-08T09:00:00Z"
    assert body["user"]["demo_expires_at"] == "2026-10-08T09:00:00Z"
    # A naive value (SQLite, kastlan's columns) is read as UTC; another zone is converted.
    naive = TokenResponse(access_token="a", user={}, expires_at=datetime(2026, 10, 8, 9, 0))
    assert naive.model_dump(mode="json")["expires_at"] == "2026-10-08T09:00:00Z"
    zurich = datetime(2026, 10, 8, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    assert UserResponse(**_user(demo_expires_at=zurich)).demo_expires_at == ends
    # A real session and a real account leave them out.
    real = TokenResponse[UserResponse](access_token="a", refresh_token="r", user=_user())
    assert real.expires_at is None and real.user.demo_expires_at is None
    assert real.model_dump(mode="json")["user"]["demo_expires_at"] is None


def test_the_challenges_are_discriminated_by_their_field_name() -> None:
    assert TwoFactorChallenge(challenge_token="c").model_dump() == {"requires_2fa": True, "challenge_token": "c"}
    assert PasswordChangeChallenge(challenge_token="c").model_dump() == {
        "requires_password_change": True,
        "challenge_token": "c",
    }
    with pytest.raises(ValidationError):
        TwoFactorChallenge(requires_2fa=False, challenge_token="c")

    class KeksdoseChallenge(PasswordChangeChallenge):
        encrypted: bool = False

    assert KeksdoseChallenge(challenge_token="c", encrypted=True).model_dump()["encrypted"] is True


def test_profile_update() -> None:
    assert ProfileUpdate(locale="fr").model_dump(exclude_unset=True) == {"locale": "fr"}
    assert ProfileUpdate(first_name=" Ada ", last_name="Example").first_name == "Ada"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProfileUpdate.model_validate({"role": "ADMIN"})
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProfileUpdate.model_validate({"display_name": "Ada"})
    for field in ("first_name", "last_name", "locale"):
        with pytest.raises(ValidationError, match="cannot be null"):
            ProfileUpdate.model_validate({field: None})
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({"first_name": "  "})
    with pytest.raises(ValidationError, match="valid dictionary"):
        ProfileUpdate.model_validate(["first_name", None])


def test_the_text_size_and_contrast_vocabularies_are_the_contracts() -> None:
    """text-size §10.6: three sizes, and "system" as a stored contrast value."""
    assert TEXT_SIZES == ("normal", "large", "xlarge")
    assert CONTRAST_MODES == ("system", "standard", "more")


def test_the_account_carries_its_text_size_and_contrast() -> None:
    """``/auth/me`` answers both, ``None`` while never chosen (text-size §6)."""
    fresh = UserResponse(**_user())
    assert (fresh.text_size, fresh.contrast) == (None, None)
    chosen = UserResponse(**_user(text_size="xlarge", contrast="system")).model_dump()
    assert (chosen["text_size"], chosen["contrast"]) == ("xlarge", "system")
    with pytest.raises(ValidationError):
        UserResponse(**_user(text_size="huge"))
    with pytest.raises(ValidationError):
        UserResponse(**_user(contrast="high"))


def test_the_profile_update_writes_them_alone_and_never_clears_them() -> None:
    """A pick writes one field; an explicit null is refused — "System" is the way back
    (text-size §10.6, settings §6.1 rule 3)."""
    for size in TEXT_SIZES:
        assert ProfileUpdate(text_size=size).model_dump(exclude_unset=True) == {"text_size": size}
    for mode in CONTRAST_MODES:
        assert ProfileUpdate(contrast=mode).model_dump(exclude_unset=True) == {"contrast": mode}
    assert {"text_size", "contrast"} <= set(PROFILE_NOT_NULLABLE)
    for field in ("text_size", "contrast"):
        with pytest.raises(ValidationError, match="cannot be null"):
            ProfileUpdate.model_validate({field: None})
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({"text_size": "Large"})
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({"contrast": "high"})


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        (AuthErrorCode.INVALID_CREDENTIALS, 401),
        (AuthErrorCode.REGISTRATION_CLOSED, 403),
        (AuthErrorCode.EMAIL_TAKEN, 409),
        (AuthErrorCode.INVITATION_INVALID, 400),
        (AuthErrorCode.INVITATION_EXPIRED, 400),
        (AuthErrorCode.TOKEN_INVALID, 400),
        (AuthErrorCode.TOKEN_EXPIRED, 400),
    ],
)
def test_auth_errors_carry_their_code_and_status(code: AuthErrorCode, status_code: int) -> None:
    error = AuthError(code)
    assert isinstance(error, ValueError)
    assert (error.code, error.status_code) == (code, status_code) == (code, AUTH_ERROR_STATUS[code])
    assert str(error) and str(code) == code.value
    assert str(AuthError(code, "More words")) == "More words"


def test_auth_error_takes_the_wire_value_too() -> None:
    error = AuthError("email_taken")  # type: ignore[arg-type]
    assert error.code is AuthErrorCode.EMAIL_TAKEN and str(error) == "Email already registered"
    with pytest.raises(ValueError, match="not a valid AuthErrorCode"):
        AuthError("nope")  # type: ignore[arg-type]
