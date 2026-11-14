"""Private in-memory fakes of the account layer's ports, for the kit's own tests.

Another session writes the shared fakes and contract suites as ``eifi1_server_kit.auth.testing``;
these stay private to the kit's tests until they are swapped for it. They hold the lead's
facts of the ``[sql]`` stores (2026-11-13): every datetime aware UTC, ``apply`` refuses a
key the row lacks, ``spend_challenge`` answers True once, ``commit_now`` runs the work queued
before it, and a session's ``last_active`` starts at ``started_at``.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from eifi1_server_kit.auth.ports import (
    ACCOUNT_FIELDS,
    NewAccount,
    NewInvitation,
    NewPasskey,
    NewSession,
)
from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.mail import MailMessage

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


class Clock:
    """A clock a test moves."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


# --- accounts ------------------------------------------------------------------------------


@dataclass
class MemoryUser:
    """An account of a new app: no ``iat`` cut-off, no demo column."""

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


@dataclass
class CutoffUser(MemoryUser):
    """An account of an app that keeps the ``iat`` cut-off and demos (keksdose, Kurvenschmiede)."""

    sessions_invalid_before: datetime | None = None
    is_demo: bool = False


class MemoryUserStore:
    def __init__(self, clock: Clock, *, cutoff: bool = False) -> None:
        self.rows: dict[int, MemoryUser] = {}
        self._ids = itertools.count(1)
        self._clock = clock
        self._kind: type[MemoryUser] = CutoffUser if cutoff else MemoryUser
        self.applied: list[dict[str, Any]] = []

    def seed(self, **values: Any) -> MemoryUser:
        values.setdefault("password_hash", "hashed:correct horse")
        values.setdefault("first_name", "Ada")
        values.setdefault("last_name", "Lovelace")
        values.setdefault("locale", "en")
        values.setdefault("role", "MEMBER")
        values.setdefault("created_at", self._clock())
        row = self._kind(id=next(self._ids), **values)
        self.rows[row.id] = row
        return row

    async def get(self, user_id: int) -> MemoryUser | None:
        return self.rows.get(user_id)

    async def by_email(self, email: str) -> MemoryUser | None:
        return next((row for row in self.rows.values() if row.email == email), None)

    async def by_emails(self, emails: Sequence[str]) -> Sequence[MemoryUser]:
        return [row for row in self.rows.values() if row.email in emails]

    async def count_people(self) -> int:
        return sum(1 for row in self.rows.values() if not getattr(row, "is_demo", False))

    async def count_active(self, *, role: str) -> int:
        return sum(1 for row in self.rows.values() if row.is_active and row.role == role)

    async def add(self, new: NewAccount) -> MemoryUser:
        values = {item.name: getattr(new, item.name) for item in fields(new)}
        return self.seed(**values)

    async def apply(self, row: MemoryUser, changes: Mapping[str, Any]) -> MemoryUser:
        for key, value in changes.items():
            if key not in ACCOUNT_FIELDS or not hasattr(row, key):
                raise ValueError(f"no column {key}")
            setattr(row, key, value)
        self.applied.append(dict(changes))
        return row

    async def delete(self, row: MemoryUser) -> None:
        del self.rows[row.id]


# --- sessions ------------------------------------------------------------------------------


@dataclass
class MemorySession:
    sid: str
    user_id: int
    started_at: datetime
    last_active: datetime
    expires_at: datetime
    shared_device: bool = False
    device: str | None = None
    ip_address: str | None = None
    is_active: bool = True


class MemorySessionStore:
    def __init__(self) -> None:
        self.rows: dict[str, MemorySession] = {}

    async def start(self, new: NewSession) -> MemorySession:
        row = MemorySession(
            sid=new.sid,
            user_id=new.user_id,
            started_at=new.started_at,
            last_active=new.started_at,
            expires_at=new.expires_at,
            shared_device=new.shared_device,
            device=new.device,
            ip_address=new.ip_address,
        )
        self.rows[row.sid] = row
        return row

    async def get(self, sid: str) -> MemorySession | None:
        return self.rows.get(sid)

    async def touch(self, session: MemorySession, *, last_active: datetime, expires_at: datetime) -> None:
        session.last_active, session.expires_at = last_active, expires_at

    async def end(self, session: MemorySession) -> None:
        session.is_active = False

    async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
        ended = [row for row in self.rows.values() if row.user_id == user_id and row.is_active and row.sid != keep]
        for row in ended:
            row.is_active = False
        return len(ended)

    async def live(self, user_id: int) -> Sequence[MemorySession]:
        rows = [row for row in self.rows.values() if row.user_id == user_id and row.is_active]
        return sorted(rows, key=lambda row: row.last_active, reverse=True)

    def of(self, user_id: int) -> list[MemorySession]:
        return [row for row in self.rows.values() if row.user_id == user_id]


# --- one-time links --------------------------------------------------------------------------


@dataclass
class MemoryToken:
    kind: OneTimeTokenKind
    user_id: int
    token_hash: str
    email: str
    expires_at: datetime
    created_at: datetime


class MemoryTokenStore:
    def __init__(self, clock: Clock) -> None:
        self.rows: list[MemoryToken] = []
        self._clock = clock

    async def put(
        self, kind: OneTimeTokenKind, *, user_id: int, token_hash: str, email: str, expires_at: datetime
    ) -> MemoryToken:
        await self.drop(kind, user_id)
        row = MemoryToken(kind, user_id, token_hash, email, expires_at, self._clock())
        self.rows.append(row)
        return row

    async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryToken | None:
        return next((row for row in self.rows if row.kind == kind and row.token_hash == token_hash), None)

    async def pending(self, kind: OneTimeTokenKind, user_id: int) -> MemoryToken | None:
        return next((row for row in self.rows if row.kind == kind and row.user_id == user_id), None)

    async def drop(self, kind: OneTimeTokenKind, user_id: int) -> None:
        self.rows = [row for row in self.rows if not (row.kind == kind and row.user_id == user_id)]


# --- invitations -----------------------------------------------------------------------------


@dataclass
class MemoryInvitation:
    id: int
    email: str
    token_hash: str | None
    expires_at: datetime | None
    locale: str
    invited_by_user_id: int | None
    created_at: datetime
    role: str | None = None
    scope: Mapping[str, Any] | None = None
    note: str | None = None
    accepted_at: datetime | None = None
    accepted_by_user_id: int | None = None
    revoked_at: datetime | None = None


class MemoryInvitationStore:
    def __init__(self, clock: Clock) -> None:
        self.rows: dict[int, MemoryInvitation] = {}
        self._ids = itertools.count(1)
        self._clock = clock

    def seed(self, **values: Any) -> MemoryInvitation:
        values.setdefault("token_hash", None)
        values.setdefault("expires_at", self._clock() + timedelta(days=14))
        values.setdefault("locale", "en")
        values.setdefault("invited_by_user_id", None)
        values.setdefault("created_at", self._clock())
        row = MemoryInvitation(id=next(self._ids), **values)
        self.rows[row.id] = row
        return row

    async def get(self, invitation_id: int) -> MemoryInvitation | None:
        return self.rows.get(invitation_id)

    async def by_token_hash(self, token_hash: str) -> MemoryInvitation | None:
        return next((row for row in self.rows.values() if row.token_hash == token_hash), None)

    async def open_for(self, email: str) -> Sequence[MemoryInvitation]:
        return [
            row
            for row in self.rows.values()
            if row.email == email and row.accepted_at is None and row.revoked_at is None
        ]

    async def listing(self) -> Sequence[MemoryInvitation]:
        rows = [row for row in self.rows.values() if row.accepted_at is None]
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)

    async def add(self, new: NewInvitation) -> MemoryInvitation:
        values = {item.name: getattr(new, item.name) for item in fields(new)}
        return self.seed(**values)

    async def remint(self, invitation: MemoryInvitation, *, token_hash: str, expires_at: datetime) -> MemoryInvitation:
        invitation.token_hash, invitation.expires_at = token_hash, expires_at
        return invitation

    async def accept(self, invitation: MemoryInvitation, *, user_id: int, at: datetime) -> None:
        invitation.accepted_at, invitation.accepted_by_user_id = at, user_id

    async def revoke(self, invitation: MemoryInvitation, *, at: datetime) -> None:
        invitation.revoked_at = at

    async def forget_accepted(self, user_id: int) -> Sequence[str]:
        gone = [row for row in self.rows.values() if row.accepted_by_user_id == user_id]
        for row in gone:
            del self.rows[row.id]
        return [row.email for row in gone]


# --- passkeys ---------------------------------------------------------------------------------


@dataclass
class MemoryPasskey:
    id: int
    user_id: int
    credential_id: bytes
    public_key: bytes
    sign_count: int
    name: str
    created_at: datetime
    transports: str | None = None
    last_used_at: datetime | None = None


class MemoryPasskeyStore:
    def __init__(self, clock: Clock) -> None:
        self.rows: dict[int, MemoryPasskey] = {}
        self.spent: dict[str, datetime] = {}
        self._ids = itertools.count(1)
        self._clock = clock

    async def for_user(self, user_id: int) -> Sequence[MemoryPasskey]:
        return sorted((row for row in self.rows.values() if row.user_id == user_id), key=lambda row: row.id)

    async def by_credential_id(self, credential_id: bytes) -> MemoryPasskey | None:
        return next((row for row in self.rows.values() if row.credential_id == credential_id), None)

    async def add(self, new: NewPasskey) -> MemoryPasskey:
        values = {item.name: getattr(new, item.name) for item in fields(new)}
        row = MemoryPasskey(id=next(self._ids), created_at=self._clock(), **values)
        self.rows[row.id] = row
        return row

    async def used(self, passkey: MemoryPasskey, *, sign_count: int, at: datetime) -> None:
        passkey.sign_count, passkey.last_used_at = sign_count, at

    async def rename(self, passkey: MemoryPasskey, name: str) -> None:
        self.rows[passkey.id] = replace(passkey, name=name)

    async def remove(self, passkey: MemoryPasskey) -> None:
        del self.rows[passkey.id]

    async def spend_challenge(self, challenge_hash: str, *, expires_at: datetime) -> bool:
        if challenge_hash in self.spent:
            return False
        self.spent[challenge_hash] = expires_at
        return True


# --- the admin log -------------------------------------------------------------------------------


@dataclass
class MemoryAction:
    id: int
    at: datetime
    actor_id: int | None
    action: str
    target_user_id: int | None
    target_email: str | None
    detail: Mapping[str, Any] = field(default_factory=dict)


class MemoryActionLog:
    def __init__(self) -> None:
        self.rows: list[MemoryAction] = []

    async def add(self, record: Mapping[str, Any]) -> MemoryAction:
        row = MemoryAction(id=len(self.rows) + 1, **record)
        self.rows.append(row)
        return row

    async def page(self, *, target_user_id: int | None, limit: int, offset: int) -> tuple[Sequence[MemoryAction], int]:
        rows = [row for row in reversed(self.rows) if target_user_id is None or row.target_user_id == target_user_id]
        return rows[offset : offset + limit], len(rows)

    async def scrub_email(self, email: str) -> None:
        for row in self.rows:
            if row.target_email == email:
                row.target_email = None

    def actions(self) -> list[str]:
        return [row.action for row in self.rows]


# --- after the commit, the signer, the hasher, the mailer ------------------------------------------


class RecordingAfterCommit:
    """The app's queue: ``commit_now`` commits (and runs what waited); the request's end
    runs the rest (``finish``), a refusal drops it (``drop``)."""

    def __init__(self) -> None:
        self.queue: list[Callable[[], Awaitable[None]]] = []
        self.commits = 0

    def later(self, work: Callable[[], Awaitable[None]]) -> None:
        self.queue.append(work)

    async def commit_now(self) -> None:
        await self.finish()

    async def finish(self) -> None:
        self.commits += 1
        queued, self.queue = self.queue, []
        for work in queued:
            await work()

    def drop(self) -> None:
        self.queue = []


class FakeSigner:
    """Signs by keeping the claims; ``verify`` answers them until their ``exp``."""

    def __init__(self, clock: Clock) -> None:
        self._issued: dict[str, dict[str, Any]] = {}
        self._clock = clock
        self._ids = itertools.count(1)

    def sign(self, claims: Mapping[str, Any]) -> str:
        token = f"jwt.{next(self._ids)}"
        self._issued[token] = dict(claims)
        return token

    def verify(self, token: str) -> Mapping[str, Any] | None:
        claims = self._issued.get(token)
        if claims is None or claims["exp"] <= self._clock().timestamp():
            return None
        return claims

    def claims(self, token: str) -> dict[str, Any]:
        return self._issued[token]


class PlainHasher:
    """``hashed:<password>``; counts the checks, so a test sees the dummy one."""

    def __init__(self) -> None:
        self.checks = 0

    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password: str, password_hash: str) -> bool:
        self.checks += 1
        return password_hash == f"hashed:{password}"


@dataclass
class Sent:
    to: str
    message: MailMessage
    kind: str
    #: How many commits had happened when it went: a mail before any is a dead link's risk.
    after_commits: int


class RecordingMailer:
    def __init__(self, after: RecordingAfterCommit) -> None:
        self.sent: list[Sent] = []
        self.works = True
        self._after = after

    async def send(self, to: str, message: MailMessage, *, kind: str = "other") -> bool:
        self.sent.append(Sent(to, message, kind, self._after.commits))
        return self.works

    def of(self, kind: str) -> list[Sent]:
        return [mail for mail in self.sent if mail.kind == kind]

    def link(self, kind: str) -> str:
        """The link of the last mail of ``kind``: the line after the call to action."""
        found = re.search(r"https?://\S+", self.of(kind)[-1].message.text)
        assert found is not None
        return found.group(0)

    def token(self, kind: str) -> str:
        return self.link(kind).rsplit("=", 1)[1]
