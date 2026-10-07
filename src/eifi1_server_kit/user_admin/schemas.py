"""The wire shapes of user administration and the account's own settings.

``docs/user-admin-harmonization.md`` §3–§6 in ``Eifi1/ui-kit``. As with the auth schemas,
every class is meant to be SUBCLASSED: the app narrows ``role`` to its enum and adds its
fields::

    class AdminUserRow(kit.AdminUserRow):
        role: UserRole
        is_demo: bool = False

    class RoleChange(kit.RoleChange):
        role: UserRole

The request bodies refuse unknown fields (``extra="forbid"``), as ``ProfileUpdate`` does:
the next field somebody sends is ``email`` or ``company_id``, and a body that silently
ignored it would read as having accepted it.

**Confirmations travel in the body** (§4.2): every admin action's body is an
:class:`ActionConfirmation` — ``acknowledged`` for the ``acknowledge`` level,
``confirm_email`` for ``type_email`` — and
:func:`~eifi1_server_kit.user_admin.require_confirmation` checks them against the level
the server decided. The page sends what its dialog asked for; a hand-written request that
leaves them out is refused.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from eifi1_server_kit.auth.schemas import (
    DEFAULT_LOCALE,
    MAX_EMAIL_LENGTH,
    Email,
    ExistingPassword,
    LocaleTag,
    UserResponse,
)
from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.user_admin.actions import AdminAction, ConfirmationLevel

__all__ = [
    "MAIL_BACKEND_CONSOLE",
    "MAX_NOTE_LENGTH",
    "MAX_REVIEWER_ENTRIES",
    "MAX_ROLE_LENGTH",
    "ActionConfirmation",
    "ActiveChange",
    "AdminActionRow",
    "AdminUserRow",
    "DeletionRequest",
    "EmailChangeConfirm",
    "EmailChangeRequest",
    "InvitationCreate",
    "InvitationRow",
    "InvitationStatus",
    "MailKind",
    "MailRequest",
    "MailResult",
    "PersonRef",
    "ReviewerUpdate",
    "RoleChange",
    "RolesChange",
    "UserListResponse",
    "invitation_status",
]

#: The mail backend whose answers may carry a link (§4.1, §5): nothing leaves the
#: machine, so the admin is the only way the link reaches anyone.
MAIL_BACKEND_CONSOLE = "console"
#: The :class:`UserListResponse` ``summary`` key with the count of active admins — what
#: the roster's last-admin lock reads (§4.1).
SUMMARY_ACTIVE_ADMINS = "active_admins"
#: A role value as an app writes it (``ADMIN``, ``PROPERTY_MANAGER``).
MAX_ROLE_LENGTH = 64
#: An invitation's note (kastlan's ``String(255)``).
MAX_NOTE_LENGTH = 255
#: Locales and areas in one reviewer grant (keksdose ``AdminReviewerRequest``).
MAX_REVIEWER_ENTRIES = 8

_Role = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ROLE_LENGTH)]
#: What a person types to confirm: not checked as an address — a typo is a
#: ``confirmation_mismatch``, not a 422 — only bounded.
_Typed = Annotated[str, Field(max_length=MAX_EMAIL_LENGTH)]


# --- §3: the user list ---------------------------------------------------------------


def _list_or_empty(value: object) -> object:
    return [] if value is None else value


def _dict_or_empty(value: object) -> object:
    return {} if value is None else value


class AdminUserRow(UserResponse):
    """One row of ``GET /admin/users`` (§3.2): the auth contract's ``UserResponse`` core —
    with its derived ``display_name`` and ``name_incomplete`` — plus what the admin page
    shows. Reads an ORM row (``from_attributes``).

    ``extra`` holds the app's own columns: keksdose's plan and key custody,
    Kurvenschmiede's owned counts; kastlan's companies only in the PLATFORM list — a
    company's list never names another company (§3.2). Attributes and counts, never
    content (keksdose's roster rule).
    """

    #: ``None``: never signed in (or not since the column shipped) — "never", not "0
    #: days ago". kastlan's comes from its session rows, account-wide (§3.2).
    last_login_at: datetime | None = None
    #: An admin required a new password; the next sign-in answers
    #: ``password_change_required`` (§4.1).
    password_change_required_at: datetime | None = None
    #: The user asked to delete the account (§6.4); the account is deactivated.
    deletion_requested_at: datetime | None = None
    #: When the erasure runs; ``None`` in ``operator`` mode, where an operator erases.
    deletion_scheduled_at: datetime | None = None
    #: The reviewer scope (``[]`` = none; an admin reviews everything without one).
    translation_review_locales: Annotated[list[str], BeforeValidator(_list_or_empty)] = Field(default_factory=list)
    #: …limited to these areas; ``None`` = every area.
    translation_review_areas: list[str] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class UserListResponse[RowT = AdminUserRow](BaseModel):
    """``GET /admin/users`` (§3.1): ``{users, total, mail_backend, summary?, levels?}``.

    ``total`` counts the rows matching the filter across every page — what the pager
    counts against. ``summary`` (keksdose's) describes the whole install and does NOT
    follow the filter; its ``active_admins`` key (:data:`SUMMARY_ACTIVE_ADMINS`) is the
    count every roster needs for the last-admin lock, sent by kastlan and Kurvenschmiede
    alike. ``mail_backend`` says whether the mail actions really send
    (:data:`MAIL_BACKEND_CONSOLE` = they log). ``levels`` maps an action (an
    :class:`AdminAction` value, or the app's own) to the :class:`ConfirmationLevel` the
    server will demand (§4.2), so the page renders the right confirmation before the
    first request instead of learning it from a refusal (kastlan's and Kurvenschmiede's
    shape, 2026-10-07). ``UserListResponse[MyRow]`` types the rows.
    """

    users: list[RowT]
    total: int = Field(ge=0)
    mail_backend: str
    summary: dict[str, Any] | None = None
    levels: dict[str, ConfirmationLevel] | None = None


# --- §4: admin actions ---------------------------------------------------------------


class ActionConfirmation(BaseModel):
    """The confirmation a request carries (§4.2): ``acknowledged`` for the ``acknowledge``
    level, ``confirm_email`` — the target's address, typed — for ``type_email``. Also the
    whole body of ``POST /admin/users/{id}/password-change``."""

    model_config = ConfigDict(extra="forbid")

    acknowledged: bool = False
    confirm_email: _Typed | None = None


class ActiveChange(ActionConfirmation):
    """``POST /admin/users/{id}/active {active, confirm_email?}`` (§4.1). Deactivating
    types the address; reactivating needs nothing and also clears a pending deletion."""

    active: bool


class RoleChange(ActionConfirmation):
    """``POST /admin/users/{id}/role {role}`` (§4.1) — ``acknowledge``. Narrow ``role``
    to the app's enum in a subclass."""

    role: _Role


def _deduplicated(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class RolesChange(ActionConfirmation):
    """kastlan's ``PUT /admin/users/{id}/roles {roles}`` for the current company (§4.1):
    at least one, de-duplicated in order. Removing every role is "Remove from company",
    its own request."""

    roles: Annotated[list[_Role], AfterValidator(_deduplicated)] = Field(min_length=1, max_length=16)


class MailKind(enum.StrEnum):
    """Which account mail an admin (re)sends (§4.1)."""

    VERIFICATION = "verification"
    PASSWORD_RESET = "password_reset"

    @property
    def action(self) -> AdminAction:
        """The ``admin_actions`` row it is logged as."""
        return AdminAction.MAIL_VERIFICATION if self is MailKind.VERIFICATION else AdminAction.MAIL_RESET


class MailRequest(ActionConfirmation):
    """``POST /admin/users/{id}/mail {kind}`` (§4.1). ``none`` by the contract; keksdose
    raises the reset mail's level for an account a password key opens, so the
    confirmation fields ride along."""

    kind: MailKind


class MailResult(BaseModel):
    """The answer to a mail action: ``{sent, mail_backend, link?}`` (§4.1).

    ``sent=false`` is a 200, not an error: the mail is best effort and the admin can do
    nothing about a provider being down (keksdose). ``link`` only while mail goes to the
    console — the shown-once rule of invitations — and a ``link`` with any other backend
    is refused here, so a production answer can never hand an admin someone's reset
    link.
    """

    sent: bool
    mail_backend: str
    link: str | None = None

    @model_validator(mode="after")
    def _link_only_on_the_console(self) -> Self:
        if self.link is not None and self.mail_backend != MAIL_BACKEND_CONSOLE:
            raise ValueError("a mail answer carries its link only while mail goes to the console")
        return self


class ReviewerUpdate(BaseModel):
    """``PUT /admin/users/{id}/reviewer {locales, areas}`` (§4.1; keksdose
    ``AdminReviewerRequest``): the reviewer role and its scope in one write. ``[]``
    locales takes the role away; ``areas`` ``None`` or ``[]`` is every area. Check both
    against the app's vocabulary with
    :func:`~eifi1_server_kit.translation_review.normalize_locales` and
    :func:`~eifi1_server_kit.translation_review.normalize_areas`."""

    model_config = ConfigDict(extra="forbid")

    locales: list[str] = Field(max_length=MAX_REVIEWER_ENTRIES)
    areas: list[str] | None = Field(default=None, max_length=MAX_REVIEWER_ENTRIES)


class PersonRef(BaseModel):
    """Who someone is, for a row that names them: the inviter, an action's actor. Names
    are formatted on the page, in the reader's order. Every field may be empty — an
    erased account leaves nothing to show (§4.3)."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    email: str | None = None
    first_name: str = ""
    last_name: str = ""


class AdminActionRow(BaseModel):
    """One row of ``GET /admin/actions?limit=&offset=&target=``, newest first (§4.3): the
    table's columns, plus the actor joined for display.

    ``actor_id`` is ``None`` for the erasure job; ``target_user_id`` for an invitation and
    after an erasure, when ``target_email`` is scrubbed too. ``company_id`` is kastlan's.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    action: AdminAction
    actor_id: int | None = None
    target_user_id: int | None = None
    target_email: str | None = None
    detail: Annotated[dict[str, Any], BeforeValidator(_dict_or_empty)] = Field(default_factory=dict)
    company_id: int | None = None
    actor: PersonRef | None = None


# --- §5: invitations -------------------------------------------------------------------


class InvitationStatus(enum.StrEnum):
    """Where an invitation stands (§5)."""

    OPEN = "open"
    ACCEPTED = "accepted"
    #: Run out unredeemed; listed until it is resent or removed (auth §4.4).
    EXPIRED = "expired"
    #: Withdrawn. An app that deletes the row instead (kastlan) never shows it.
    REVOKED = "revoked"


def invitation_status(
    *,
    expires_at: datetime,
    now: datetime,
    accepted_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> InvitationStatus:
    """An invitation's status from its columns: accepted, then revoked, then expired (AT
    ``expires_at``, as :func:`~eifi1_server_kit.auth.is_expired`), else open. Naive
    datetimes are read as UTC."""
    if accepted_at is not None:
        return InvitationStatus.ACCEPTED
    if revoked_at is not None:
        return InvitationStatus.REVOKED
    if _aware(expires_at) <= _aware(now):
        return InvitationStatus.EXPIRED
    return InvitationStatus.OPEN


def _blank_is_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


_Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE_LENGTH)]


class InvitationCreate(BaseModel):
    """``POST /admin/invitations {email, role, scope?, note?, locale}`` (§5).

    The address is normalised like every entry; the kit's form suggests no ``+tag`` — it
    is someone else's address. ``scope`` is the app's: a Kurvenschmiede team, ``None``
    for registration; kastlan's company comes from the admin's own, never from the body.
    ``locale`` is the invitee's language, which the mail is written in.
    """

    model_config = ConfigDict(extra="forbid")

    email: Email
    role: _Role
    scope: int | str | None = None
    note: Annotated[_Note | None, BeforeValidator(_blank_is_none)] = None
    locale: LocaleTag = DEFAULT_LOCALE


class InvitationRow(BaseModel):
    """One row of ``GET /admin/invitations`` (§5): ``{id, email, role, scope, note,
    locale, invited_by, created_at, expires_at, status, link?}``.

    ``link`` appears ONCE — in the answer to the create and to a resend, while mail goes
    to the console — never in the list: the token is stored hashed and cannot be shown
    again. ``role`` is ``None`` for kastlan's sign-up invitations.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str | None = None
    scope: int | str | None = None
    note: str | None = None
    locale: str
    invited_by: PersonRef | None = None
    created_at: datetime
    expires_at: datetime
    status: InvitationStatus
    link: str | None = None


# --- §6: the account's own settings -----------------------------------------------------


class EmailChangeRequest(BaseModel):
    """``POST /auth/me/email {new_email, password}`` (§6.2). The password again, because
    the address is the identity. The new address is normalised (a ``+tag`` stays) and
    must be free — ``email_taken`` otherwise. Nothing changes until the link mailed to
    it (:data:`~eifi1_server_kit.auth.EMAIL_CHANGE_TTL`) is confirmed."""

    model_config = ConfigDict(extra="forbid")

    new_email: Email
    password: ExistingPassword


class EmailChangeConfirm(BaseModel):
    """``POST /auth/me/email/confirm {token}`` (§6.2): switches the address, sets
    ``email_verified_at``, keeps the sessions."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=256)


class DeletionRequest(BaseModel):
    """``POST /auth/me/deletion {password, confirm_email}`` (§6.4): the password, and the
    account's own address typed — check it with
    :func:`~eifi1_server_kit.user_admin.require_confirmation` at
    ``confirmation_level(AdminAction.DELETION_REQUEST)``."""

    model_config = ConfigDict(extra="forbid")

    password: ExistingPassword
    confirm_email: _Typed
