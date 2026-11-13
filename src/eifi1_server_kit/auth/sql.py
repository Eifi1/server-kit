"""The account tables as SQLAlchemy declarative mixins, the ``sql`` extra
(``docs/auth-harmonization.md`` §12.3 in ``Eifi1/ui-kit``, decision C of 2026-11-13).

Layer 2's ports read rows through protocols, so an app may keep tables of its own. For the
common case (one role per account, the global admin scope) these mixins ARE the tables: the
app subclasses each on its own declarative base, names the table, adds what is its own (a
column, an index, a relationship) and writes its own Alembic migration. Without them every
app writes the same nine tables again, and the copies drift the way the routes did (§12.2).

* :class:`AccountColumns`: the account row; two optional columns beside it,
  :class:`DemoAccountColumn` for an app with a demo and :class:`SessionCutoffColumn` for one
  that still ends sessions by an ``iat`` cut-off. A new app needs neither: "end every
  session" ends every :class:`SessionColumns` row (§12.5).
* :class:`SessionColumns`: a signed-in session, server-kit 0.27's row (``sid``, the shared
  device and its :func:`~eifi1_server_kit.auth.session_expires_at`).
* :class:`OneTimeTokenColumns`: the reset, verification and address-change links, one row
  per account and :class:`~eifi1_server_kit.auth.tokens.OneTimeTokenKind`.
* :class:`InvitationColumns`: invitations; ``scope`` is the app's (a company, a team, a
  budget) and opaque to the kit.
* :class:`PasskeyColumns` and :class:`SpentChallengeColumns`: WebAuthn credentials, and the
  challenges already answered, so that each is accepted once.
* :class:`AdminActionColumns`: the activity log, the fields
  :func:`~eifi1_server_kit.user_admin.admin_action_record` returns, plus ``scope``.

**The stores** implement :mod:`~eifi1_server_kit.auth.ports` over models built from these
mixins, on the request's ``AsyncSession``: :class:`SqlUserStore`, :class:`SqlSessionStore`,
:class:`SqlOneTimeTokenStore`, :class:`SqlInvitationStore`, :class:`SqlPasskeyStore`,
:class:`SqlAdminActionLog`, and :class:`SqlAfterCommit` for the work that waits for the
commit. Every write flushes and none commits, but :meth:`SqlAfterCommit.commit_now`, the one
place the port allows it. A store sets only mapped attributes: a value for a column the
model lacks (a kastlan ``company_id`` on a log without one) is a :class:`ValueError`, never
a plain attribute the flush silently leaves out.

**What this does not do.** It names no table (the app's ``__tablename__``), picks no base,
seals no column (an app that encrypts one overrides that attribute with its own type), and
creates no schema: ``create_all`` is for tests, the migrations are the app's.

**The account table's name.** Every column that points at an account builds its foreign key
from ``__accounts_table__`` (default ``"users"``). An app whose table is called otherwise sets
it on each subclass, or once on a mixin of its own placed before these.

**Constraints are unnamed**, so the app's ``MetaData(naming_convention=…)`` names them as it
names its own, and its migrations and its models agree. A subclass that needs table arguments
of its own adds them to the mixin's: ``__table_args__ = (*OneTimeTokenColumns.table_args(), …)``.

Needs the ``sql`` extra (``eifi1-server-kit[sql]``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, cast

try:
    from sqlalchemy import (
        JSON,
        BigInteger,
        Boolean,
        CursorResult,
        DateTime,
        Delete,
        ForeignKey,
        Insert,
        Integer,
        LargeBinary,
        String,
        TypeDecorator,
        UniqueConstraint,
        delete,
        event,
        func,
        select,
        update,
    )
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.dialects.postgresql import insert as postgresql_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.engine import Dialect
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Mapped, Session, SessionTransaction, class_mapper, declared_attr, mapped_column
    from sqlalchemy.util import await_only
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.sql needs SQLAlchemy: install the kit with its sql extra, eifi1-server-kit[sql]."
    ) from exc

from eifi1_server_kit.auth.ports import ACCOUNT_FIELDS, NewAccount, NewInvitation, NewPasskey, NewSession
from eifi1_server_kit.auth.schemas import MAX_NAME_LENGTH, MAX_ZONE_LENGTH
from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.clock import utc_now

__all__ = [
    "ACCOUNTS_TABLE",
    "EMAIL_WIDTH",
    "AccountColumns",
    "AccountPrune",
    "AdminActionColumns",
    "DemoAccountColumn",
    "InvitationColumns",
    "OneTimeTokenColumns",
    "PasskeyColumns",
    "SessionColumns",
    "SessionCutoffColumn",
    "SpentChallengeColumns",
    "SqlAdminActionLog",
    "SqlAfterCommit",
    "SqlInvitationStore",
    "SqlOneTimeTokenStore",
    "SqlPasskeyStore",
    "SqlSessionStore",
    "SqlUserStore",
    "UtcDateTime",
    "alembic_render_item",
    "prune_account_rows",
]

logger = logging.getLogger(__name__)

#: The account table every foreign key points at unless a subclass says otherwise.
ACCOUNTS_TABLE = "users"

#: The width of every address column: 320, the apps' columns as they are, so adopting a
#: mixin changes no address column. What a request may send is shorter,
#: :data:`~eifi1_server_kit.auth.schemas.MAX_EMAIL_LENGTH`.
EMAIL_WIDTH = 320

#: :func:`~eifi1_server_kit.auth.hash_token`'s SHA-256 hex digest.
_DIGEST_WIDTH = 64
#: A role's stored value, as :class:`~eifi1_server_kit.auth.service.AccountPolicy` names it
#: (``"ADMIN"``, ``"MEMBER"``, ``"REVIEWER"``).
_ROLE_WIDTH = 16
#: A BCP 47 tag the apps ship (``"de-CH"``, ``"zh"``), with room for a script subtag.
_LOCALE_WIDTH = 16
#: A ``sid``: a UUID's 36 characters or ``token_urlsafe(32)``'s 43 both fit.
_SID_WIDTH = 64

#: JSON, stored as JSONB on PostgreSQL (indexable, compared by value) and as JSON anywhere
#: else, so the same model runs the app's tests on SQLite.
_JSON = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator[datetime]):
    """An instant, written in UTC and read back aware on every database.

    PostgreSQL's ``timestamptz`` keeps the zone itself; SQLite keeps none, and SQLAlchemy's
    SQLite type would drop an offset without converting the value. So the value goes in as
    UTC, and a naive one read back is UTC by construction: a store answers the same instant
    on the app's database and in its SQLite tests, and asyncpg never meets a naive value for
    an aware column. A naive value written is taken as UTC, as the kit's clock means it.

    Public since 0.30, for an app's own instant columns; ``_UtcDateTime``, its 0.29 name,
    still answers. The database sees ``DateTime(timezone=True)`` and nothing else, which is
    what a migration writes (:func:`alembic_render_item`)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


#: 0.29's name for :class:`UtcDateTime`, kept so nothing that imported it breaks.
_UtcDateTime = UtcDateTime


def _aware() -> UtcDateTime:
    """Every instant is stored with its zone, as :class:`UtcDateTime` describes."""
    return UtcDateTime()


class _PointsAtAccounts:
    """The account table's name for the foreign keys of the mixins below."""

    __accounts_table__: ClassVar[str] = ACCOUNTS_TABLE

    @classmethod
    def _account_id(cls) -> str:
        return f"{cls.__accounts_table__}.id"


class AccountColumns:
    """The account row (§12.3's ``AccountRow``): the address, the password's hash, the name,
    the language, the one role, the states an admin or the person sets, the appearance, the
    zone and when the account was made.

    The review scope (``translation_review_locales``, ``translation_review_areas``) is a
    reviewer's grant (:func:`~eifi1_server_kit.translation_review.review_grant`): ``None`` is
    no grant for the locales and every area for the areas, as
    :func:`~eifi1_server_kit.translation_review.normalize_areas` answers them."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Stored as :func:`~eifi1_server_kit.auth.normalise_email` answers it, so the unique
    #: index is the one-account-per-address rule.
    email: Mapped[str] = mapped_column(String(EMAIL_WIDTH), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH), nullable=False)
    #: Empty, not ``None``, for a person who gave one name (§3.2).
    last_name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH), default="", nullable=False)
    #: No default: the app's own fallback language, set at sign-up.
    locale: Mapped[str] = mapped_column(String(_LOCALE_WIDTH), nullable=False)
    #: No default either: which role a new account gets is the app's
    #: (:func:`~eifi1_server_kit.auth.registration_decision`).
    role: Mapped[str] = mapped_column(String(_ROLE_WIDTH), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    #: An admin's "must choose a new password" (§6.6), until the person does.
    password_change_required_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    #: When the erasure is due, for an app whose deletion runs on a schedule; ``None`` for an
    #: operator's erasure.
    deletion_scheduled_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    #: One of :data:`~eifi1_server_kit.auth.schemas.TEXT_SIZES`; ``None`` is the device's.
    text_size: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: One of :data:`~eifi1_server_kit.auth.schemas.CONTRAST_MODES`; ``None`` is the device's.
    contrast: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: The zone the person's device last reported (an IANA name).
    device_timezone: Mapped[str | None] = mapped_column(String(MAX_ZONE_LENGTH), nullable=True)
    translation_review_locales: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    translation_review_areas: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)


class DemoAccountColumn:
    """``is_demo``, for an app with a demo session: the demo's accounts are never people
    (``AccountRow``'s optional attribute; without the column it reads ``False``)."""

    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)


class SessionCutoffColumn:
    """``sessions_invalid_before``, the ``iat`` cut-off of an app that signed out by it before
    server-kit 0.27's session rows. A new app does without: it ends the rows."""

    sessions_invalid_before: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)


class SessionColumns(_PointsAtAccounts):
    """One signed-in session (server-kit 0.27). The token's ``sid`` claim names the row, and
    every request checks it is active and not expired, so ending the row ends the session
    whatever tokens are still out."""

    sid: Mapped[str] = mapped_column(String(_SID_WIDTH), primary_key=True)

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="CASCADE"), index=True, nullable=False)

    #: What the person sees in the sessions list ("Firefox on Linux").
    device: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The caller's address at sign-in, as the edge-checked ``client_ip`` reads it (45: an
    #: IPv6 address with an IPv4 tail).
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    started_at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)
    last_active: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)
    shared_device: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: :func:`~eifi1_server_kit.auth.session_expires_at`, rewritten at every refresh; indexed
    #: so the expired rows can be swept.
    expires_at: Mapped[datetime] = mapped_column(_aware(), index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class OneTimeTokenColumns(_PointsAtAccounts):
    """A reset, verification or address-change link: the digest, never the token
    (:func:`~eifi1_server_kit.auth.mint`). One row per account and kind, so a new link
    replaces the old and only the newest works."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: A :class:`~eifi1_server_kit.auth.tokens.OneTimeTokenKind` value.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="CASCADE"), index=True, nullable=False)

    token_hash: Mapped[str] = mapped_column(String(_DIGEST_WIDTH), unique=True, index=True, nullable=False)
    #: The address the link is for: the account's, pinned when it was sent (a reset or a
    #: verification), or the NEW one for an address change, which the link proves.
    email: Mapped[str] = mapped_column(String(EMAIL_WIDTH), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_aware(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)

    @classmethod
    def table_args(cls) -> tuple[Any, ...]:
        """The one-row-per-account-and-kind rule, a fresh constraint per table."""
        return (UniqueConstraint("user_id", "kind"),)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return cls.table_args()


class InvitationColumns(_PointsAtAccounts):
    """An invitation: an address, the role it gives, the app's ``scope``, and its token's
    digest. Revoked or expired, it stays on the admin's list (§4.4); accepted, it leaves it."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(EMAIL_WIDTH), index=True, nullable=False)
    #: ``None``: the app's default role for a new account.
    role: Mapped[str | None] = mapped_column(String(_ROLE_WIDTH), nullable=True)
    #: What the invitation brings the person into: the app's, opaque to the kit
    #: (``{"company_id": 3}``, ``{"team_id": 7, "team_role": "manager"}``), ``None`` for none.
    scope: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    #: The digest of the token in the link; "resend" mints a new one and replaces it.
    #: ``None`` (with ``expires_at``) for a row that was never sent: an allow-list entry
    #: carried over. Unique all the same; PostgreSQL and SQLite let any number be NULL.
    token_hash: Mapped[str | None] = mapped_column(String(_DIGEST_WIDTH), unique=True, index=True, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)

    @declared_attr
    def accepted_by_user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)

    @declared_attr
    def invited_by_user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), nullable=True)

    #: The language the invitation mail went out in, and the sign-up form starts in.
    locale: Mapped[str] = mapped_column(String(_LOCALE_WIDTH), nullable=False)
    #: The admin's own note, never shown to the person invited.
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)


class PasskeyColumns(_PointsAtAccounts):
    """A WebAuthn credential: the authenticator's id and public key, its signature counter,
    how it can be reached, and the person's name for it."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="CASCADE"), index=True, nullable=False)

    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True, index=True, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: BigInteger: the counter is an unsigned 32-bit value, past a signed INTEGER's range.
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: The authenticator's transports, comma-separated as the browser reported them
    #: (``"internal,hybrid"``), handed back at sign-in; ``None`` when it reported none.
    transports: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LENGTH), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(_aware(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, nullable=False)


class SpentChallengeColumns:
    """A WebAuthn challenge already answered, by its digest, until it would have expired
    anyway. The primary key is the one-use rule: a second insert of the same challenge fails,
    in every app (§12.5)."""

    challenge_hash: Mapped[str] = mapped_column(String(_DIGEST_WIDTH), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(_aware(), index=True, nullable=False)


class AdminActionColumns(_PointsAtAccounts):
    """One row of the activity log: :func:`~eifi1_server_kit.user_admin.admin_action_record`'s
    fields, so the record goes in as it is. The people stay named by ``target_email`` after an
    erasure deletes their account (the keys are ``SET NULL``)."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(_aware(), default=utc_now, index=True, nullable=False)

    @declared_attr
    def actor_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), nullable=True)

    #: An :class:`~eifi1_server_kit.user_admin.AdminAction` value or the app's own action.
    action: Mapped[str] = mapped_column(String(40), nullable=False)

    @declared_attr
    def target_user_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    target_email: Mapped[str | None] = mapped_column(String(EMAIL_WIDTH), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(_JSON, default=dict, nullable=False)
    target_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_group_name: Mapped[str | None] = mapped_column(String(MAX_NAME_LENGTH), nullable=True)
    #: The admin scope the action was taken in (§12.4), ``None`` in the global one.
    scope: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)


# ── The stores ──────────────────────────────────────────────────────────────


def _assign(row: object, values: Mapping[str, Any]) -> None:
    """Set ``values`` on ``row``, refusing a name its model does not map: an unmapped
    attribute is a plain Python one, which the flush leaves out without a word."""
    mapped = class_mapper(type(row)).attrs.keys()
    unknown = sorted(set(values) - set(mapped))
    if unknown:
        raise ValueError(f"{type(row).__name__} maps no {', '.join(unknown)}")
    for name, value in values.items():
        setattr(row, name, value)


def _new[RowT](model: type[RowT], values: Mapping[str, Any]) -> RowT:
    row = model()
    _assign(row, values)
    return row


def _as_list(value: Sequence[str] | None) -> list[str] | None:
    """A review scope as the JSON column stores it: a list, whatever sequence came in."""
    return None if value is None else list(value)


def _rowcount(result: object) -> int:
    """How many rows an UPDATE, DELETE or INSERT touched (an ORM ``execute`` answers the
    cursor's result, typed as the general one)."""
    return cast(CursorResult[Any], result).rowcount


class SqlUserStore[UserT: AccountColumns]:
    """:class:`~eifi1_server_kit.auth.ports.UserStore` over a model built from
    :class:`AccountColumns`. ``count_people`` leaves out ``is_demo`` accounts where the model
    has :class:`DemoAccountColumn`; an app with a system account of its own subclasses and
    narrows :meth:`people`."""

    def __init__(self, session: AsyncSession, model: type[UserT]) -> None:
        self._session = session
        self._model = model

    async def get(self, user_id: int) -> UserT | None:
        return await self._session.get(self._model, user_id)

    async def by_email(self, email: str) -> UserT | None:
        rows = await self._session.scalars(select(self._model).where(self._model.email == email))
        return rows.one_or_none()

    async def by_emails(self, emails: Sequence[str]) -> Sequence[UserT]:
        if not emails:
            return []
        rows = await self._session.scalars(select(self._model).where(self._model.email.in_(list(emails))))
        return list(rows)

    def people(self) -> Any:
        """The ``WHERE`` clause of an account that is a person, or ``None`` for every row."""
        demo = getattr(self._model, "is_demo", None)
        return None if demo is None else demo.is_(False)

    async def count_people(self) -> int:
        query = select(func.count()).select_from(self._model)
        people = self.people()
        if people is not None:
            query = query.where(people)
        return int(await self._session.scalar(query) or 0)

    async def count_active(self, *, role: str) -> int:
        query = select(func.count()).select_from(self._model).where(self._model.is_active.is_(True))
        query = query.where(self._model.role == role)
        people = self.people()
        if people is not None:
            query = query.where(people)
        return int(await self._session.scalar(query) or 0)

    async def add(self, new: NewAccount) -> UserT:
        row = _new(self._model, {field.name: getattr(new, field.name) for field in fields(new)})
        self._session.add(row)
        await self._session.flush()
        return row

    async def apply(self, row: UserT, changes: Mapping[str, Any]) -> UserT:
        unknown = sorted(set(changes) - ACCOUNT_FIELDS)
        if unknown:
            raise ValueError(f"not an account field: {', '.join(unknown)}")
        values = dict(changes)
        # The port's word: an app without the `iat` cut-off ignores it (SessionCutoffColumn).
        if "sessions_invalid_before" in values and not hasattr(type(row), "sessions_invalid_before"):
            del values["sessions_invalid_before"]
        for name in ("translation_review_locales", "translation_review_areas"):
            if name in values:
                values[name] = _as_list(values[name])
        _assign(row, values)
        await self._session.flush()
        return row

    async def delete(self, row: UserT) -> None:
        await self._session.delete(row)
        await self._session.flush()


class SqlSessionStore[SessionT: SessionColumns]:
    """:class:`~eifi1_server_kit.auth.ports.SessionStore` over a model built from
    :class:`SessionColumns`."""

    def __init__(self, session: AsyncSession, model: type[SessionT]) -> None:
        self._session = session
        self._model = model

    async def start(self, new: NewSession) -> SessionT:
        row = _new(self._model, {field.name: getattr(new, field.name) for field in fields(new)})
        row.last_active = new.started_at
        row.is_active = True
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, sid: str) -> SessionT | None:
        return await self._session.get(self._model, sid)

    async def touch(self, session: SessionT, *, last_active: datetime, expires_at: datetime) -> None:
        session.last_active = last_active
        session.expires_at = expires_at
        await self._session.flush()

    async def end(self, session: SessionT) -> None:
        session.is_active = False
        await self._session.flush()

    async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
        model = self._model
        query = update(model).where(model.user_id == user_id, model.is_active.is_(True))
        if keep is not None:
            query = query.where(model.sid != keep)
        # "fetch": the rows this session already holds read as ended too, at once.
        result = await self._session.execute(
            query.values(is_active=False).execution_options(synchronize_session="fetch")
        )
        return _rowcount(result)

    async def live(self, user_id: int) -> Sequence[SessionT]:
        model = self._model
        rows = await self._session.scalars(
            select(model)
            .where(model.user_id == user_id, model.is_active.is_(True))
            .order_by(model.last_active.desc(), model.sid.desc())
        )
        return list(rows)

    async def prune(self, *, before: datetime) -> int:
        """Delete the sessions that ended or expired before ``before`` and answer how many:
        the inactive rows whose ``last_active`` is before it (an ended row keeps no time of
        its ending, so its last activity stands for it), and every row whose ``expires_at``
        is before it, ended or not. A row that is gone answers as an ended one: a token
        naming it is ``session_ended``.

        Not on the port (:class:`~eifi1_server_kit.auth.ports.SessionStore`), and run by no
        route: a scheduled job's, through :func:`prune_account_rows`, which says why. Flushes
        first, commits nothing; ``before`` must have passed."""
        model = self._model
        horizon = _horizon(before)
        ended = model.is_active.is_(False) & (model.last_active < horizon)
        return await _prune(self._session, delete(model).where(ended | (model.expires_at < horizon)))


class SqlOneTimeTokenStore[TokenT: OneTimeTokenColumns]:
    """:class:`~eifi1_server_kit.auth.ports.OneTimeTokenStore` over a model built from
    :class:`OneTimeTokenColumns`: one row per account and kind, the unique constraint
    holding the rule even against a writer that skips the store."""

    def __init__(self, session: AsyncSession, model: type[TokenT]) -> None:
        self._session = session
        self._model = model

    async def put(
        self, kind: OneTimeTokenKind, *, user_id: int, token_hash: str, email: str, expires_at: datetime
    ) -> TokenT:
        await self.drop(kind, user_id)
        row = _new(
            self._model,
            {
                "kind": kind.value,
                "user_id": user_id,
                "token_hash": token_hash,
                "email": email,
                "expires_at": expires_at,
            },
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def take(self, kind: OneTimeTokenKind, token_hash: str) -> TokenT | None:
        model = self._model
        rows = await self._session.scalars(
            select(model).where(model.kind == kind.value, model.token_hash == token_hash)
        )
        return rows.one_or_none()

    async def pending(self, kind: OneTimeTokenKind, user_id: int) -> TokenT | None:
        model = self._model
        rows = await self._session.scalars(select(model).where(model.kind == kind.value, model.user_id == user_id))
        return rows.one_or_none()

    async def drop(self, kind: OneTimeTokenKind, user_id: int) -> None:
        model = self._model
        await self._session.execute(
            delete(model)
            .where(model.kind == kind.value, model.user_id == user_id)
            .execution_options(synchronize_session="fetch")
        )

    async def prune(self, *, before: datetime) -> int:
        """Delete the links of every kind that expired before ``before`` and answer how many.
        An expired link only ever answers ``token_expired``; gone, it answers
        ``token_invalid``, and the person asks for a new one either way. Not on the port; see
        :func:`prune_account_rows`. Flushes first, commits nothing; ``before`` must have
        passed."""
        model = self._model
        return await _prune(self._session, delete(model).where(model.expires_at < _horizon(before)))


class SqlInvitationStore[InvT: InvitationColumns]:
    """:class:`~eifi1_server_kit.auth.ports.InvitationStore` over a model built from
    :class:`InvitationColumns`. ``revoke`` keeps the row (stamped); an app that deletes a
    withdrawn invitation instead subclasses it."""

    def __init__(self, session: AsyncSession, model: type[InvT]) -> None:
        self._session = session
        self._model = model

    async def get(self, invitation_id: int) -> InvT | None:
        return await self._session.get(self._model, invitation_id)

    async def by_token_hash(self, token_hash: str) -> InvT | None:
        rows = await self._session.scalars(select(self._model).where(self._model.token_hash == token_hash))
        return rows.one_or_none()

    async def open_for(self, email: str) -> Sequence[InvT]:
        model = self._model
        rows = await self._session.scalars(
            select(model)
            .where(model.email == email, model.accepted_at.is_(None), model.revoked_at.is_(None))
            .order_by(model.created_at.desc(), model.id.desc())
        )
        return list(rows)

    async def listing(self) -> Sequence[InvT]:
        model = self._model
        rows = await self._session.scalars(
            select(model).where(model.accepted_at.is_(None)).order_by(model.created_at.desc(), model.id.desc())
        )
        return list(rows)

    async def prune(self, *, before: datetime) -> int:
        """Delete the invitations that were never accepted and expired or were revoked before
        ``before``, and answer how many. They leave the admin's list (which shows expired and
        revoked ones, :meth:`listing`): one pruned cannot be re-sent, a new one is. An
        accepted invitation stays, whatever its dates (it records how its account came in),
        and so does an allow-list entry carried over, which has no expiry. Not on the port;
        see :func:`prune_account_rows`. Flushes first, commits nothing; ``before`` must have
        passed."""
        model = self._model
        horizon = _horizon(before)
        # A NULL date compares as unknown, so a row is gone only by a date it has.
        gone = (model.expires_at < horizon) | (model.revoked_at < horizon)
        return await _prune(self._session, delete(model).where(model.accepted_at.is_(None), gone))

    async def add(self, new: NewInvitation) -> InvT:
        values = {field.name: getattr(new, field.name) for field in fields(new)}
        values["scope"] = None if new.scope is None else dict(new.scope)
        row = _new(self._model, values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def remint(self, invitation: InvT, *, token_hash: str, expires_at: datetime) -> InvT:
        invitation.token_hash = token_hash
        invitation.expires_at = expires_at
        await self._session.flush()
        return invitation

    async def accept(self, invitation: InvT, *, user_id: int, at: datetime) -> None:
        invitation.accepted_at = at
        invitation.accepted_by_user_id = user_id
        await self._session.flush()

    async def revoke(self, invitation: InvT, *, at: datetime) -> None:
        invitation.revoked_at = at
        await self._session.flush()


def _insert_once(model: type[object], values: Mapping[str, Any], dialect: str) -> Insert:
    """An INSERT that does nothing when the key exists: the one-use rule in ONE statement, so
    two requests answering the same challenge at once cannot both pass (a read-then-insert
    could), and the loser's transaction is not left failed (an IntegrityError would)."""
    if dialect == "postgresql":
        return postgresql_insert(model).values(dict(values)).on_conflict_do_nothing()
    if dialect == "sqlite":
        return sqlite_insert(model).values(dict(values)).on_conflict_do_nothing()
    raise NotImplementedError(f"spend_challenge knows PostgreSQL and SQLite, not {dialect}")


class SqlPasskeyStore[PasskeyT: PasskeyColumns, ChallengeT: SpentChallengeColumns]:
    """:class:`~eifi1_server_kit.auth.ports.PasskeyStore` over two models, one built from
    :class:`PasskeyColumns` and one from :class:`SpentChallengeColumns`."""

    def __init__(self, session: AsyncSession, model: type[PasskeyT], challenges: type[ChallengeT]) -> None:
        self._session = session
        self._model = model
        self._challenges = challenges

    async def for_user(self, user_id: int) -> Sequence[PasskeyT]:
        model = self._model
        rows = await self._session.scalars(
            select(model).where(model.user_id == user_id).order_by(model.created_at, model.id)
        )
        return list(rows)

    async def by_credential_id(self, credential_id: bytes) -> PasskeyT | None:
        rows = await self._session.scalars(select(self._model).where(self._model.credential_id == credential_id))
        return rows.one_or_none()

    async def add(self, new: NewPasskey) -> PasskeyT:
        row = _new(self._model, {field.name: getattr(new, field.name) for field in fields(new)})
        self._session.add(row)
        await self._session.flush()
        return row

    async def used(self, passkey: PasskeyT, *, sign_count: int, at: datetime) -> None:
        passkey.sign_count = sign_count
        passkey.last_used_at = at
        await self._session.flush()

    async def rename(self, passkey: PasskeyT, name: str) -> None:
        passkey.name = name
        await self._session.flush()

    async def remove(self, passkey: PasskeyT) -> None:
        await self._session.delete(passkey)
        await self._session.flush()

    async def spend_challenge(self, challenge_hash: str, *, expires_at: datetime) -> bool:
        dialect = self._session.get_bind().dialect.name
        statement = _insert_once(
            self._challenges, {"challenge_hash": challenge_hash, "expires_at": expires_at}, dialect
        )
        return _rowcount(await self._session.execute(statement)) == 1

    async def prune(self, *, before: datetime) -> int:
        """Delete the spent challenges that expired before ``before`` and answer how many. A
        challenge past its expiry is refused by its own token, so its row guards nothing any
        more; one that has not expired yet is what refuses a replay, which is why ``before``
        must have passed. The passkeys themselves are never pruned. Not on the port (whose
        ``spend_challenge`` may sweep on its own; this one does not); see
        :func:`prune_account_rows`. Flushes first, commits nothing."""
        challenges = self._challenges
        return await _prune(self._session, delete(challenges).where(challenges.expires_at < _horizon(before)))


class SqlAdminActionLog[ActionT: AdminActionColumns]:
    """:class:`~eifi1_server_kit.auth.ports.AdminActionLog` over a model built from
    :class:`AdminActionColumns`. ``add`` takes
    :func:`~eifi1_server_kit.user_admin.admin_action_record`'s mapping as it is, and refuses a
    key the model does not map (a ``company_id`` on a log without one)."""

    def __init__(self, session: AsyncSession, model: type[ActionT]) -> None:
        self._session = session
        self._model = model

    async def add(self, record: Mapping[str, Any]) -> ActionT:
        row = _new(self._model, record)
        self._session.add(row)
        await self._session.flush()
        return row

    async def page(self, *, target_user_id: int | None, limit: int, offset: int) -> tuple[Sequence[ActionT], int]:
        model = self._model
        query = select(model)
        if target_user_id is not None:
            query = query.where(model.target_user_id == target_user_id)
        total = await self._session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self._session.scalars(query.order_by(model.at.desc(), model.id.desc()).limit(limit).offset(offset))
        return list(rows), int(total or 0)

    async def scrub_email(self, email: str) -> None:
        model = self._model
        await self._session.execute(
            update(model)
            .where(model.target_email == email)
            .values(target_email=None)
            .execution_options(synchronize_session="fetch")
        )


# ── After the commit ────────────────────────────────────────────────────────

#: Where the queued work waits: the sync session's ``info``, so every
#: :class:`SqlAfterCommit` on one session shares one queue and one pair of listeners.
_QUEUE = "eifi1_server_kit.auth.after_commit"


def _run_committed(sync: Session) -> None:
    if sync.in_nested_transaction():
        # A savepoint released: the outer transaction can still roll back.
        return
    for work in sync.info.pop(_QUEUE, []):
        try:
            await_only(work())
        except Exception:
            # The commit has landed: a mail or a purge must not turn it into a failure.
            logger.exception("Work queued for after the commit failed")


def _drop_uncommitted(sync: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        # The outermost transaction ended. After a commit the queue is already empty;
        # after a rollback, or a close, its work would tell of nothing that happened.
        sync.info.pop(_QUEUE, None)


class SqlAfterCommit:
    """:class:`~eifi1_server_kit.auth.ports.AfterCommit` on an ``AsyncSession``
    (Kurvenschmiede's ``send_after_commit``, keksdose's object purge).

    The work waits on the session's ``info``, and an ``after_commit`` listener runs it from
    inside the commit's greenlet, so every writer that commits (the request's unit of work,
    a kit router under it, :meth:`commit_now`) runs it with no step of its own to forget. A
    rollback, or a session closed uncommitted, drops it; a savepoint's release is no commit
    and runs nothing. Each item runs on its own: one that raises is logged and the rest run.

    The work runs after the commit, when the session must not be used for SQL: a mail is
    rendered whole before it is queued, a purge knows its keys."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def later(self, work: Callable[[], Awaitable[None]]) -> None:
        sync = self._session.sync_session
        if not sync.in_transaction():
            # Queued before any SQL ran: without a transaction a rollback would end nothing,
            # and the work would wait for the NEXT commit. Beginning one connects nothing.
            sync.begin()
        sync.info.setdefault(_QUEUE, []).append(work)
        # Module functions, not closures, so `event.contains` knows them and a session that
        # queues several items holds one pair of listeners.
        if not event.contains(sync, "after_commit", _run_committed):
            event.listen(sync, "after_commit", _run_committed)
            event.listen(sync, "after_transaction_end", _drop_uncommitted)

    async def commit_now(self) -> None:
        await self._session.commit()


# ── Migrations ──────────────────────────────────────────────────────────────


def alembic_render_item(type_: str, obj: object, autogen_context: Any) -> str | Literal[False]:
    """Alembic's ``render_item`` hook for an app's ``env.py``: :class:`UtcDateTime` is written
    as ``sa.DateTime(timezone=True)``, everything else is left to Alembic (``False``)::

        from eifi1_server_kit.auth.sql import alembic_render_item

        context.configure(connection=connection, target_metadata=Base.metadata,
                          render_item=alembic_render_item)

    **Why.** Without it, ``alembic revision --autogenerate`` writes the kit's type by its
    module path (``eifi1_server_kit.auth.sql._UtcDateTime()`` in 0.29) and imports nothing
    for it, so the revision fails when it runs; and a revision that imported it would tie a
    file that must describe the schema as it WAS to the kit's code as it is today. The
    database never sees the type's work (the UTC on the way in and out is Python's): its
    column is ``timestamptz``, so the plain type is the whole truth, and Alembic's comparison
    of the models with the database finds nothing to change.

    An app with a ``render_item`` of its own calls this one first and goes on when it answers
    ``False``. Alembic is the app's: the kit imports nothing of it."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        prefix = autogen_context.opts.get("sqlalchemy_module_prefix", "sa.") or ""
        return f"{prefix}DateTime(timezone=True)"
    return False


# ── Pruning ─────────────────────────────────────────────────────────────────


def _horizon(before: datetime) -> datetime:
    """``before`` as an aware instant (a naive one is UTC, as :class:`UtcDateTime` takes it),
    refused when it has not passed: a row that expires later is still in use (a session, a
    link, a challenge that refuses a replay), so a horizon ahead of the clock is a slip of
    the sign, never a retention."""
    horizon = before.replace(tzinfo=UTC) if before.tzinfo is None else before
    if horizon > utc_now():
        raise ValueError(f"prune before an instant that has passed: {horizon.isoformat()} has not passed yet")
    return horizon


async def _prune(session: AsyncSession, statement: Delete) -> int:
    """Flush, so a change this unit of work made counts even without autoflush, then delete,
    taking the rows out of the session too ("fetch"); answer how many went."""
    await session.flush()
    return _rowcount(await session.execute(statement.execution_options(synchronize_session="fetch")))


@dataclass(frozen=True, slots=True)
class AccountPrune:
    """How many rows :func:`prune_account_rows` deleted, per table; 0 for a store it was not
    given."""

    #: Sessions that ended or expired (:meth:`SqlSessionStore.prune`).
    sessions: int = 0
    #: Spent passkey challenges past their expiry (:meth:`SqlPasskeyStore.prune`).
    challenges: int = 0
    #: Reset, verification and address-change links past theirs (:meth:`SqlOneTimeTokenStore.prune`).
    links: int = 0
    #: Invitations never accepted, expired or revoked (:meth:`SqlInvitationStore.prune`).
    invitations: int = 0

    @property
    def total(self) -> int:
        """Every row deleted."""
        return self.sessions + self.challenges + self.links + self.invitations


async def prune_account_rows(
    *,
    before: datetime,
    sessions: SqlSessionStore[Any] | None = None,
    passkeys: SqlPasskeyStore[Any, Any] | None = None,
    links: SqlOneTimeTokenStore[Any] | None = None,
    invitations: SqlInvitationStore[Any] | None = None,
) -> AccountPrune:
    """Delete, through each store given, the rows that were done with before ``before``, and
    answer how many went: an app's scheduled job. Each store's ``prune`` says which rows.

    **Why.** The account tables keep rows that no request reads again, and nothing in the
    kit deletes them: the ports have no prune, and a route does the person's work, not the
    housekeeping. Left alone they stay **for ever**, and some carry personal data:

    * an ended or expired **session** keeps the address it was signed in from
      (``ip_address``) and the browser's User-Agent description (``device``). A privacy
      policy has to say how long those are kept (storage limitation, GDPR Art. 5(1)(e)), and
      "indefinitely" is no answer it can give;
    * an expired **link** keeps an address (an address change's, the NEW one), and an
      invitation never taken up keeps the invitee's address and the admin's note;
    * **spent challenges** carry nothing personal, but grow by one row per passkey ceremony.

    ``before`` is the app's retention, the one its privacy policy names
    (``utc_now() - timedelta(days=30)``), and must have passed: a horizon ahead of the clock
    is refused before anything is deleted. An app that keeps one table longer than another
    calls the stores' ``prune`` itself. Accounts, passkeys, the admin log and accepted
    invitations are not this job's.

    Every store flushes and none commits: the caller's transaction is the job's, e.g. a
    scheduler's route behind :func:`~eifi1_server_kit.jobs.require_jobs_token` on the
    request's unit of work, or a command::

        async with database.sessions() as session, session.begin():
            pruned = await prune_account_rows(
                before=utc_now() - timedelta(days=30),
                sessions=SqlSessionStore(session, UserSession),
                passkeys=SqlPasskeyStore(session, Passkey, SpentChallenge),
                links=SqlOneTimeTokenStore(session, OneTimeToken),
                invitations=SqlInvitationStore(session, Invitation),
            )
        logger.info("Pruned %d account rows: %s", pruned.total, pruned)
    """
    horizon = _horizon(before)
    return AccountPrune(
        sessions=0 if sessions is None else await sessions.prune(before=horizon),
        challenges=0 if passkeys is None else await passkeys.prune(before=horizon),
        links=0 if links is None else await links.prune(before=horizon),
        invitations=0 if invitations is None else await invitations.prune(before=horizon),
    )
