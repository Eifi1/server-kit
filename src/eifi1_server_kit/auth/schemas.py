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

from collections.abc import MutableMapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from eifi1_server_kit.auth.accounts import full_name, name_incomplete, normalise_email
from eifi1_server_kit.notifications.timing import is_known_zone
from eifi1_server_kit.settings import canonical_locale

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

#: A text size as stored on the account (``docs/text-size-harmonization.md`` §2.1,
#: §10.6): 100, 125 and 150 % of the browser's own font size.
TextSize = Literal["normal", "large", "xlarge"]
#: A contrast mode as stored on the account (text-size §2.4, §10.6). ``"system"`` follows
#: the device's ``prefers-contrast``; it is a stored value, not the absence of one.
ContrastMode = Literal["system", "standard", "more"]
#: The text sizes in the settings card's order (text-size §10.6) — the kit's
#: ``TextSizeSetting`` offers the same three.
TEXT_SIZES: tuple[TextSize, ...] = ("normal", "large", "xlarge")
#: The contrast modes in the settings card's order (text-size §10.6). ``"system"`` is one
#: of them, because the PATCH rule refuses an explicit ``null``: without it, somebody who
#: picked "More" could never go back to following the device.
CONTRAST_MODES: tuple[ContrastMode, ...] = ("system", "standard", "more")


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


#: The longest IANA name the account's zone columns hold (``String(64)``).
MAX_ZONE_LENGTH = 64


def _known_zone(value: str) -> str:
    if not is_known_zone(value):
        raise ValueError("must be an IANA zone name, such as Europe/Zurich")
    return value


def _known_zone_or_none(value: object) -> object:
    """A device's zone at sign-up: one this server doesn't know is dropped, never a 422 —
    a sign-up must not fail over an observation (the next app start reports again)."""
    return value if value is None or (isinstance(value, str) and is_known_zone(value)) else None


#: An IANA zone ``zoneinfo`` knows (``Europe/Zurich``), at most :data:`MAX_ZONE_LENGTH`
#: characters (notifications §10.3).
ZoneName = Annotated[str, Field(max_length=MAX_ZONE_LENGTH), AfterValidator(_known_zone)]
#: :func:`apply_zone_update`'s fields: pass them as ``apply_patch``'s ``skip``, so a demo's
#: zone is never written.
PROFILE_ZONE_FIELDS: tuple[str, ...] = ("device_timezone",)


def _as_utc(value: datetime) -> datetime:
    """Aware and in UTC: a naive value is read as UTC (SQLite, and kastlan's naive
    columns), an aware one converted."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


#: An instant on the wire, always ISO-8601 in UTC (``2026-10-08T09:00:00Z``): the client
#: counts down to it (landing-demo §5.3), so it must never be a local time without an
#: offset, which a browser would read in ITS zone.
UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


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
    #: The device's zone (``Intl…timeZone``, notifications §10.3, 0.16), stored as the
    #: account's ``device_timezone``; one this server doesn't know is dropped, not refused.
    device_timezone: Annotated[str | None, BeforeValidator(_known_zone_or_none)] = Field(
        default=None, max_length=MAX_ZONE_LENGTH
    )


class LoginRequest(BaseModel):
    """``POST /auth/login {email, password}`` (§5.1; keksdose ``UserLogin``)."""

    email: Email
    password: ExistingPassword


class TokenResponse[UserT = Any](BaseModel):
    """A session (§5.1; keksdose ``TokenResponse``): ``{access_token, refresh_token,
    token_type, user, expires_at}``.

    ``refresh_token`` is ``None`` for a session that cannot be renewed — the demo
    (``docs/landing-demo-harmonization.md`` §5.3), whose access token lives to the demo
    account's end. ``expires_at`` says when the access token ends; the demo MUST send it,
    because the client's countdown and its end page run from it and the client never
    decodes the JWT (§5.3). ``user`` is the app's :class:`UserResponse` subclass —
    ``TokenResponse[MyUserResponse]`` — and ``Any`` unparametrised. An app that boots
    offline reads the user from here, so it carries ``name_incomplete`` too (§3.3).
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    user: UserT
    #: When the access token ends, ISO-8601 in UTC: set for a demo session
    #: (:func:`~eifi1_server_kit.demo.demo_expires_at`); optional for any other.
    expires_at: UtcDateTime | None = None


class RefreshRequest(BaseModel):
    """``POST /auth/refresh {refresh_token}`` (auth §6.2, §11; keksdose, kastlan and
    Kurvenschmiede ``RefreshRequest``, field for field).

    The token is plain text, with no length rule: an empty or malformed one is the
    route's ``401 {code: "session_ended"}``, never a 422. ui-kit's
    ``attachSessionRefresh`` reads a 422 as a client fault that a sign-out would not mend
    (transient), so a 422 here would leave a device that holds a broken token signed in,
    refreshing at every request. What the route answers is
    :mod:`~eifi1_server_kit.auth.tokens`' "Refreshing a session".
    """

    refresh_token: str


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
    #: When a demo account ends, ISO-8601 in UTC (landing-demo §5.3): ``created_at`` plus
    #: the maximum age, :func:`~eifi1_server_kit.demo.demo_expires_at`. ``None`` for a real
    #: account. The ORM row rarely has it, so the app passes it — a property on its model
    #: read through ``from_attributes``, or ``model_copy(update=…)``.
    demo_expires_at: UtcDateTime | None = None
    #: The account's text size (text-size §6, §10.6); ``None`` = never chosen, so a device
    #: without its own choice uses Normal. The app's column, read through
    #: ``from_attributes``; an app without the column yet answers ``None``.
    text_size: TextSize | None = None
    #: The account's contrast mode (text-size §6, §10.6); ``None`` = never chosen, which
    #: reads as ``"system"``.
    contrast: ContrastMode | None = None
    #: The zone the device last used reported (notifications §10.3, 0.16), an IANA name;
    #: the client reports its own when it differs. It IS the account's zone — automatic
    #: only (amended 2026-10-20), ``None`` = the app's (``effective_zone``).
    device_timezone: str | None = None

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


#: The fields of :class:`ProfileUpdate` that a PATCH may leave out but never null. The
#: text size and the contrast are among them (text-size §10.6): their ``null`` means only
#: "never chosen", which a person cannot choose — "System" is a value of its own.
PROFILE_NOT_NULLABLE: tuple[str, ...] = (
    "first_name",
    "last_name",
    "locale",
    "text_size",
    "contrast",
    "device_timezone",
)


class ProfileUpdate(BaseModel):
    """``PATCH /auth/me {first_name?, last_name?, locale?, text_size?, contrast?,
    device_timezone?}`` (§6.1;
    Kurvenschmiede ``ProfileUpdate``; ``docs/text-size-harmonization.md`` §6, §10.6).

    Every field optional, so the language picker writes the locale alone, and the text-size
    and contrast cards write theirs alone (a pick writes the device and the account). Unknown fields
    are refused (``extra="forbid"``): the obvious next field somebody sends is ``role``
    or ``email``, and a schema that silently ignored it would read as having accepted it.
    An explicit ``null`` is refused too — a name cannot be cleared, and
    ``model_dump(exclude_unset=True)`` would otherwise carry it onto a NOT NULL column.
    Write it with :func:`~eifi1_server_kit.settings.apply_patch` and
    :data:`PROFILE_NOT_NULLABLE` (settings §6.1).

    **The locale, from the app's languages** (settings §6.2): set :attr:`offered_locales`
    in a subclass — or build the class with
    :func:`~eifi1_server_kit.settings.profile_update_model` — and the locale goes through
    :func:`~eifi1_server_kit.settings.canonical_locale`: ``de_ch`` is stored as ``de-CH``,
    ``de`` as the offered German, and a language the app doesn't offer is a 422. Unset,
    only the tag's shape is checked, as before.

    **The zone** (notifications §10.3, 0.16; amended 2026-10-20, Marcel: automatic only):
    ``device_timezone``, the device's report (never ``null``), an IANA name ``zoneinfo``
    knows; one it doesn't know (a browser's tzdata newer than the server's), or any value
    that isn't a name, is **dropped** — left unset, never written — as
    :class:`RegisterRequest` drops it (0.17.0; ``null`` stays a 422). There is no pick: a ``timezone`` field — only a stale ≤0.45 bundle's
    picker still sends one — is **a 422 from 0.18** as any unknown field is (ignored in 0.16
    and 0.17). A demo's zone is ignored, as
    its locale and appearance are in Kurvenschmiede::

        apply_patch(user, body, not_nullable=PROFILE_NOT_NULLABLE, skip=PROFILE_ZONE_FIELDS)
        apply_zone_update(user, body, is_demo=user.is_demo)
    """

    model_config = ConfigDict(extra="forbid")

    #: The app's languages in its menu's order (``("de-CH", "en", "fr", "it")``); ``None``
    #: checks the tag's shape only (:data:`LOCALE_PATTERN`).
    offered_locales: ClassVar[Sequence[str] | None] = None

    first_name: PersonName | None = None
    last_name: PersonName | None = None
    locale: LocaleTag | None = None
    #: One of :data:`TEXT_SIZES`; left out keeps the account's, ``null`` is refused.
    text_size: TextSize | None = None
    #: One of :data:`CONTRAST_MODES`; left out keeps the account's, ``null`` is refused.
    contrast: ContrastMode | None = None
    #: The device's zone, reported at app start when it differs from the stored one; a
    #: report, so ``null`` is refused, and one this server doesn't know is dropped (left
    #: unset, 0.17.0). Write it with :func:`apply_zone_update`, which ignores a demo's.
    device_timezone: ZoneName | None = None

    @field_validator("locale", mode="before")
    @classmethod
    def _offered_locale(cls, value: object) -> object:
        """Canonical BEFORE the pattern runs, so ``de_ch`` is read as ``de-CH`` rather than
        refused for its underscore."""
        if cls.offered_locales is None or not isinstance(value, str):
            return value
        locale = canonical_locale(value, cls.offered_locales)
        if locale is None:
            raise ValueError(f"locale must be one of {', '.join(cls.offered_locales)}")
        return locale

    @model_validator(mode="before")
    @classmethod
    def _refuse_explicit_nulls(cls, data: object) -> object:
        if isinstance(data, dict):
            for name in PROFILE_NOT_NULLABLE:
                if name in data and data[name] is None:
                    raise ValueError(f"{name} cannot be null; leave it out to keep it")
            if "device_timezone" in data and _known_zone_or_none(data["device_timezone"]) is None:
                # A zone this server doesn't know: dropped as at sign-up, so a report from a
                # browser with newer tzdata doesn't fail on every start (0.17.0).
                data = {key: value for key, value in data.items() if key != "device_timezone"}
        return data


def apply_zone_update(
    user: object | MutableMapping[str, object], patch: BaseModel, *, is_demo: bool
) -> dict[str, object]:
    """Write the zone field the PATCH sent (:data:`PROFILE_ZONE_FIELDS`: the device's
    report) onto ``user`` — an ORM row (``setattr``) or a mutable mapping — and answer it;
    nothing for a demo, whose device zone is never stored (notifications §10.3 Settled).
    ``patch`` is a :class:`ProfileUpdate` (or any model with the field); left out keeps."""
    if is_demo:
        return {}
    written: dict[str, object] = {}
    for name in PROFILE_ZONE_FIELDS:
        if name in patch.model_fields_set:
            value = getattr(patch, name)
            if isinstance(user, MutableMapping):
                user[name] = value
            else:
                setattr(user, name, value)
            written[name] = value
    return written
