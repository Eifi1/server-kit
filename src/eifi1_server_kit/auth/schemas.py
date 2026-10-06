"""The wire shapes of sign-up, sign-in and the account (§4.2, §5.1, §6.1).

Lifted from keksdose ``backend/keksdose/domain/schemas/auth.py`` (cited per class), with
the contract's changes: first and last name instead of ``display_name``, a read-only
``display_name`` and ``name_incomplete`` derived from them, and the challenges'
discriminators as ``Literal[True]``. Every class is meant to be SUBCLASSED — the app adds
its fields and narrows a type::

    class RegisterRequest(kit.RegisterRequest):
        reporting_currency: CurrencyCode | None = None  # keksdose's app field

    class UserResponse(kit.UserResponse):
        role: UserRole                                  # the app's vocabulary
        is_demo: bool = False

        def name_completion_exempt(self) -> bool:      # §3.3: a demo is never incomplete
            return self.is_demo

    class PasswordChangeChallenge(kit.PasswordChangeChallenge):
        encrypted: bool = False                         # keksdose's app field (§5.1)

No ``email-validator``: kastlan does not install it, so :data:`Email` checks the shape
(one ``@``, a dotted domain, no spaces) and normalises (§3.1). The verification mail is
what proves an address; an app that wants ``EmailStr``'s stricter check re-declares the
field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    model_validator,
)

from eifi1_server_kit.auth.accounts import full_name, name_incomplete, normalise_email

#: bcrypt hashes at most 72 BYTES, and bcrypt 5 RAISES past it in both the hash and the
#: check (keksdose ``infrastructure/security.py`` ``BCRYPT_MAX_PASSWORD_BYTES``).
MAX_PASSWORD_BYTES = 72
#: The one hard rule a NEW password has (keksdose feedback #124); any checklist is advisory.
MIN_PASSWORD_LENGTH = 8
#: First and last name, after trimming (§3.1): the old ``display_name`` limit, so a
#: migrated name fits.
MAX_NAME_LENGTH = 120
#: RFC 5321's path limit, less the angle brackets.
MAX_EMAIL_LENGTH = 254
#: A locale tag as the UI sends it — ``de-CH``, ``en``, ``zh-Hans`` (keksdose
#: ``ProfileUpdateRequest.locale``). Not a list of shipped languages: an app narrows it.
LOCALE_PATTERN = r"^[A-Za-z]{2}(-[A-Za-z0-9]{2,4})?$"
#: What a new account is written to in when the form did not say: the UI's German, the
#: kit's fallback language (i18n H7).
DEFAULT_LOCALE = "de-CH"


def _normalised(value: object) -> object:
    return normalise_email(value) if isinstance(value, str) else value


def _plausible_email(value: str) -> str:
    """One ``@``, a non-empty local part of at most 64, a dotted domain, no whitespace."""
    local, at, domain = value.partition("@")
    labels = domain.split(".")
    if (
        not at
        or not local
        or len(local) > 64
        or "@" in domain
        or len(labels) < 2
        or not all(labels)
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("Invalid email address")
    return value


#: An address as it comes in: trimmed, lower-cased (a ``+tag`` kept), then checked for
#: shape — every entry normalises (§3.1).
Email = Annotated[
    str,
    BeforeValidator(_normalised),
    Field(max_length=MAX_EMAIL_LENGTH),
    AfterValidator(_plausible_email),
]


def _within_bcrypt_limit(value: str) -> str:
    if len(value.encode()) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes (non-ASCII characters count for more than one)"
        )
    return value


#: A NEW password: 8 characters at least, 72 bytes at most — a clean 422 where a password
#: is SET, because truncating would let two passwords open one account (keksdose
#: ``schemas/auth.py`` ``NewPassword``).
NewPassword = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH), AfterValidator(_within_bcrypt_limit)]
#: An EXISTING password presented for checking: no minimum (the account may predate the
#: rule), the same ceiling — an over-long one could not have been set (keksdose
#: ``ExistingPassword``).
ExistingPassword = Annotated[str, AfterValidator(_within_bcrypt_limit)]
#: A first or last name: 1–120 characters after trimming, any script (§3.1).
PersonName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)]
#: A locale tag (:data:`LOCALE_PATTERN`).
LocaleTag = Annotated[str, StringConstraints(strip_whitespace=True, pattern=LOCALE_PATTERN)]


class RegisterRequest(BaseModel):
    """``POST /auth/register`` (§4.2; keksdose ``UserCreate``).

    ``{email, password, first_name, last_name, locale, invite_token?}`` plus the app's
    fields in its subclass (kastlan's company, keksdose's currency). Unknown fields are
    refused: a role or a company id sent by a client is a 422, not silently dropped —
    they come from the invitation or the app's default, never from the form (§4.3).
    """

    model_config = ConfigDict(extra="forbid")

    email: Email
    password: NewPassword
    first_name: PersonName
    last_name: PersonName
    locale: LocaleTag = DEFAULT_LOCALE
    #: ``/register?invite=<token>`` (§4.4); its mail address must accept ``email``
    #: (:func:`~eifi1_server_kit.auth.invitation_accepts`).
    invite_token: str | None = Field(default=None, min_length=1, max_length=256)


class LoginRequest(BaseModel):
    """``POST /auth/login {email, password}`` (§5.1; keksdose ``UserLogin``)."""

    email: Email
    password: ExistingPassword


class TokenResponse[UserT = Any](BaseModel):
    """A session (§5.1; keksdose ``TokenResponse``): ``{access_token, refresh_token,
    token_type, user}``.

    ``refresh_token`` is ``None`` for a session that cannot be renewed (keksdose's demo:
    60 minutes, no refresh). ``user`` is the app's :class:`UserResponse` subclass —
    ``TokenResponse[MyUserResponse]`` — and ``Any`` unparametrised. An app that boots
    offline reads the user from here, so it carries ``name_incomplete`` too (§3.3).
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    user: UserT


class TwoFactorChallenge(BaseModel):
    """The password was right; the second factor is next (§5.1; keksdose
    ``TwoFactorChallengeResponse``) → ``POST /auth/login/2fa {challenge_token, code}``."""

    requires_2fa: Literal[True] = True
    challenge_token: str


class PasswordChangeChallenge(BaseModel):
    """The sign-in passed; the account must choose a new password before it gets a
    session (§5.1; keksdose ``PasswordChangeChallengeResponse``) →
    ``POST /auth/login/set-password``.

    A DIFFERENT discriminator from :class:`TwoFactorChallenge` on purpose: a client
    narrows the ``/auth/login`` union on the field's name. App fields ride along in a
    subclass — keksdose's ``encrypted`` — and the kit's form passes them through.
    """

    requires_password_change: Literal[True] = True
    challenge_token: str


class UserResponse(BaseModel):
    """``GET /auth/me`` (§6.1): the core every app answers; subclass to add its own.

    ``display_name`` and ``name_incomplete`` are DERIVED and read-only: computed from
    ``first_name`` / ``last_name`` on every serialisation, never accepted as input. The
    display name is written in the user's own ``locale`` — this is the user reading about
    themselves; the kit formats from first and last name on screen, in the reader's
    language, and uses ``display_name`` only as a fallback (§3.2).

    ``role`` is the app's vocabulary; narrow it to the app's enum in the subclass.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: As stored — already normalised; not re-validated on the way out.
    email: str
    first_name: str
    #: May be ``""`` on a migrated account until the name is completed (§3.3).
    last_name: str
    locale: str
    role: str
    is_active: bool
    email_verified: bool = False
    totp_enabled: bool = False
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        """The name in the user's language (:func:`~eifi1_server_kit.auth.full_name`)."""
        return full_name(self.first_name, self.last_name, self.locale)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def name_incomplete(self) -> bool:
        """Either part empty — the app shows ``CompleteNameDialog`` after sign-in (§3.3)."""
        return name_incomplete(self.first_name, self.last_name, is_demo=self.name_completion_exempt())

    def name_completion_exempt(self) -> bool:
        """Override to exempt an account from completing its name — keksdose's demo
        session (``return self.is_demo``, §3.3)."""
        return False


#: The fields of :class:`ProfileUpdate` that a PATCH may leave out but never null.
PROFILE_NOT_NULLABLE: tuple[str, ...] = ("first_name", "last_name", "locale")


class ProfileUpdate(BaseModel):
    """``PATCH /auth/me {first_name?, last_name?, locale?}`` (§6.1; Kurvenschmiede
    ``ProfileUpdate``).

    Every field optional, so the language picker writes the locale alone. Unknown fields
    are refused (``extra="forbid"``): the obvious next field somebody sends is ``role``
    or ``email``, and a schema that silently ignored it would read as having accepted it.
    An explicit ``null`` is refused too — a name cannot be cleared, and
    ``model_dump(exclude_unset=True)`` would otherwise carry it onto a NOT NULL column.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: PersonName | None = None
    last_name: PersonName | None = None
    locale: LocaleTag | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_explicit_nulls(cls, data: object) -> object:
        if isinstance(data, dict):
            for name in PROFILE_NOT_NULLABLE:
                if name in data and data[name] is None:
                    raise ValueError(f"{name} cannot be null; leave it out to keep it")
        return data
