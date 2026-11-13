"""Test helpers for the account layer: in-memory stores, two fakes, and a conformance check
per port.

``docs/auth-harmonization.md`` §12.3 ("Testing support") in ``Eifi1/ui-kit``, on the pattern
of :mod:`eifi1_server_kit.feedback.testing`. In the wheel and NOT re-exported from
:mod:`eifi1_server_kit.auth`: import it as ``eifi1_server_kit.auth.testing``. No SQLAlchemy,
no FastAPI and no JWT library.

* the stores: :class:`InMemoryUserStore`, :class:`InMemorySessionStore`,
  :class:`InMemoryOneTimeTokenStore`, :class:`InMemoryInvitationStore`,
  :class:`InMemoryPasskeyStore` and :class:`InMemoryAdminActionLog`, each over a slotted row
  (:class:`MemoryAccountRow` and its siblings) that satisfies its port's row protocol, with
  ids counted up and ``created_at`` from an injectable clock; and :class:`InMemoryAfterCommit`,
  whose :meth:`~InMemoryAfterCommit.run` and :meth:`~InMemoryAfterCommit.drop` stand for the
  app's commit and rollback. The kit's own service tests run on these;
* the fakes: :class:`FakePasswordHasher`, a deterministic hasher that costs nothing, and
  :class:`HmacTokenSigner`, a signer over HMAC-SHA256 with an injectable clock;
* the checks: :func:`check_user_store`, :func:`check_session_store`,
  :func:`check_one_time_token_store`, :func:`check_invitation_store`,
  :func:`check_passkey_store`, :func:`check_admin_action_log` and :func:`check_after_commit`.
  An app runs them once against its own adapters (or the ``sql`` extra's), on a fresh
  session over its test database as it is. Each calls every method of its port, raises
  :class:`AssertionError` naming the first promise that fails, writes under fresh addresses,
  digests and credential ids, and asserts only on what it adds. Each docstring says which
  accounts the check needs.

**What it deliberately does not do.** It never commits, opens no session and imports no
SQLAlchemy: a check runs on the session the app's store was given, so that store's flushes,
foreign keys and unique indexes are what get checked. The fakes are for tests only: the
hash is a bare SHA-256 and the tokens are no JWTs. The software authenticator for passkey
tests is not here (``auth.soft_webauthn``). Not checked, because no port promises it: what
``count_people`` leaves out besides demo accounts (an app's system account), whether
``live`` also leaves out an expired session, and that failed ``AfterCommit`` work is logged.

Synthetic data only: every address, digest and credential id carries a fresh token, so two
runs on the same database never share a row.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import itertools
import json
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, TypeGuard

from eifi1_server_kit.auth.ports import (
    ACCOUNT_FIELDS,
    AccountRow,
    AdminActionLog,
    AfterCommit,
    InvitationStore,
    NewAccount,
    NewInvitation,
    NewPasskey,
    NewSession,
    OneTimeTokenStore,
    PasskeyStore,
    SessionStore,
    UserStore,
)
from eifi1_server_kit.auth.tokens import INVITE_TTL, OneTimeTokenKind, hash_token
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.user_admin.actions import AdminAction
from eifi1_server_kit.user_admin.audit import admin_action_record

if TYPE_CHECKING:
    from eifi1_server_kit.auth.soft_webauthn import SoftAuthenticator, SoftCredential

__all__ = [
    "FakePasswordHasher",
    "HmacTokenSigner",
    "InMemoryAdminActionLog",
    "InMemoryAfterCommit",
    "InMemoryInvitationStore",
    "InMemoryOneTimeTokenStore",
    "InMemoryPasskeyStore",
    "InMemorySessionStore",
    "InMemoryUserStore",
    "MemoryAccountRow",
    "MemoryAdminActionRow",
    "MemoryCutoffAccountRow",
    "MemoryInvitationRow",
    "MemoryOneTimeTokenRow",
    "MemoryPasskeyRow",
    "MemorySessionRow",
    "SoftAuthenticator",
    "SoftCredential",
    "check_admin_action_log",
    "check_after_commit",
    "check_invitation_store",
    "check_one_time_token_store",
    "check_passkey_store",
    "check_session_store",
    "check_user_store",
]

_log = logging.getLogger(__name__)

#: An id no test database reaches: the largest a Postgres ``integer`` holds.
_MISSING_ID = 2**31 - 1

#: The kinds a :class:`~eifi1_server_kit.auth.ports.OneTimeTokenStore` holds: an
#: invitation's link is the invitation store's (§12.3).
_LINK_KINDS = (OneTimeTokenKind.PASSWORD_RESET, OneTimeTokenKind.VERIFICATION, OneTimeTokenKind.EMAIL_CHANGE)

#: The keys :func:`~eifi1_server_kit.user_admin.admin_action_record` may return.
_ADMIN_RECORD_KEYS = frozenset(
    {
        "at",
        "actor_id",
        "action",
        "target_user_id",
        "target_email",
        "detail",
        "company_id",
        "target_group_id",
        "target_group_name",
    }
)


# ── The rows ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MemoryAccountRow:
    """One account in memory: every :class:`~eifi1_server_kit.auth.ports.AccountRow` member,
    mutable, and ``is_demo``; no ``sessions_invalid_before``, as a new app's account has
    none (:class:`MemoryCutoffAccountRow` has it). Slotted, so a write naming a column that
    does not exist fails loudly."""

    id: int
    email: str
    password_hash: str
    first_name: str
    last_name: str
    locale: str
    role: str
    created_at: datetime
    is_active: bool = True
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    password_change_required_at: datetime | None = None
    deletion_requested_at: datetime | None = None
    deletion_scheduled_at: datetime | None = None
    text_size: str | None = None
    contrast: str | None = None
    device_timezone: str | None = None
    translation_review_locales: list[str] | None = None
    translation_review_areas: list[str] | None = None
    #: Set by :meth:`InMemoryUserStore.add_demo` only: no port write sets it.
    is_demo: bool = False


@dataclass(slots=True)
class MemoryCutoffAccountRow(MemoryAccountRow):
    """An account of an app that still ends sessions by the ``iat`` cut-off (keksdose's and
    Kurvenschmiede's ``sessions_invalid_before``): :class:`InMemoryUserStore` with
    ``cutoff=True``."""

    sessions_invalid_before: datetime | None = None


@dataclass(slots=True)
class MemorySessionRow:
    """One session in memory; satisfies :class:`~eifi1_server_kit.auth.ports.SessionRow`."""

    sid: str
    user_id: int
    started_at: datetime
    last_active: datetime
    expires_at: datetime
    shared_device: bool = False
    device: str | None = None
    ip_address: str | None = None
    is_active: bool = True


@dataclass(slots=True)
class MemoryOneTimeTokenRow:
    """One link in memory; satisfies :class:`~eifi1_server_kit.auth.ports.OneTimeTokenRow`."""

    kind: OneTimeTokenKind
    user_id: int
    token_hash: str
    email: str
    expires_at: datetime
    created_at: datetime


@dataclass(slots=True)
class MemoryInvitationRow:
    """One invitation in memory; satisfies :class:`~eifi1_server_kit.auth.ports.InvitationRow`."""

    id: int
    email: str
    token_hash: str | None
    expires_at: datetime | None
    locale: str
    invited_by_user_id: int | None
    created_at: datetime
    role: str | None = None
    scope: dict[str, Any] | None = None
    note: str | None = None
    accepted_at: datetime | None = None
    accepted_by_user_id: int | None = None
    revoked_at: datetime | None = None


@dataclass(slots=True)
class MemoryPasskeyRow:
    """One passkey in memory; satisfies :class:`~eifi1_server_kit.auth.ports.PasskeyRow`."""

    id: int
    user_id: int
    credential_id: bytes
    public_key: bytes
    sign_count: int
    name: str
    created_at: datetime
    transports: str | None = None
    last_used_at: datetime | None = None


@dataclass(slots=True)
class MemoryAdminActionRow:
    """One ``admin_actions`` row in memory: the fields
    :func:`~eifi1_server_kit.user_admin.admin_action_record` returns. Satisfies
    :class:`~eifi1_server_kit.auth.ports.AdminActionRowView`."""

    id: int
    at: datetime
    actor_id: int | None
    action: str
    target_user_id: int | None
    target_email: str | None
    detail: dict[str, Any]
    company_id: int | None = None
    target_group_id: int | None = None
    target_group_name: str | None = None


def _newest_first[RowT](rows: list[RowT], key: Callable[[RowT], tuple[datetime, int]]) -> list[RowT]:
    return sorted(rows, key=key, reverse=True)


# ── The stores ──────────────────────────────────────────────────────────────


class InMemoryUserStore:
    """A :class:`~eifi1_server_kit.auth.ports.UserStore` of :class:`MemoryAccountRow`.

    ``now`` stamps ``created_at``. ``cutoff=True`` makes :class:`MemoryCutoffAccountRow`\\ s,
    an app with the ``iat`` cut-off; without it :meth:`apply` ignores
    ``sessions_invalid_before``, as the port says. :meth:`apply` refuses any key outside
    :data:`~eifi1_server_kit.auth.ports.ACCOUNT_FIELDS` with :class:`ValueError`: a service
    that wrote ``id`` or ``is_demo`` through it would pass on a mapped row, which takes any
    attribute. :meth:`add_demo` makes a demo account, as an app's demo session does outside
    the port. ``count_people`` leaves the demo accounts out.
    """

    def __init__(self, *, now: Clock = utc_now, cutoff: bool = False) -> None:
        #: Every account, by id.
        self.rows: dict[int, MemoryAccountRow] = {}
        self.now = now
        self.cutoff = cutoff
        # A sequence, never ``max + 1``: an erased account's id must not come back, or a
        # session or a log row that named the old account would name the new one.
        self._ids = itertools.count(1)

    async def get(self, user_id: int) -> MemoryAccountRow | None:
        return self.rows.get(user_id)

    async def by_email(self, email: str) -> MemoryAccountRow | None:
        return next((row for row in self.rows.values() if row.email == email), None)

    async def by_emails(self, emails: Sequence[str]) -> Sequence[MemoryAccountRow]:
        wanted = set(emails)
        return [row for row in self.rows.values() if row.email in wanted]

    async def count_people(self) -> int:
        return sum(not row.is_demo for row in self.rows.values())

    async def count_active(self, *, role: str) -> int:
        return sum(row.is_active and row.role == role and not row.is_demo for row in self.rows.values())

    async def add(self, new: NewAccount) -> MemoryAccountRow:
        kind = MemoryCutoffAccountRow if self.cutoff else MemoryAccountRow
        row = kind(id=next(self._ids), created_at=self.now(), **_fields(new))
        self.rows[row.id] = row
        return row

    async def add_demo(self, new: NewAccount) -> MemoryAccountRow:
        """Insert a demo account (``is_demo``); not a port method."""
        row = await self.add(new)
        row.is_demo = True
        return row

    async def apply(self, row: MemoryAccountRow, changes: Mapping[str, Any]) -> MemoryAccountRow:
        if unknown := sorted(changes.keys() - ACCOUNT_FIELDS):
            raise ValueError(f"not an account field the kit may change: {', '.join(unknown)}")
        for name, value in changes.items():
            if name == "sessions_invalid_before" and not self.cutoff:
                continue
            # A copy of a list, so the caller's later change to it is not the row's.
            setattr(row, name, list(value) if isinstance(value, list | tuple) else value)
        return row

    async def delete(self, row: MemoryAccountRow) -> None:
        self.rows.pop(row.id, None)


class InMemorySessionStore:
    """A :class:`~eifi1_server_kit.auth.ports.SessionStore` of :class:`MemorySessionRow`. A new
    session's ``last_active`` is its ``started_at``. A second :meth:`start` with the same
    ``sid`` is a :class:`ValueError`, the primary key's refusal, so a test that reused one
    does not pass by overwriting the first."""

    def __init__(self) -> None:
        #: Every session, by sid.
        self.rows: dict[str, MemorySessionRow] = {}

    async def start(self, new: NewSession) -> MemorySessionRow:
        if new.sid in self.rows:
            raise ValueError(f"a session with the sid {new.sid!r} exists")
        row = MemorySessionRow(last_active=new.started_at, **_fields(new))
        self.rows[row.sid] = row
        return row

    async def get(self, sid: str) -> MemorySessionRow | None:
        return self.rows.get(sid)

    async def touch(self, session: MemorySessionRow, *, last_active: datetime, expires_at: datetime) -> None:
        session.last_active = last_active
        session.expires_at = expires_at

    async def end(self, session: MemorySessionRow) -> None:
        session.is_active = False

    async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
        ending = [row for row in await self.live(user_id) if row.sid != keep]
        for row in ending:
            row.is_active = False
        return len(ending)

    async def live(self, user_id: int) -> Sequence[MemorySessionRow]:
        rows = [row for row in self.rows.values() if row.user_id == user_id and row.is_active]
        return sorted(rows, key=lambda row: (row.last_active, row.sid), reverse=True)


class InMemoryOneTimeTokenStore:
    """A :class:`~eifi1_server_kit.auth.ports.OneTimeTokenStore` of
    :class:`MemoryOneTimeTokenRow`, one per account and kind. ``now`` stamps ``created_at``.
    :attr:`~eifi1_server_kit.auth.tokens.OneTimeTokenKind.INVITATION` is a
    :class:`ValueError`: an invitation's link lives in the invitation store."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        #: Every link, by kind and account.
        self.rows: dict[tuple[OneTimeTokenKind, int], MemoryOneTimeTokenRow] = {}
        self.now = now

    async def put(
        self, kind: OneTimeTokenKind, *, user_id: int, token_hash: str, email: str, expires_at: datetime
    ) -> MemoryOneTimeTokenRow:
        if kind not in _LINK_KINDS:
            raise ValueError(f"{kind!r} is no link of this store: an invitation's is the invitation store's")
        row = MemoryOneTimeTokenRow(
            kind=OneTimeTokenKind(kind),
            user_id=user_id,
            token_hash=token_hash,
            email=email,
            expires_at=expires_at,
            created_at=self.now(),
        )
        self.rows[(row.kind, user_id)] = row
        return row

    async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryOneTimeTokenRow | None:
        return next((row for row in self.rows.values() if row.kind == kind and row.token_hash == token_hash), None)

    async def pending(self, kind: OneTimeTokenKind, user_id: int) -> MemoryOneTimeTokenRow | None:
        return self.rows.get((OneTimeTokenKind(kind), user_id))

    async def drop(self, kind: OneTimeTokenKind, user_id: int) -> None:
        self.rows.pop((OneTimeTokenKind(kind), user_id), None)


class InMemoryInvitationStore:
    """A :class:`~eifi1_server_kit.auth.ports.InvitationStore` of :class:`MemoryInvitationRow`.
    ``now`` stamps ``created_at``. ``revoke_deletes=True`` deletes a revoked invitation, the
    port's other choice, so a service test runs on both."""

    def __init__(self, *, now: Clock = utc_now, revoke_deletes: bool = False) -> None:
        #: Every invitation, by id.
        self.rows: dict[int, MemoryInvitationRow] = {}
        self.now = now
        self.revoke_deletes = revoke_deletes
        self._ids = itertools.count(1)

    async def get(self, invitation_id: int) -> MemoryInvitationRow | None:
        return self.rows.get(invitation_id)

    async def by_token_hash(self, token_hash: str) -> MemoryInvitationRow | None:
        return next((row for row in self.rows.values() if row.token_hash == token_hash), None)

    async def open_for(self, email: str) -> Sequence[MemoryInvitationRow]:
        return [
            row
            for row in self.rows.values()
            if row.email == email and row.accepted_at is None and row.revoked_at is None
        ]

    async def listing(self) -> Sequence[MemoryInvitationRow]:
        rows = [row for row in self.rows.values() if row.accepted_at is None]
        return _newest_first(rows, lambda row: (row.created_at, row.id))

    async def add(self, new: NewInvitation) -> MemoryInvitationRow:
        fields = _fields(new)
        fields["scope"] = None if new.scope is None else dict(new.scope)
        row = MemoryInvitationRow(id=next(self._ids), created_at=self.now(), **fields)
        self.rows[row.id] = row
        return row

    async def remint(
        self, invitation: MemoryInvitationRow, *, token_hash: str, expires_at: datetime
    ) -> MemoryInvitationRow:
        invitation.token_hash = token_hash
        invitation.expires_at = expires_at
        return invitation

    async def accept(self, invitation: MemoryInvitationRow, *, user_id: int, at: datetime) -> None:
        invitation.accepted_at = at
        invitation.accepted_by_user_id = user_id

    async def revoke(self, invitation: MemoryInvitationRow, *, at: datetime) -> None:
        if self.revoke_deletes:
            self.rows.pop(invitation.id, None)
        else:
            invitation.revoked_at = at


class InMemoryPasskeyStore:
    """A :class:`~eifi1_server_kit.auth.ports.PasskeyStore` of :class:`MemoryPasskeyRow`, with
    the spent challenges in :attr:`spent`. ``now`` stamps ``created_at``. A credential id that
    exists already is a :class:`ValueError`, the integrity error the port names, so a
    service that forgot to check first fails here as it would on the database. A spent
    challenge is kept for good: the port lets it go after its expiry, and never requires it."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        #: Every passkey, by id.
        self.rows: dict[int, MemoryPasskeyRow] = {}
        #: Every spent challenge's digest, with its ``expires_at``.
        self.spent: dict[str, datetime] = {}
        self.now = now
        self._ids = itertools.count(1)

    async def for_user(self, user_id: int) -> Sequence[MemoryPasskeyRow]:
        rows = [row for row in self.rows.values() if row.user_id == user_id]
        return sorted(rows, key=lambda row: (row.created_at, row.id))

    async def by_credential_id(self, credential_id: bytes) -> MemoryPasskeyRow | None:
        return next((row for row in self.rows.values() if row.credential_id == credential_id), None)

    async def add(self, new: NewPasskey) -> MemoryPasskeyRow:
        if await self.by_credential_id(new.credential_id) is not None:
            raise ValueError("a passkey with this credential id exists (the app's integrity error)")
        row = MemoryPasskeyRow(id=next(self._ids), created_at=self.now(), **_fields(new))
        self.rows[row.id] = row
        return row

    async def used(self, passkey: MemoryPasskeyRow, *, sign_count: int, at: datetime) -> None:
        passkey.sign_count = sign_count
        passkey.last_used_at = at

    async def rename(self, passkey: MemoryPasskeyRow, name: str) -> None:
        passkey.name = name

    async def remove(self, passkey: MemoryPasskeyRow) -> None:
        self.rows.pop(passkey.id, None)

    async def spend_challenge(self, challenge_hash: str, *, expires_at: datetime) -> bool:
        if challenge_hash in self.spent:
            return False
        self.spent[challenge_hash] = expires_at
        return True


class InMemoryAdminActionLog:
    """A :class:`~eifi1_server_kit.auth.ports.AdminActionLog` of :class:`MemoryAdminActionRow`.
    :meth:`add` takes :func:`~eifi1_server_kit.user_admin.admin_action_record`'s mapping and
    refuses any other key with :class:`ValueError` (a field no table has); a record without
    ``at`` is stamped by ``now``, as the column's default would."""

    def __init__(self, *, now: Clock = utc_now) -> None:
        #: Every row, by id.
        self.rows: dict[int, MemoryAdminActionRow] = {}
        self.now = now
        self._ids = itertools.count(1)

    async def add(self, record: Mapping[str, Any]) -> MemoryAdminActionRow:
        if unknown := sorted(record.keys() - _ADMIN_RECORD_KEYS):
            raise ValueError(f"not a field of admin_action_record: {', '.join(unknown)}")
        fields = dict(record)
        fields["at"] = fields.get("at") or self.now()
        fields["detail"] = dict(fields.get("detail") or {})
        fields.setdefault("actor_id", None)
        fields.setdefault("target_user_id", None)
        fields.setdefault("target_email", None)
        row = MemoryAdminActionRow(id=next(self._ids), **fields)
        self.rows[row.id] = row
        return row

    async def page(
        self, *, target_user_id: int | None, limit: int, offset: int
    ) -> tuple[Sequence[MemoryAdminActionRow], int]:
        rows = [row for row in self.rows.values() if target_user_id is None or row.target_user_id == target_user_id]
        ordered = _newest_first(rows, lambda row: (row.at, row.id))
        return ordered[offset : offset + limit], len(ordered)

    async def scrub_email(self, email: str) -> None:
        for row in self.rows.values():
            if row.target_email == email:
                row.target_email = None


class InMemoryAfterCommit:
    """An :class:`~eifi1_server_kit.auth.ports.AfterCommit` whose queue a test runs by hand.

    :meth:`run` is the app's commit: each queued item runs on its own, and one that raises
    is logged and kept in :attr:`failures` while the rest run. :meth:`drop` is the rollback.
    :meth:`commit_now` is the kit's own commit: it counts in :attr:`commits` and then runs
    the queue, as the app's commit event would. Work queued while the queue runs waits for
    the next commit."""

    def __init__(self) -> None:
        #: The work waiting for the commit, in the order it was queued.
        self.queue: list[Callable[[], Awaitable[None]]] = []
        #: How often the kit committed through :meth:`commit_now`.
        self.commits = 0
        #: What queued work raised, in order.
        self.failures: list[Exception] = []

    def later(self, work: Callable[[], Awaitable[None]]) -> None:
        self.queue.append(work)

    async def commit_now(self) -> None:
        self.commits += 1
        await self.run()

    async def run(self) -> None:
        """The commit: run the queue and empty it."""
        queued, self.queue = self.queue, []
        for work in queued:
            try:
                await work()
            except Exception as error:
                # The port's rule: one that fails is logged and the rest still run, so one
                # bad address never keeps the other mails of a commit back.
                _log.exception("work queued for after the commit failed")
                self.failures.append(error)

    async def drop(self) -> None:
        """The rollback: the queue is dropped, unrun."""
        self.queue.clear()


# ── The fakes ───────────────────────────────────────────────────────────────


class FakePasswordHasher:
    """A :class:`~eifi1_server_kit.auth.ports.PasswordHasher` that costs nothing: ``fake$`` and
    the password's SHA-256, the same for the same password (bcrypt salts; a test need not).
    :meth:`verify` never raises, whatever the hash, as the port says. :attr:`checks` records
    each hash :meth:`verify` was asked about, so a test sees that an address with no account
    was checked all the same (:func:`~eifi1_server_kit.auth.absent_account_check`)."""

    PREFIX: ClassVar[str] = "fake$"

    def __init__(self) -> None:
        #: Each hash :meth:`verify` compared against, in order.
        self.checks: list[str] = []

    def hash(self, password: str) -> str:
        # ``surrogatepass``: a password a browser could not have sent still hashes, so a
        # test about refusing it fails on the refusal, not in the fake.
        return self.PREFIX + hashlib.sha256(password.encode("utf-8", "surrogatepass")).hexdigest()

    def verify(self, password: str, password_hash: str) -> bool:
        self.checks.append(password_hash)
        return password_hash == self.hash(password)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    """``text`` as base64url without padding, or :class:`ValueError`."""
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    # The decoder skips characters outside its alphabet; without this, two different
    # strings would be the same token.
    if _b64(data) != text:
        raise ValueError("not canonical base64url")
    return data


def _finite_number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class HmacTokenSigner:
    """A :class:`~eifi1_server_kit.auth.ports.TokenSigner` without a JWT library: the claims as
    canonical JSON (sorted keys, no spaces) in base64url without padding, a dot, and that
    part's HMAC-SHA256 in base64url. Not a JWT, and for tests only.

    :meth:`sign` refuses claims without a finite ``exp`` (:class:`ValueError`): the kit's
    claims always carry one (``access_claims``), and a token that never ends is a fault an
    app's JWT library would not report. :meth:`verify` answers None for another secret's
    token, for anything not of this shape, and for an ``exp`` at or before ``now()``
    (PyJWT's rule, and :func:`~eifi1_server_kit.auth.is_expired`'s). ``secret`` defaults to
    a fresh random one, so two signers stand for two apps.
    """

    def __init__(self, secret: bytes | None = None, *, now: Clock = utc_now) -> None:
        self.secret = secret if secret is not None else secrets.token_bytes(32)
        self.now = now

    def sign(self, claims: Mapping[str, Any]) -> str:
        if not _finite_number(claims.get("exp")):
            raise ValueError("a token's claims carry a finite exp (TokenSigner.sign)")
        return self._seal(json.dumps(dict(claims), sort_keys=True, separators=(",", ":"), allow_nan=False).encode())

    def verify(self, token: str) -> Mapping[str, Any] | None:
        try:
            body, signature = token.split(".")
            payload = _unb64(body)
            if not hmac.compare_digest(_unb64(signature), self._mac(body)):
                return None
            claims = json.loads(payload)
        except ValueError:
            # A token of the wrong shape, not base64url, or not JSON: never this app's.
            return None
        if not isinstance(claims, dict):
            return None
        exp = claims.get("exp")
        if _finite_number(exp) and exp > self.now().timestamp():
            return claims
        return None

    def _mac(self, body: str) -> bytes:
        return hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()

    def _seal(self, payload: bytes) -> str:
        body = _b64(payload)
        return f"{body}.{_b64(self._mac(body))}"


# ── What the checks share ───────────────────────────────────────────────────

_ABSENT = object()


def _check(condition: bool, message: str) -> None:
    """``assert`` that survives ``python -O``: a check that stripped itself would pass every
    store."""
    if not condition:
        raise AssertionError(message)


def _found[T](row: T | None, message: str) -> T:
    """``row``, or the failure ``message`` rather than an ``AttributeError`` on None."""
    if row is None:
        raise AssertionError(message)
    return row


def _two_accounts(user_id: int, other_user_id: int) -> None:
    if user_id == other_user_id:
        raise ValueError("the check needs two different accounts: user_id and other_user_id are the same")


def _fields(new: Any) -> dict[str, Any]:
    """A ``New…`` dataclass's fields by name."""
    return {field.name: getattr(new, field.name) for field in dataclasses.fields(new)}


def _aware(moment: datetime) -> datetime:
    """A naive read-back is UTC: SQLite hands a ``DateTime(timezone=True)`` column back naive."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _same_instant(read: object, wanted: datetime) -> bool:
    return isinstance(read, datetime) and _aware(read) == _aware(wanted)


def _reads_back(read: object, wanted: object) -> bool:
    if isinstance(wanted, datetime):
        return _same_instant(read, wanted)
    if isinstance(wanted, list):
        # A JSON column may answer another sequence; the values are what count.
        return isinstance(read, Sequence) and list(read) == wanted
    return read == wanted


def _check_written(row: object, values: Mapping[str, object], step: str) -> None:
    for name, wanted in values.items():
        # The one member a row may lack: an app without the ``iat`` cut-off ignores it (§12.3).
        if name == "sessions_invalid_before" and not hasattr(row, name):
            continue
        _check(_reads_back(getattr(row, name, _ABSENT), wanted), f"{step}: {name} reads back as {wanted!r}")


def _in_order(keys: Sequence[tuple[datetime, int]], *, newest_first: bool) -> bool:
    """Are ``(instant, id)`` keys in order, the id deciding a tie? A transaction's ``now()``
    makes ties the common case on Postgres, and an order a tie leaves open makes a page skip
    or repeat a row."""
    aware = [(_aware(at), number) for at, number in keys]
    return aware == sorted(aware, reverse=newest_first)


def _latest_first(moments: Sequence[datetime]) -> bool:
    aware = [_aware(moment) for moment in moments]
    return aware == sorted(aware, reverse=True)


def _address(token: str, label: object) -> str:
    return f"check-{token}-{label}@example.test"


def _digest(token: str, label: object) -> str:
    return hash_token(f"check-{token}-{label}")


# ── The checks ──────────────────────────────────────────────────────────────

#: Instants with microseconds: the ``iat`` cut-off is compared to the microsecond
#: (``token_is_revoked``), so a column that drops them is a fault.
_VERIFIED = datetime(2026, 1, 2, 3, 4, 5, 100001, tzinfo=UTC)
_SIGNED_IN = datetime(2026, 1, 3, 4, 5, 6, 200002, tzinfo=UTC)

#: The nullable members cleared again after :func:`_account_changes`, and the account made
#: active again: a store that skips a None would keep an admin's forced password change.
_ACCOUNT_CLEARS: Mapping[str, object] = {
    "is_active": True,
    "email_verified_at": None,
    "password_change_required_at": None,
    "deletion_requested_at": None,
    "deletion_scheduled_at": None,
    "text_size": None,
    "contrast": None,
    "device_timezone": None,
    "translation_review_locales": None,
    "translation_review_areas": None,
    "sessions_invalid_before": None,
}

#: The members a new account starts without (``NewAccount`` carries none of them).
_UNSET_ON_ADD = (
    "password_change_required_at",
    "deletion_requested_at",
    "deletion_scheduled_at",
    "text_size",
    "contrast",
    "translation_review_locales",
    "translation_review_areas",
)


def _account_changes(token: str, role: str) -> dict[str, Any]:
    """A new value for every :data:`~eifi1_server_kit.auth.ports.ACCOUNT_FIELDS` member."""
    return {
        "email": _address(token, "changed"),
        "password_hash": f"check-hash-{token}-changed",
        "first_name": "Grace",
        "last_name": "Changed",
        "locale": "fr",
        "role": role,
        "is_active": False,
        "email_verified_at": datetime(2026, 2, 1, 1, 0, 0, 300003, tzinfo=UTC),
        "last_login_at": datetime(2026, 2, 2, 2, 0, 0, 400004, tzinfo=UTC),
        "password_change_required_at": datetime(2026, 2, 3, 3, 0, 0, 500005, tzinfo=UTC),
        "deletion_requested_at": datetime(2026, 2, 4, 4, 0, 0, 600006, tzinfo=UTC),
        "deletion_scheduled_at": datetime(2026, 3, 6, 4, 0, 0, 600006, tzinfo=UTC),
        "text_size": "large",
        "contrast": "more",
        "device_timezone": "Asia/Shanghai",
        "translation_review_locales": ["fr", "it"],
        "translation_review_areas": ["auth", "feedback"],
        "sessions_invalid_before": datetime(2026, 2, 5, 5, 0, 0, 700007, tzinfo=UTC),
    }


async def _people_grew(store: UserStore[Any], before: int, by: int, message: str) -> int:
    """``count_people`` grew by ``by`` since ``before``; answers the count now."""
    now = await store.count_people()
    _check(now - before == by, message)
    return now


async def check_user_store(
    store: UserStore[Any],
    *,
    role: str = "MEMBER",
    other_role: str = "ADMIN",
    add_demo: Callable[[NewAccount], Awaitable[AccountRow]] | None = None,
) -> None:
    """Does the app's account store keep the port's promises (§12.3)? Raises
    :class:`AssertionError` naming the first that fails.

    Needs nothing from outside: it adds two accounts under fresh addresses, applies a new
    value for every :data:`~eifi1_server_kit.auth.ports.ACCOUNT_FIELDS` member to one, clears
    the nullable ones again, and deletes both at the end, so it runs on the app's test
    database as it is. ``role`` and ``other_role`` are two of the app's role values (its
    column may be an enum), by default :class:`~eifi1_server_kit.auth.service.AccountPolicy`'s
    ``default_role`` and ``admin_role`` (upper case since 0.30, so a column that holds only
    the app's names takes them). A new account must be active, with every member ``NewAccount``
    does not carry None; ``sessions_invalid_before`` is checked only on a row that has the
    member (an app without the ``iat`` cut-off ignores the write).

    ``count_people`` is checked by the change each step makes: a new account counts, a
    deactivated one or one asking for its deletion still counts (``registration_decision``'s
    first account must not come round again), a deleted one no longer does. With
    ``add_demo``, the app's way to insert a demo account from a :class:`NewAccount` (its demo
    session's), a demo account must not count; the check deletes it through the store.
    """
    token = secrets.token_hex(8)
    people = await store.count_people()
    holders = await store.count_active(role=role)
    ada = NewAccount(
        email=_address(token, 1),
        password_hash=f"check-hash-{token}-1",
        first_name="Ada",
        last_name="Check",
        locale="de-CH",
        role=role,
        email_verified_at=_VERIFIED,
        last_login_at=_SIGNED_IN,
        device_timezone="Europe/Zurich",
    )
    grace = NewAccount(
        email=_address(token, 2),
        password_hash=f"check-hash-{token}-2",
        first_name="Grace",
        last_name="",
        locale="en",
        role=role,
    )
    first = await store.add(ada)
    second = await store.add(grace)

    # 1. add: the id is set, every field reads back, the rest starts blank.
    _check(isinstance(first.id, int) and isinstance(second.id, int), "add: the id is set by the flush")
    _check(first.id != second.id, "add: each account has its own id")
    _check_written(first, _fields(ada), "add")
    _check_written(second, _fields(grace), "add")
    _check(isinstance(first.created_at, datetime), "add: created_at is set")
    _check(first.is_active is True, "add: a new account is active")
    for name in _UNSET_ON_ADD:
        _check(getattr(first, name, _ABSENT) is None, f"add: {name} starts as None")
    _check(getattr(first, "is_demo", False) is False, "add: a new account is no demo account")
    _check(getattr(first, "sessions_invalid_before", None) is None, "add: a new account has no session cut-off")
    people = await _people_grew(store, people, 2, "count_people: each new account counts")
    _check(
        await store.count_active(role=role) == holders + 2,
        "count_active: each new active account with the role counts",
    )

    # 2. The lookups.
    found = await store.get(first.id)
    _check(found is not None and found.id == first.id, "get: the account by its id")
    _check(await store.get(_MISSING_ID) is None, "get: a missing id is None")
    found = await store.by_email(ada.email)
    _check(found is not None and found.id == first.id, "by_email: the account with exactly this address")
    _check(
        await store.by_email(f"check-{token}-1+tag@example.test") is None,
        "by_email: exactly the address, not a tagged variant of it",
    )
    _check(await store.by_email(_address(token, "absent")) is None, "by_email: an address with no account is None")
    many = {row.id for row in await store.by_emails([ada.email, grace.email, _address(token, "absent")])}
    _check(many == {first.id, second.id}, "by_emails: the accounts of the addresses given, and no others")
    _check(list(await store.by_emails([])) == [], "by_emails: no addresses, no accounts")

    # 3. apply: every member the kit may change, then the nullable ones back to None.
    await store.apply(second, {"is_active": False})
    _check(await store.count_active(role=role) == holders + 1, "count_active: a deactivated account does not count")
    await store.apply(second, {"is_active": True, "role": other_role})
    if other_role != role:
        _check(
            await store.count_active(role=role) == holders + 1,
            "count_active: an account with another role does not count",
        )
    changes = _account_changes(token, other_role)
    applied = await store.apply(second, changes)
    _check(applied.id == second.id, "apply: answers the account it changed")
    _check_written(_found(await store.get(second.id), "apply: get still finds the account"), changes, "apply")
    _check(await store.by_email(grace.email) is None, "apply: the old address finds the account no longer")
    moved = await store.by_email(changes["email"])
    _check(moved is not None and moved.id == second.id, "apply: the new address finds the account")
    people = await _people_grew(
        store, people, 0, "count_people: a deactivated account, or one asking for its deletion, is still a person"
    )
    await store.apply(second, _ACCOUNT_CLEARS)
    _check_written(_found(await store.get(second.id), "apply: get still finds the account"), _ACCOUNT_CLEARS, "apply")
    try:
        await store.apply(second, {"no_such_member": token})
    except ValueError:
        pass
    else:
        raise AssertionError("apply: a key outside ACCOUNT_FIELDS raises ValueError, never drops silently")

    # 4. A demo account is no person.
    if add_demo is not None:
        demo = await add_demo(
            NewAccount(
                email=_address(token, "demo"),
                password_hash=f"check-hash-{token}-demo",
                first_name="Demo",
                last_name="",
                locale="en",
                role=role,
            )
        )
        _check(getattr(demo, "is_demo", False) is True, "add_demo: the account it made is a demo account")
        people = await _people_grew(store, people, 0, "count_people: a demo account is no person")
        _check(await store.count_active(role=role) == holders + 1, "count_active: a demo account is no person")
        await store.delete(demo)

    # 5. delete.
    await store.delete(second)
    _check(await store.get(second.id) is None, "delete: get finds the account no longer")
    _check(await store.by_email(changes["email"]) is None, "delete: by_email finds the account no longer")
    people = await _people_grew(store, people, -1, "count_people: a deleted account counts no longer")
    await store.delete(first)


async def check_session_store(store: SessionStore[Any], *, user_id: int, other_user_id: int) -> None:
    """Does the app's session store keep the port's promises (§12.3, server-kit 0.27)? Raises
    :class:`AssertionError` naming the first that fails.

    ``user_id`` and ``other_user_id`` are two existing accounts (the rows' foreign key). The
    check ends every session of ``user_id``, so pass an account made for it; of
    ``other_user_id`` it starts one session and ends only that one. Every ``expires_at`` lies
    ahead, so a store whose ``live`` also leaves out an expired session passes too.
    """
    _two_accounts(user_id, other_user_id)
    token = secrets.token_hex(8)
    base = utc_now()
    already = {row.sid for row in await store.live(user_id)}

    def at(seconds: float) -> datetime:
        return base + timedelta(seconds=seconds)

    plain = NewSession(sid=f"check-{token}-1", user_id=user_id, started_at=base, expires_at=at(86_400))
    shared_new = NewSession(
        sid=f"check-{token}-2",
        user_id=user_id,
        started_at=at(1),
        expires_at=at(3_600),
        shared_device=True,
        device="Firefox on Linux",
        ip_address="2001:db8::ffff:203.0.113.7",
    )
    first = await store.start(plain)
    shared = await store.start(shared_new)
    third = await store.start(
        NewSession(sid=f"check-{token}-3", user_id=user_id, started_at=at(2), expires_at=at(86_400))
    )
    others = await store.start(
        NewSession(sid=f"check-{token}-4", user_id=other_user_id, started_at=base, expires_at=at(86_400))
    )

    # 1. start and get.
    _check_written(first, _fields(plain), "start")
    _check_written(shared, _fields(shared_new), "start")
    _check(all(row.is_active is True for row in (first, shared, third, others)), "start: a new session is active")
    _check(_same_instant(first.last_active, plain.started_at), "start: last_active is started_at")
    found = await store.get(first.sid)
    _check(found is not None and found.sid == first.sid, "get: the session by its sid")
    _check(await store.get(f"check-{token}-absent") is None, "get: an unknown sid is None")

    # 2. touch, and live by the latest activity: never the order they started in.
    await store.touch(first, last_active=at(5), expires_at=at(172_800))
    await store.touch(shared, last_active=at(10), expires_at=at(7_200))
    await store.touch(third, last_active=at(7), expires_at=at(172_800))
    touched = _found(await store.get(first.sid), "get: the touched session")
    _check(
        _same_instant(touched.last_active, at(5)) and _same_instant(touched.expires_at, at(172_800)),
        "touch: last_active and expires_at read back",
    )
    live = list(await store.live(user_id))
    _check(all(row.user_id == user_id and row.is_active for row in live), "live: the account's active sessions only")
    _check(_latest_first([row.last_active for row in live]), "live: the latest activity first")
    ours = [row.sid for row in live if row.sid in {first.sid, shared.sid, third.sid}]
    _check(
        ours == [shared.sid, third.sid, first.sid], "live: all three, the latest activity first, not the latest start"
    )
    await store.touch(third, last_active=at(10), expires_at=at(172_800))
    tied = [row.sid for row in await store.live(user_id) if row.sid in {shared.sid, third.sid}]
    _check(tied == [third.sid, shared.sid], "live: the higher sid first on a tie")
    _check(others.sid in {row.sid for row in await store.live(other_user_id)}, "live: another account's own session")

    # 3. end: the row stays and is no longer active; ending it twice is no error.
    await store.end(third)
    ended = await store.get(third.sid)
    _check(ended is not None and ended.is_active is False, "end: get still finds an ended session, inactive")
    _check(third.sid not in {row.sid for row in await store.live(user_id)}, "live: not an ended session")
    await store.end(third)

    # 4. end_all: the active ones but ``keep``, counted; another account's stay.
    ended_count = await store.end_all(user_id, keep=first.sid)
    _check(
        ended_count == len(already - {first.sid}) + 1,
        "end_all: answers how many it ended: the active sessions but keep, not one ended before",
    )
    _check([row.sid for row in await store.live(user_id)] == [first.sid], "end_all: keeps keep and ends the rest")
    gone = await store.get(shared.sid)
    _check(gone is not None and gone.is_active is False, "end_all: get finds an ended session inactive")
    kept = await store.get(others.sid)
    _check(kept is not None and kept.is_active is True, "end_all: another account's session stays active")
    _check(await store.end_all(user_id) == 1, "end_all: without keep, every active session ends (here the one kept)")
    _check(list(await store.live(user_id)) == [], "end_all: without keep, no active session is left")
    _check(await store.end_all(user_id) == 0, "end_all: with nothing left to end, 0")
    await store.end(others)


async def check_one_time_token_store(store: OneTimeTokenStore[Any], *, user_id: int, other_user_id: int) -> None:
    """Does the app's link store keep the port's promises (§12.3)? Raises
    :class:`AssertionError` naming the first that fails.

    ``user_id`` and ``other_user_id`` are two existing accounts (the rows' foreign key). The
    check replaces and drops ``user_id``'s links of every kind (reset, verification, address
    change) and ``other_user_id``'s reset link, so pass accounts made for it. One link is
    expired when it is put: ``take`` and ``pending`` answer it all the same.
    """
    _two_accounts(user_id, other_user_id)
    token = secrets.token_hex(8)
    now = utc_now()
    expired, ahead = now - timedelta(hours=1), now + timedelta(hours=1)
    address, new_address = _address(token, "link"), _address(token, "new")
    reset, verification, change = _LINK_KINDS
    for kind in _LINK_KINDS:
        await store.drop(kind, user_id)
    await store.drop(reset, other_user_id)

    # 1. put, take and pending; an expired link is still answered, reading spends nothing.
    others = await store.put(
        reset,
        user_id=other_user_id,
        token_hash=_digest(token, "other"),
        email=_address(token, "other"),
        expires_at=ahead,
    )
    first = await store.put(reset, user_id=user_id, token_hash=_digest(token, 1), email=address, expires_at=expired)
    _check(OneTimeTokenKind(first.kind) is reset, "put: the kind reads back")
    _check_written(first, {"user_id": user_id, "token_hash": _digest(token, 1), "email": address}, "put")
    _check(_same_instant(first.expires_at, expired), "put: expires_at reads back")
    _check(isinstance(first.created_at, datetime), "put: created_at is set")
    taken = await store.take(reset, _digest(token, 1))
    _check(taken is not None and taken.user_id == user_id, "take: the link by its hash, expired or not")
    _check(await store.take(reset, _digest(token, 1)) is not None, "take: reading does not spend the link")
    _check(await store.take(verification, _digest(token, 1)) is None, "take: a link of another kind is not this one")
    _check(await store.take(reset, _digest(token, "absent")) is None, "take: an unknown hash is None")
    pending = await store.pending(reset, user_id)
    _check(
        pending is not None and pending.token_hash == _digest(token, 1),
        "pending: the account's link of this kind, expired or not",
    )
    _check(await store.pending(verification, user_id) is None, "pending: None for a kind the account has no link of")

    # 2. A second put replaces the account's link of that kind, and nothing else.
    await store.put(reset, user_id=user_id, token_hash=_digest(token, 2), email=address, expires_at=ahead)
    _check(await store.take(reset, _digest(token, 1)) is None, "put: replaces the account's earlier link of this kind")
    taken = await store.take(reset, _digest(token, 2))
    _check(taken is not None and taken.user_id == user_id, "put: the new link is taken by its hash")
    pending = await store.pending(reset, user_id)
    _check(pending is not None and pending.token_hash == _digest(token, 2), "pending: the newest link of the kind")
    pending = await store.pending(reset, other_user_id)
    _check(
        pending is not None and pending.token_hash == others.token_hash,
        "put: another account's link of the same kind stays",
    )
    await store.put(verification, user_id=user_id, token_hash=_digest(token, 3), email=address, expires_at=ahead)
    await store.put(change, user_id=user_id, token_hash=_digest(token, 4), email=new_address, expires_at=ahead)
    pending = await store.pending(reset, user_id)
    _check(
        pending is not None and pending.token_hash == _digest(token, 2), "put: a link of another kind replaces no reset"
    )
    pending = await store.pending(change, user_id)
    _check(
        pending is not None and OneTimeTokenKind(pending.kind) is change and pending.email == new_address,
        "pending: the address change's link, holding the new address",
    )
    _check(await store.take(change, _digest(token, 3)) is None, "take: a verification link is no address change's link")

    # 3. drop: the one kind of the one account; twice is no error.
    await store.drop(reset, user_id)
    _check(await store.pending(reset, user_id) is None, "drop: the account's link of this kind is gone")
    _check(await store.take(reset, _digest(token, 2)) is None, "drop: its hash finds nothing")
    _check(
        await store.pending(verification, user_id) is not None and await store.pending(change, user_id) is not None,
        "drop: the account's links of the other kinds stay",
    )
    _check(await store.pending(reset, other_user_id) is not None, "drop: another account's link stays")
    await store.drop(reset, user_id)
    for kind in (verification, change):
        await store.drop(kind, user_id)
    await store.drop(reset, other_user_id)


async def check_invitation_store(
    store: InvitationStore[Any], *, inviter_id: int, user_id: int, role: str = "MEMBER"
) -> None:
    """Does the app's invitation store keep the port's promises (§12.3, auth §4.4)? Raises
    :class:`AssertionError` naming the first that fails.

    ``inviter_id`` and ``user_id`` are existing accounts (they may be the same): every
    invitation is sent by ``inviter_id`` and one is accepted by ``user_id``. It adds five
    invitations to fresh addresses (open, expired, accepted, revoked, to another address)
    and asserts only on those, so it runs on the app's test database as it is; ``listing``'s
    order is checked over every row it answers. ``role`` is one of the app's role values,
    by default the policy's ``default_role`` (upper case since 0.30).
    ``revoke`` may keep the row with its ``revoked_at`` or delete it, as the port allows; a
    kept one is still listed, since ``listing`` leaves out only accepted ones.
    """
    token = secrets.token_hex(8)
    now = utc_now()
    ahead = now + INVITE_TTL
    address, elsewhere = _address(token, "invitee"), _address(token, "elsewhere")

    def invitation(label: object, **fields: Any) -> NewInvitation:
        values: dict[str, Any] = {
            "email": address,
            "token_hash": _digest(token, label),
            "expires_at": ahead,
            "locale": "fr",
            "invited_by_user_id": inviter_id,
        }
        values.update(fields)
        return NewInvitation(**values)

    full = invitation(1, role=role, scope={"team_id": 7, "team_role": "manager"}, note=f"Checked ({token})")
    first = await store.add(full)
    expired = await store.add(invitation(2, expires_at=now - timedelta(days=1)))
    accepted = await store.add(invitation(3))
    revoked = await store.add(invitation(4))
    other = await store.add(invitation(5, email=elsewhere))

    # 1. add, get, by_token_hash.
    ids = {row.id for row in (first, expired, accepted, revoked, other)}
    _check(all(isinstance(row_id, int) for row_id in ids) and len(ids) == 5, "add: each invitation has its own id")
    _check_written(first, _fields(full), "add")
    _check(isinstance(first.created_at, datetime), "add: created_at is set")
    _check(
        first.accepted_at is None and first.accepted_by_user_id is None and first.revoked_at is None,
        "add: a new invitation is neither accepted nor revoked",
    )
    _check(
        expired.role is None and expired.scope is None and expired.note is None, "add: no role, scope or note is None"
    )
    found = await store.get(first.id)
    _check(found is not None and found.id == first.id, "get: the invitation by its id")
    _check(await store.get(_MISSING_ID) is None, "get: a missing id is None")
    found = await store.by_token_hash(_digest(token, 1))
    _check(found is not None and found.id == first.id, "by_token_hash: the invitation whose link this is")
    _check(await store.by_token_hash(_digest(token, "absent")) is None, "by_token_hash: an unknown hash is None")

    # 2. accept and revoke; by_token_hash answers an invitation in any state.
    accepted_at, revoked_at = now - timedelta(minutes=2), now - timedelta(minutes=1)
    await store.accept(accepted, user_id=user_id, at=accepted_at)
    again = _found(await store.get(accepted.id), "accept: get still finds the invitation")
    _check(
        _same_instant(again.accepted_at, accepted_at) and again.accepted_by_user_id == user_id,
        "accept: accepted_at and accepted_by_user_id read back",
    )
    found = await store.by_token_hash(_digest(token, 3))
    _check(found is not None and found.id == accepted.id, "by_token_hash: an accepted invitation too")
    await store.revoke(revoked, at=revoked_at)
    withdrawn = await store.get(revoked.id)
    if withdrawn is not None:
        _check(_same_instant(withdrawn.revoked_at, revoked_at), "revoke: revoked_at reads back")
        found = await store.by_token_hash(_digest(token, 4))
        _check(found is not None and found.id == revoked.id, "by_token_hash: a revoked invitation too")

    # 3. open_for: neither accepted nor revoked, expired ones included.
    open_ids = {row.id for row in await store.open_for(address)}
    _check(expired.id in open_ids, "open_for: an expired invitation is still open (the kit decides on the expiry)")
    _check(accepted.id not in open_ids, "open_for: not an accepted invitation")
    _check(revoked.id not in open_ids, "open_for: not a revoked invitation")
    _check(open_ids == {first.id, expired.id}, "open_for: the address's open invitations, and no others")
    _check({row.id for row in await store.open_for(elsewhere)} == {other.id}, "open_for: only the address given")

    # 4. listing: every invitation not accepted, newest first.
    listing = list(await store.listing())
    listed = {row.id for row in listing}
    _check(all(row.accepted_at is None for row in listing), "listing: not an accepted invitation")
    _check(accepted.id not in listed, "listing: not the invitation accepted here")
    _check({first.id, expired.id, other.id} <= listed, "listing: every invitation not accepted, expired ones too")
    _check(withdrawn is None or revoked.id in listed, "listing: a revoked invitation the store keeps is still listed")
    _check(
        _in_order([(row.created_at, row.id) for row in listing], newest_first=True),
        "listing: newest first, the higher id first on a tie",
    )

    # 5. remint: a new link for the same invitation, the old one dead.
    later = now + 2 * INVITE_TTL
    reminted = await store.remint(first, token_hash=_digest(token, 6), expires_at=later)
    _check(reminted.id == first.id, "remint: answers the same invitation")
    _check(
        reminted.token_hash == _digest(token, 6) and _same_instant(reminted.expires_at, later),
        "remint: the new token_hash and expires_at read back",
    )
    _check(await store.by_token_hash(_digest(token, 1)) is None, "remint: the old link finds nothing")
    found = await store.by_token_hash(_digest(token, 6))
    _check(found is not None and found.id == first.id, "remint: the new link finds the invitation")
    _check(first.id in {row.id for row in await store.open_for(address)}, "remint: the invitation is still open")


async def check_passkey_store(store: PasskeyStore[Any], *, user_id: int, other_user_id: int) -> None:
    """Does the app's passkey store keep the port's promises (§12.3, §12.5 item 8)? Raises
    :class:`AssertionError` naming the first that fails.

    ``user_id`` and ``other_user_id`` are two existing accounts (the rows' foreign key). It
    adds three passkeys under fresh credential ids, binary as an authenticator's are, and
    removes them again; ``for_user``'s order is checked over every row it answers, so the
    accounts may have passkeys of their own. The counter is set to the largest an
    authenticator sends (2³² − 1), which a signed 32-bit column cannot hold. Two fresh
    challenges are spent; the check does not wait for one to expire.
    """
    _two_accounts(user_id, other_user_id)
    token = secrets.token_bytes(16)

    def credential(number: int) -> bytes:
        # A NUL and a high byte first: a column that treats the id as text fails here.
        return b"\x00\xff" + token + bytes([number])

    laptop = NewPasskey(
        user_id=user_id,
        credential_id=credential(1),
        public_key=b"\xa5\x01\x02" + token,
        sign_count=0,
        name="Laptop",
        transports="internal,hybrid",
    )
    phone = NewPasskey(
        user_id=user_id, credential_id=credential(2), public_key=b"\xa5\x01\x03" + token, sign_count=3, name="Phone"
    )
    first = await store.add(laptop)
    second = await store.add(phone)
    others = await store.add(
        NewPasskey(
            user_id=other_user_id,
            credential_id=credential(3),
            public_key=b"\xa5\x01\x04" + token,
            sign_count=0,
            name="Elsewhere",
        )
    )

    # 1. add and the lookups.
    ids = {first.id, second.id, others.id}
    _check(all(isinstance(row_id, int) for row_id in ids) and len(ids) == 3, "add: each passkey has its own id")
    _check_written(first, _fields(laptop), "add")
    _check_written(second, _fields(phone), "add")
    _check(isinstance(first.created_at, datetime), "add: created_at is set")
    _check(first.last_used_at is None, "add: a new passkey was never used")
    mine = list(await store.for_user(user_id))
    _check(all(row.user_id == user_id for row in mine), "for_user: the account's passkeys only")
    _check(
        _in_order([(row.created_at, row.id) for row in mine], newest_first=False),
        "for_user: oldest first, the lower id first on a tie",
    )
    ours = [row.id for row in mine if row.id in ids]
    _check(ours == [first.id, second.id], "for_user: both of the account's, the older first, not another's")
    found = await store.by_credential_id(credential(1))
    _check(found is not None and found.id == first.id, "by_credential_id: the passkey with this credential id")
    _check(await store.by_credential_id(credential(9)) is None, "by_credential_id: an unknown credential id is None")

    # 2. used, rename, remove.
    used_at = utc_now()
    await store.used(first, sign_count=2**32 - 1, at=used_at)
    again = _found(await store.by_credential_id(credential(1)), "used: the passkey is still there")
    _check(
        again.sign_count == 2**32 - 1 and _same_instant(again.last_used_at, used_at),
        "used: the new counter and time read back",
    )
    await store.rename(first, "Work laptop")
    renamed = _found(await store.by_credential_id(credential(1)), "rename: the passkey is still there")
    _check(renamed.name == "Work laptop", "rename: the new name reads back")
    await store.remove(second)
    _check(await store.by_credential_id(credential(2)) is None, "remove: the passkey is gone")
    _check(second.id not in {row.id for row in await store.for_user(user_id)}, "remove: for_user lists it no longer")
    _check(await store.by_credential_id(credential(3)) is not None, "remove: another account's passkey stays")

    # 3. One use per challenge.
    challenge, another = hash_token(f"challenge-{token.hex()}-1"), hash_token(f"challenge-{token.hex()}-2")
    ahead = utc_now() + timedelta(minutes=5)
    _check(await store.spend_challenge(challenge, expires_at=ahead) is True, "spend_challenge: True the first time")
    _check(
        await store.spend_challenge(challenge, expires_at=ahead) is False,
        "spend_challenge: a second spend of the same challenge is refused",
    )
    _check(
        await store.spend_challenge(another, expires_at=ahead) is True, "spend_challenge: another challenge is its own"
    )
    await store.remove(first)
    await store.remove(others)


async def check_admin_action_log(log: AdminActionLog[Any], *, actor_id: int, target_user_id: int) -> None:
    """Does the app's admin log keep the port's promises (§12.3, user-admin §4.3)? Raises
    :class:`AssertionError` naming the first that fails.

    ``actor_id`` and ``target_user_id`` are existing accounts (the rows' foreign keys; they
    may be the same). It adds five rows built by
    :func:`~eifi1_server_kit.user_admin.admin_action_record`: four about ``target_user_id``,
    written out of time order and two of them in the same instant, so "newest first" is by
    ``at`` and then the higher id, and one invitation with no target. ``page`` is checked
    against the totals it answered before, by paging through every row of the target, and
    with ``target_user_id=None`` (every row); so the log may hold rows of its own, though a
    large one makes the check slow. ``scrub_email`` must clear exactly the address given.
    """
    token = secrets.token_hex(8)
    target_email, invitee = _address(token, "target"), _address(token, "invitee")
    base = utc_now()
    _, before_target = await log.page(target_user_id=target_user_id, limit=1, offset=0)
    _, before_all = await log.page(target_user_id=None, limit=1, offset=0)

    def record(action: AdminAction, seconds: int, **fields: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "actor_id": actor_id,
            "target_user_id": target_user_id,
            "target_email": target_email,
            "now": base + timedelta(seconds=seconds),
        }
        values.update(fields)
        return admin_action_record(action, **values)

    role_record = record(AdminAction.ROLE, 30, detail={"from": "MEMBER", "to": "ADMIN"})
    latest = await log.add(role_record)
    earliest = await log.add(record(AdminAction.DEACTIVATE, 10, detail={"sessions_ended": True}))
    tied_first = await log.add(record(AdminAction.REACTIVATE, 20))
    tied_second = await log.add(record(AdminAction.PASSWORD_CHANGE_REQUIRE, 20))
    invite_record = record(AdminAction.INVITE, 40, target_user_id=None, target_email=invitee, detail={"role": "MEMBER"})
    invitation = await log.add(invite_record)

    # 1. add: the id is set, the record reads back.
    ids = [row.id for row in (latest, earliest, tied_first, tied_second, invitation)]
    _check(all(isinstance(row_id, int) for row_id in ids) and len(set(ids)) == 5, "add: each row has its own id")
    _check_written(latest, role_record, "add")
    _check_written(invitation, invite_record, "add")

    # 2. page: the target's rows, newest first, with the total of all; limit and offset walk it.
    total_target = before_target + 4
    whole, total = await log.page(target_user_id=target_user_id, limit=total_target, offset=0)
    _check(total == total_target, "page: the total counts every row of the target, not the page")
    _check(len(whole) == total, "page: as many rows as the limit allows")
    _check(all(row.target_user_id == target_user_id for row in whole), "page: the target's rows only")
    _check(
        _in_order([(row.at, row.id) for row in whole], newest_first=True),
        "page: newest first, the higher id first on a tie",
    )
    ours = [row.id for row in whole if row.id in ids]
    _check(
        ours == [latest.id, tied_second.id, tied_first.id, earliest.id],
        "page: newest first by at, then the higher id, not in the order written",
    )
    walked: list[int] = []
    for offset in range(0, total, 2):
        rows, counted = await log.page(target_user_id=target_user_id, limit=2, offset=offset)
        _check(len(rows) <= 2 and counted == total, "page: at most limit rows, and always the total of all")
        walked.extend(row.id for row in rows)
    _check(walked == [row.id for row in whole], "page: offset and limit walk the same order, no row twice or missing")
    rows, counted = await log.page(target_user_id=target_user_id, limit=2, offset=total)
    _check(list(rows) == [] and counted == total, "page: past the end, no rows and still the total")
    everything, total_all = await log.page(target_user_id=None, limit=before_all + 5, offset=0)
    _check(total_all == before_all + 5, "page(target_user_id=None): the total counts every row")
    _check(set(ids) <= {row.id for row in everything}, "page(target_user_id=None): every row, with a target or none")
    _check(
        _in_order([(row.at, row.id) for row in everything], newest_first=True),
        "page(target_user_id=None): newest first, the higher id first on a tie",
    )

    # 3. scrub_email: exactly this address, wherever it is; the rest of the row stays.
    await log.scrub_email(target_email)
    scrubbed, _ = await log.page(target_user_id=target_user_id, limit=total, offset=0)
    ours_now = [row for row in scrubbed if row.id in ids]
    # The erasure's foreign key nulls target_user_id, not the scrub: a scrub that did would
    # lose the rows from the person's page here.
    _check(len(ours_now) == 4, "scrub_email: the rows stay, target_user_id with them")
    _check(
        all(row.target_email is None for row in ours_now),
        "scrub_email: target_email cleared wherever it is this address",
    )
    everything, _ = await log.page(target_user_id=None, limit=total_all, offset=0)
    untouched = [row for row in everything if row.id == invitation.id]
    _check(len(untouched) == 1 and untouched[0].target_email == invitee, "scrub_email: another address stays")
    await log.scrub_email(invitee)
    everything, _ = await log.page(target_user_id=None, limit=total_all, offset=0)
    cleared = [row for row in everything if row.id == invitation.id]
    _check(len(cleared) == 1 and cleared[0].target_email is None, "scrub_email: a row with no target is scrubbed too")


async def check_after_commit(
    after_commit: AfterCommit,
    *,
    commit: Callable[[], Awaitable[None]],
    rollback: Callable[[], Awaitable[None]],
    settle: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Does the app's ``AfterCommit`` keep the port's promises (§12.3)? Raises
    :class:`AssertionError` naming the first that fails.

    ``commit`` and ``rollback`` end the transaction ``after_commit`` is bound to the way the
    app's request does (``session.commit``, ``session.rollback``). ``settle``, when given, is
    awaited after each, for an app that runs the queue in a task of its own from the commit
    event. The check queues only its own work: queued work does not run before the commit, a
    rollback drops it for good, a commit runs each item once and on its own (one that raises
    does not stop the rest), and ``commit_now`` is a commit. Not checked: that the failure
    is logged.
    """
    ran: list[str] = []

    def work(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            ran.append(name)

        return run

    async def fails() -> None:
        raise RuntimeError("work queued by check_after_commit fails on purpose")

    async def settled(end: Callable[[], Awaitable[None]]) -> None:
        await end()
        if settle is not None:
            await settle()

    after_commit.later(work("rolled back"))
    _check(ran == [], "later: nothing runs before the commit")
    await settled(rollback)
    await settled(commit)
    _check(ran == [], "rollback: the queue is dropped, and the next commit does not run it")
    after_commit.later(fails)
    after_commit.later(work("committed"))
    _check(ran == [], "later: nothing runs before the commit, after a rollback either")
    await settled(commit)
    _check(ran == ["committed"], "commit: the queue runs, each item on its own: one that raises, the rest run")
    await settled(commit)
    _check(ran == ["committed"], "commit: each item runs once")
    after_commit.later(work("commit_now"))
    await settled(after_commit.commit_now)
    _check(ran == ["committed", "commit_now"], "commit_now: commits, so the queue runs")


#: The software authenticator's names (:mod:`~eifi1_server_kit.auth.soft_webauthn`), loaded on
#: first use: it needs cbor2 and cryptography (the ``passkeys`` extra), and an app that tests
#: no passkey imports this module without them.
_SOFT_WEBAUTHN = frozenset({"SoftAuthenticator", "SoftCredential"})


def __getattr__(name: str) -> object:
    """The software authenticator, imported only when asked for."""
    if name in _SOFT_WEBAUTHN:
        from eifi1_server_kit.auth import soft_webauthn

        return getattr(soft_webauthn, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
