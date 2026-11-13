"""The ports the account service runs on: the rows, the sessions, the links, the invitations,
the passkeys, the admin log, the work after the commit, and the app's JWT signer and
password hasher.

Layer 2 (``docs/auth-harmonization.md`` §12 in ``Eifi1/ui-kit``, on the pattern of
:mod:`eifi1_server_kit.feedback.ports`). Each app implements these over its own session and
tables, or inherits the ready ones of the ``sql`` extra; the kit never sees the app's
models, and no SQLAlchemy or FastAPI is imported here.

The ports are async and run in the caller's transaction: **every write flushes and none
commits** (``docs/server-services-harmonization.md`` §2.7). Rows are read through protocols
with read-only members, so an app's mapped row satisfies them as it is and its own columns
(sealed ones included) stay in its adapter; every write goes through the store.

**What waits for the commit** goes through :class:`AfterCommit`: the account mails and the
purge of an erased person's files. The kit never commits, so it cannot know when the work
is safe to do; the app runs the queue from its session's commit event and drops it on a
rollback (Kurvenschmiede's ``send_after_commit``).

**Not in this cut**: the second factor's store (keksdose's adoption brings it) and the admin
scope of a company (kastlan rests, Marcel 2026-11-13).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from eifi1_server_kit.auth.tokens import OneTimeTokenKind

__all__ = [
    "ACCOUNT_FIELDS",
    "OPTIONAL_ACCOUNT_FIELDS",
    "AccountRow",
    "AdminActionLog",
    "AdminActionRowView",
    "AfterCommit",
    "InvitationRow",
    "InvitationStore",
    "NewAccount",
    "NewInvitation",
    "NewPasskey",
    "NewSession",
    "OneTimeTokenRow",
    "OneTimeTokenStore",
    "PasskeyRow",
    "PasskeyStore",
    "PasswordHasher",
    "SessionRow",
    "SessionStore",
    "TokenSigner",
    "UserStore",
]


# ── The account ─────────────────────────────────────────────────────────────


class AccountRow(Protocol):
    """An account as the kit reads it (§12.3): read-only members; every write goes through
    :meth:`UserStore.apply`.

    Two members are optional and read with a default, so an app without them leaves them
    out (:data:`OPTIONAL_ACCOUNT_FIELDS`): ``is_demo`` (False: Staunlabor has no demo) and
    ``sessions_invalid_before`` (None: the ``iat`` cut-off keksdose and Kurvenschmiede keep
    until every token carries a ``sid``; a new app ends sessions through the rows only).
    """

    @property
    def id(self) -> int: ...
    @property
    def email(self) -> str: ...
    @property
    def password_hash(self) -> str: ...
    @property
    def first_name(self) -> str: ...
    @property
    def last_name(self) -> str: ...
    @property
    def locale(self) -> str: ...
    #: The app's role name (``ADMIN``, ``MEMBER``, ``REVIEWER``), one per account.
    @property
    def role(self) -> str: ...
    @property
    def is_active(self) -> bool: ...
    @property
    def email_verified_at(self) -> datetime | None: ...
    @property
    def last_login_at(self) -> datetime | None: ...
    #: An admin's forced password change (user-admin §4).
    @property
    def password_change_required_at(self) -> datetime | None: ...
    @property
    def deletion_requested_at(self) -> datetime | None: ...
    @property
    def deletion_scheduled_at(self) -> datetime | None: ...
    @property
    def text_size(self) -> str | None: ...
    @property
    def contrast(self) -> str | None: ...
    @property
    def device_timezone(self) -> str | None: ...
    #: The translation review's scope (``review_grant``): None for an account that is no reviewer.
    @property
    def translation_review_locales(self) -> Sequence[str] | None: ...
    @property
    def translation_review_areas(self) -> Sequence[str] | None: ...
    @property
    def created_at(self) -> datetime: ...


#: The members :meth:`UserStore.apply` may change, as named on :class:`AccountRow`.
ACCOUNT_FIELDS: frozenset[str] = frozenset(
    {
        "email",
        "password_hash",
        "first_name",
        "last_name",
        "locale",
        "role",
        "is_active",
        "email_verified_at",
        "last_login_at",
        "password_change_required_at",
        "deletion_requested_at",
        "deletion_scheduled_at",
        "text_size",
        "contrast",
        "device_timezone",
        "translation_review_locales",
        "translation_review_areas",
        "sessions_invalid_before",
    }
)

#: The members an app may leave out, with what the kit reads instead.
OPTIONAL_ACCOUNT_FIELDS: Mapping[str, object] = {"is_demo": False, "sessions_invalid_before": None}


@dataclass(frozen=True, slots=True)
class NewAccount:
    """An account to insert: the kit's columns; the app adds its own in ``on_register``."""

    email: str
    password_hash: str
    first_name: str
    last_name: str
    locale: str
    role: str
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    device_timezone: str | None = None


class UserStore[UserT: AccountRow](Protocol):
    """The app's accounts, on the request's session. Async; every write flushes and never
    commits."""

    async def get(self, user_id: int) -> UserT | None:
        """The account, or None."""

    async def by_email(self, email: str) -> UserT | None:
        """The account whose address is exactly ``email`` (already normalised by the kit)."""

    async def by_emails(self, emails: Sequence[str]) -> Sequence[UserT]:
        """The accounts whose address is one of ``emails`` (the address tag's variants,
        ``addresses_for_reset``), in no particular order."""

    async def count_people(self) -> int:
        """Accounts that are people: demo accounts and an app's system account left out
        (``registration_decision``'s "is this the first account")."""

    async def count_active(self, *, role: str) -> int:
        """Active accounts that are people and hold ``role``: the last-admin rule's count
        (``refuse_last_admin``; the deletion request, and the admin's actions of 0.30).
        Added in 0.29."""

    async def add(self, new: NewAccount) -> UserT:
        """Insert and flush, so the id is set."""

    async def apply(self, row: UserT, changes: Mapping[str, Any]) -> UserT:
        """Assign and flush. The keys are :data:`ACCOUNT_FIELDS`; an app without the
        ``iat`` cut-off ignores ``sessions_invalid_before``."""

    async def delete(self, row: UserT) -> None:
        """Delete the account and flush; the app's foreign keys cascade or set null."""


# ── Sessions (server-kit 0.27's rows) ───────────────────────────────────────


class SessionRow(Protocol):
    """One signed-in device (auth §6.3): its ``sid`` is in both tokens and is checked on
    every request."""

    @property
    def sid(self) -> str: ...
    @property
    def user_id(self) -> int: ...
    @property
    def device(self) -> str | None: ...
    @property
    def ip_address(self) -> str | None: ...
    @property
    def started_at(self) -> datetime: ...
    @property
    def last_active(self) -> datetime: ...
    #: Signed in with "This is a shared computer" (§2.14).
    @property
    def shared_device(self) -> bool: ...
    #: When the session ends on its own (``session_expires_at``).
    @property
    def expires_at(self) -> datetime: ...
    @property
    def is_active(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class NewSession:
    """A session to start; ``sid`` is the kit's ``mint()``."""

    sid: str
    user_id: int
    started_at: datetime
    expires_at: datetime
    shared_device: bool = False
    device: str | None = None
    ip_address: str | None = None


class SessionStore[SessionT: SessionRow](Protocol):
    """The app's ``user_sessions`` rows. Ending a session sets ``is_active`` false; a row
    is never needed again once it ended, so an app may prune it."""

    async def start(self, new: NewSession) -> SessionT:
        """Insert and flush."""

    async def get(self, sid: str) -> SessionT | None:
        """The session, active or not, or None."""

    async def touch(self, session: SessionT, *, last_active: datetime, expires_at: datetime) -> None:
        """A refresh: the new last activity and end, flushed."""

    async def end(self, session: SessionT) -> None:
        """End this one; ending an ended one is no error."""

    async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
        """End every active session of the account but ``keep`` (the caller's, after a
        password change); answer how many ended."""

    async def live(self, user_id: int) -> Sequence[SessionT]:
        """The account's active sessions, the latest activity first."""


# ── One-time links: reset, verification, address change ─────────────────────


class OneTimeTokenRow(Protocol):
    """A link's row: only the hash of its token is stored (``hash_token``)."""

    @property
    def kind(self) -> OneTimeTokenKind | str: ...
    @property
    def user_id(self) -> int: ...
    @property
    def token_hash(self) -> str: ...
    #: The address the link was sent to (reset, verification: it dies when the address
    #: changes), or the new address (an address change: the pending change itself).
    @property
    def email(self) -> str: ...
    @property
    def expires_at(self) -> datetime: ...
    @property
    def created_at(self) -> datetime: ...


class OneTimeTokenStore[TokenT: OneTimeTokenRow](Protocol):
    """The reset, verification and address-change links: at most one per account and kind
    (kastlan keeps them as columns on ``users``; that is an adapter's business)."""

    async def put(
        self, kind: OneTimeTokenKind, *, user_id: int, token_hash: str, email: str, expires_at: datetime
    ) -> TokenT:
        """Replace the account's earlier link of this kind, and flush."""

    async def take(self, kind: OneTimeTokenKind, token_hash: str) -> TokenT | None:
        """The link with this hash, expired or not, or None. Reading does not spend it."""

    async def pending(self, kind: OneTimeTokenKind, user_id: int) -> TokenT | None:
        """The account's link of this kind, expired or not (the pending address change)."""

    async def drop(self, kind: OneTimeTokenKind, user_id: int) -> None:
        """Delete the account's link of this kind, if any."""


# ── Invitations ─────────────────────────────────────────────────────────────


class InvitationRow(Protocol):
    """An invitation (auth §4.4). ``scope`` is the app's, opaque to the kit: a team, a
    budget, nothing; ``on_invitation_redeemed`` reads it."""

    @property
    def id(self) -> int: ...
    @property
    def email(self) -> str: ...
    #: The role an account made through it gets; None: the app's default.
    @property
    def role(self) -> str | None: ...
    @property
    def scope(self) -> Mapping[str, Any] | None: ...
    #: None for a row that was never sent (an allow-list entry carried over).
    @property
    def token_hash(self) -> str | None: ...
    @property
    def expires_at(self) -> datetime | None: ...
    @property
    def accepted_at(self) -> datetime | None: ...
    @property
    def accepted_by_user_id(self) -> int | None: ...
    @property
    def revoked_at(self) -> datetime | None: ...
    @property
    def invited_by_user_id(self) -> int | None: ...
    @property
    def locale(self) -> str: ...
    @property
    def note(self) -> str | None: ...
    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class NewInvitation:
    """An invitation to insert."""

    email: str
    token_hash: str
    expires_at: datetime
    locale: str
    invited_by_user_id: int | None
    role: str | None = None
    scope: Mapping[str, Any] | None = None
    note: str | None = None


class InvitationStore[InvT: InvitationRow](Protocol):
    """The app's invitations."""

    async def get(self, invitation_id: int) -> InvT | None:
        """The invitation, or None."""

    async def by_token_hash(self, token_hash: str) -> InvT | None:
        """The invitation whose link this is, in any state, or None."""

    async def open_for(self, email: str) -> Sequence[InvT]:
        """The address's invitations that are neither accepted nor revoked, expired ones
        included: the kit decides on the expiry."""

    async def listing(self) -> Sequence[InvT]:
        """Every invitation that was not accepted, newest first: the admin's list."""

    async def add(self, new: NewInvitation) -> InvT:
        """Insert and flush."""

    async def remint(self, invitation: InvT, *, token_hash: str, expires_at: datetime) -> InvT:
        """A new link for the same invitation (a re-send), flushed."""

    async def accept(self, invitation: InvT, *, user_id: int, at: datetime) -> None:
        """Mark it accepted by the account, flushed."""

    async def revoke(self, invitation: InvT, *, at: datetime) -> None:
        """Withdraw it, flushed (an app may delete the row instead)."""


# ── Passkeys ────────────────────────────────────────────────────────────────


class PasskeyRow(Protocol):
    """A registered passkey (WebAuthn credential)."""

    @property
    def id(self) -> int: ...
    @property
    def user_id(self) -> int: ...
    @property
    def credential_id(self) -> bytes: ...
    @property
    def public_key(self) -> bytes: ...
    @property
    def sign_count(self) -> int: ...
    #: Comma-separated, as the browser reported them; None when it did not.
    @property
    def transports(self) -> str | None: ...
    @property
    def name(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def last_used_at(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class NewPasskey:
    """A passkey to insert."""

    user_id: int
    credential_id: bytes
    public_key: bytes
    sign_count: int
    name: str
    transports: str | None = None


class PasskeyStore[PasskeyT: PasskeyRow](Protocol):
    """The app's passkeys and the challenges already answered."""

    async def for_user(self, user_id: int) -> Sequence[PasskeyT]:
        """The account's passkeys, oldest first."""

    async def by_credential_id(self, credential_id: bytes) -> PasskeyT | None:
        """The passkey with this credential id, or None."""

    async def add(self, new: NewPasskey) -> PasskeyT:
        """Insert and flush. A credential id that exists already is the app's integrity
        error; the kit checks first."""

    async def used(self, passkey: PasskeyT, *, sign_count: int, at: datetime) -> None:
        """A sign-in with it: the new counter and time, flushed."""

    async def rename(self, passkey: PasskeyT, name: str) -> None:
        """Flushed."""

    async def remove(self, passkey: PasskeyT) -> None:
        """Delete and flush."""

    async def spend_challenge(self, challenge_hash: str, *, expires_at: datetime) -> bool:
        """Record that this challenge was answered: True the first time, False when it was
        spent before (one use per challenge, Kurvenschmiede's table, §12.5 item 8). The row
        may go once ``expires_at`` has passed."""


# ── The admin log ───────────────────────────────────────────────────────────


class AdminActionRowView(Protocol):
    """An ``admin_actions`` row as the kit reads it (user-admin §4.3)."""

    @property
    def id(self) -> int: ...
    @property
    def at(self) -> datetime: ...
    @property
    def actor_id(self) -> int | None: ...
    @property
    def action(self) -> str: ...
    @property
    def target_user_id(self) -> int | None: ...
    @property
    def target_email(self) -> str | None: ...
    @property
    def detail(self) -> Mapping[str, Any]: ...


class AdminActionLog[ActionT: AdminActionRowView](Protocol):
    """The admin log. The kit builds every record with ``admin_action_record``."""

    async def add(self, record: Mapping[str, Any]) -> ActionT:
        """Insert ``admin_action_record``'s mapping and flush."""

    async def page(self, *, target_user_id: int | None, limit: int, offset: int) -> tuple[Sequence[ActionT], int]:
        """The newest first, and how many there are in all."""

    async def scrub_email(self, email: str) -> None:
        """An erasure: clear ``target_email`` wherever it is this address, flushed."""


# ── After the commit, and the app's JWTs ─────────────────────────────────────


class AfterCommit(Protocol):
    """Work that may only happen once the app committed: a mail with a link the rollback
    would have voided, the purge of files whose rows were deleted."""

    def later(self, work: Callable[[], Awaitable[None]]) -> None:
        """Queue it. The app runs the queue after its commit, each item on its own (one that
        fails is logged, the rest run), and drops it on a rollback."""

    async def commit_now(self) -> None:
        """Commit the request's transaction now. The one place the kit commits: a route whose
        answer says whether its mail went out (``sent``: an address change, its re-send, an
        invitation and its re-send, an admin's mail) commits first and then sends, so a
        rollback never mails a dead link and the answer is still true (keksdose's plan and
        Kurvenschmiede's point, 2026-11-13)."""


class TokenSigner(Protocol):
    """The app's JWT library behind the kit's claims (the kit keeps no JWT dependency, §8)."""

    def sign(self, claims: Mapping[str, Any]) -> str:
        """The signed token for these claims (``exp`` included)."""

    def verify(self, token: str) -> Mapping[str, Any] | None:
        """The claims of a token this app signed and that has not expired, or None."""


class PasswordHasher(Protocol):
    """The app's password hashing (the kit depends on no hasher, ``auth.passwords``): bcrypt
    in all four apps, its cost the app's setting. The kit refuses a password over
    ``MAX_PASSWORD_BYTES`` before it asks."""

    def hash(self, password: str) -> str:
        """A new hash of the password."""

    def verify(self, password: str, password_hash: str) -> bool:
        """Whether the password matches the hash; never raises for a malformed hash."""
