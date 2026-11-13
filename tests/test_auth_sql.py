"""The ``sql`` extra (auth-harmonization §12.3, decision C): the account tables as mixins and
the ports' stores over them, on SQLite in memory through aiosqlite, foreign keys on, so a
cascade or a ``SET NULL`` is the database's and not the test's. Two schemas: the common one
(``users``) and one that renames the account table and carries both optional columns."""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from sqlalchemy import Index, String, Table, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from eifi1_server_kit.auth import sql
from eifi1_server_kit.auth.ports import NewAccount, NewInvitation, NewPasskey, NewSession
from eifi1_server_kit.auth.sql import (
    AccountColumns,
    AdminActionColumns,
    DemoAccountColumn,
    InvitationColumns,
    OneTimeTokenColumns,
    PasskeyColumns,
    SessionColumns,
    SessionCutoffColumn,
    SpentChallengeColumns,
    SqlAdminActionLog,
    SqlAfterCommit,
    SqlInvitationStore,
    SqlOneTimeTokenStore,
    SqlPasskeyStore,
    SqlSessionStore,
    SqlUserStore,
)
from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.user_admin import AdminAction, admin_action_record

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class User(AccountColumns, Base):
    __tablename__ = "users"


class UserSession(SessionColumns, Base):
    __tablename__ = "user_sessions"


class OneTimeToken(OneTimeTokenColumns, Base):
    __tablename__ = "one_time_tokens"


class Invitation(InvitationColumns, Base):
    __tablename__ = "invitations"


class Passkey(PasskeyColumns, Base):
    __tablename__ = "passkeys"


class SpentChallenge(SpentChallengeColumns, Base):
    __tablename__ = "spent_challenges"


class AdminActionRecord(AdminActionColumns, Base):
    __tablename__ = "admin_actions"


class OtherBase(DeclarativeBase):
    pass


class Account(AccountColumns, DemoAccountColumn, SessionCutoffColumn, OtherBase):
    """An app with a demo and the `iat` cut-off, whose account table is not `users`."""

    __tablename__ = "accounts"
    nickname: Mapped[str | None] = mapped_column(String(40))


class AccountSession(SessionColumns, OtherBase):
    __tablename__ = "account_sessions"
    __accounts_table__ = "accounts"


class AccountLink(OneTimeTokenColumns, OtherBase):
    """Table arguments of its own beside the mixin's rule."""

    __tablename__ = "account_links"
    __accounts_table__ = "accounts"
    __table_args__ = (*OneTimeTokenColumns.table_args(), Index("ix_account_links_expiry", "expires_at"))


def _table(model: type[Base] | type[OtherBase]) -> Table:
    return cast(Table, model.__table__)


def _ddl(model: type[Base] | type[OtherBase]) -> str:
    """The CREATE TABLE PostgreSQL would run (SQLAlchemy's compile is untyped)."""
    return str(CreateTable(_table(model)).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def _foreign_keys_on(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def on(connection: Any, record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    _foreign_keys_on(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(OtherBase.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


def _account(email: str = "ada@example.org", **extra: Any) -> NewAccount:
    return NewAccount(
        email=email, password_hash="hash", first_name="Ada", last_name="Lovelace", locale="en", role="member", **extra
    )


async def _user(session: AsyncSession, email: str = "ada@example.org") -> User:
    return await SqlUserStore(session, User).add(_account(email))


# ── The tables ──────────────────────────────────────────────────────────────


def test_the_module_names_its_extra_without_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import sqlalchemy`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.auth.sql", raising=False)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(ImportError, match=r"install the kit with its sql extra, eifi1-server-kit\[sql\]"):
        importlib.import_module("eifi1_server_kit.auth.sql")


def test_on_postgresql_the_tables_store_jsonb_and_instants_with_their_zone() -> None:
    ddl = _ddl(User)
    assert "translation_review_locales JSONB" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "email VARCHAR(320) NOT NULL" in ddl


def test_a_link_is_one_per_account_and_kind_and_the_sessions_point_at_the_accounts() -> None:
    links = _ddl(OneTimeToken)
    assert "UNIQUE (user_id, kind)" in links
    (session_key,) = UserSession.__table__.c.user_id.foreign_keys
    assert session_key.target_fullname == "users.id"
    assert session_key.ondelete == "CASCADE"


def test_a_renamed_account_table_moves_every_foreign_key_and_keeps_the_app_s_own_table_args() -> None:
    (session_key,) = AccountSession.__table__.c.user_id.foreign_keys
    assert session_key.target_fullname == "accounts.id"
    (link_key,) = AccountLink.__table__.c.user_id.foreign_keys
    assert link_key.target_fullname == "accounts.id"
    assert {index.name for index in _table(AccountLink).indexes} >= {"ix_account_links_expiry"}
    ddl = _ddl(AccountLink)
    assert "UNIQUE (user_id, kind)" in ddl


def test_the_admin_log_names_the_people_after_their_accounts_are_gone() -> None:
    keys = {key.parent.name: key.ondelete for key in AdminActionRecord.__table__.foreign_keys}
    assert keys == {"actor_id": "SET NULL", "target_user_id": "SET NULL"}


# ── Accounts ────────────────────────────────────────────────────────────────


async def test_an_account_is_found_by_id_and_by_its_exact_address(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    ada = await store.add(_account(last_login_at=NOW))
    assert ada.id is not None
    assert ada.created_at is not None
    assert ada.is_active is True
    assert await store.get(ada.id) is ada
    assert await store.by_email("ada@example.org") is ada
    assert await store.by_email("ADA@example.org") is None
    assert await store.get(ada.id + 1) is None


async def test_the_address_tag_s_variants_are_looked_up_together(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    ada = await store.add(_account("ada@example.org"))
    tagged = await store.add(_account("ada+app@example.org"))
    found = await store.by_emails(["ada@example.org", "ada+app@example.org", "nobody@example.org"])
    assert {row.id for row in found} == {ada.id, tagged.id}
    assert await store.by_emails([]) == []


async def test_one_account_per_address(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    await store.add(_account())
    with pytest.raises(IntegrityError):
        await store.add(_account())


async def test_every_account_is_a_person_where_there_is_no_demo(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    await store.add(_account("a@example.org"))
    await store.add(_account("b@example.org"))
    assert await store.count_people() == 2


async def test_demo_accounts_are_not_people(session: AsyncSession) -> None:
    store = SqlUserStore(session, Account)
    await store.add(_account("a@example.org"))
    demo = await store.add(_account("demo@example.org"))
    demo.is_demo = True
    await session.flush()
    assert await store.count_people() == 1


async def test_apply_writes_the_named_fields_and_a_review_scope_as_a_list(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    ada = await store.add(_account())
    await store.apply(ada, {"role": "reviewer", "translation_review_locales": ("fr", "it"), "text_size": "large"})
    session.expunge_all()
    again = await store.get(ada.id)
    assert again is not None
    assert (again.role, again.translation_review_locales, again.text_size) == ("reviewer", ["fr", "it"], "large")
    assert again.translation_review_areas is None


async def test_apply_refuses_a_field_that_is_no_account_field(session: AsyncSession) -> None:
    store = SqlUserStore(session, User)
    ada = await store.add(_account())
    with pytest.raises(ValueError, match="not an account field: id, nickname"):
        await store.apply(ada, {"id": 9, "nickname": "x"})


async def test_the_cut_off_is_ignored_without_its_column_and_written_with_it(session: AsyncSession) -> None:
    plain = SqlUserStore(session, User)
    ada = await plain.apply(await plain.add(_account()), {"sessions_invalid_before": NOW, "is_active": False})
    assert ada.is_active is False
    assert not hasattr(ada, "sessions_invalid_before")
    cut = SqlUserStore(session, Account)
    account = await cut.apply(await cut.add(_account()), {"sessions_invalid_before": NOW})
    assert account.sessions_invalid_before == NOW


async def test_a_deleted_account_takes_its_sessions_with_it(session: AsyncSession) -> None:
    users = SqlUserStore(session, User)
    sessions = SqlSessionStore(session, UserSession)
    ada = await _user(session)
    await sessions.start(NewSession(sid="s1", user_id=ada.id, started_at=NOW, expires_at=NOW + timedelta(days=1)))
    await users.delete(ada)
    session.expunge_all()
    assert await users.get(ada.id) is None
    assert await sessions.get("s1") is None


async def test_a_value_for_an_unmapped_attribute_is_refused_not_dropped(session: AsyncSession) -> None:
    log = SqlAdminActionLog(session, AdminActionRecord)
    record = admin_action_record(
        AdminAction.DEACTIVATE, actor_id=None, target_user_id=None, target_email=None, company_id=3, now=NOW
    )
    with pytest.raises(ValueError, match="AdminActionRecord maps no company_id"):
        await log.add(record)


# ── Sessions ────────────────────────────────────────────────────────────────


async def test_a_session_starts_active_and_last_active_when_it_started(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlSessionStore(session, UserSession)
    started = await store.start(
        NewSession(
            sid="s1",
            user_id=ada.id,
            started_at=NOW,
            expires_at=NOW + timedelta(hours=4),
            shared_device=True,
            device="Firefox on Linux",
            ip_address="2001:db8::1",
        )
    )
    assert (started.is_active, started.last_active, started.shared_device) == (True, NOW, True)
    assert await store.get("s1") is started
    assert await store.get("nope") is None


async def test_a_refresh_moves_the_last_activity_and_the_end(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlSessionStore(session, UserSession)
    row = await store.start(NewSession(sid="s1", user_id=ada.id, started_at=NOW, expires_at=NOW + timedelta(hours=1)))
    later = NOW + timedelta(minutes=30)
    await store.touch(row, last_active=later, expires_at=later + timedelta(hours=1))
    session.expunge_all()
    again = await store.get("s1")
    assert again is not None
    assert (again.last_active, again.expires_at) == (later, later + timedelta(hours=1))


async def test_ending_every_session_but_the_caller_s_counts_what_it_ended(session: AsyncSession) -> None:
    ada = await _user(session)
    bob = await _user(session, "bob@example.org")
    store = SqlSessionStore(session, UserSession)
    for sid, user in (("a1", ada), ("a2", ada), ("a3", ada), ("b1", bob)):
        await store.start(NewSession(sid=sid, user_id=user.id, started_at=NOW, expires_at=NOW + timedelta(days=1)))
    a3 = await store.get("a3")
    assert a3 is not None
    await store.end(a3)
    await store.end(a3)  # ending an ended one is no error
    assert await store.end_all(ada.id, keep="a1") == 1
    assert [row.sid for row in await store.live(ada.id)] == ["a1"]
    assert await store.end_all(ada.id) == 1
    assert await store.live(ada.id) == []
    assert [row.sid for row in await store.live(bob.id)] == ["b1"]


async def test_the_live_sessions_come_latest_activity_first(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlSessionStore(session, UserSession)
    for offset, sid in ((1, "old"), (3, "new"), (2, "mid")):
        row = await store.start(NewSession(sid=sid, user_id=ada.id, started_at=NOW, expires_at=NOW + timedelta(days=1)))
        await store.touch(row, last_active=NOW + timedelta(hours=offset), expires_at=NOW + timedelta(days=1))
    assert [row.sid for row in await store.live(ada.id)] == ["new", "mid", "old"]


# ── One-time links ──────────────────────────────────────────────────────────


async def test_a_new_link_replaces_the_account_s_earlier_one_of_its_kind(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlOneTimeTokenStore(session, OneTimeToken)
    reset = OneTimeTokenKind.PASSWORD_RESET
    first = await store.put(reset, user_id=ada.id, token_hash="h1", email=ada.email, expires_at=NOW)
    assert first.kind == "password_reset"
    await store.put(OneTimeTokenKind.VERIFICATION, user_id=ada.id, token_hash="v1", email=ada.email, expires_at=NOW)
    await store.put(reset, user_id=ada.id, token_hash="h2", email=ada.email, expires_at=NOW + timedelta(hours=1))
    assert await store.take(reset, "h1") is None
    newest = await store.take(reset, "h2")
    assert newest is not None
    assert newest.expires_at == NOW + timedelta(hours=1)
    assert await store.take(OneTimeTokenKind.VERIFICATION, "h2") is None
    pending = await store.pending(reset, ada.id)
    assert pending is not None
    assert pending.token_hash == "h2"
    await store.drop(reset, ada.id)
    assert await store.pending(reset, ada.id) is None
    assert await store.pending(OneTimeTokenKind.VERIFICATION, ada.id) is not None
    await store.drop(reset, ada.id)  # dropping nothing is no error


async def test_an_address_change_s_link_holds_the_new_address(session: AsyncSession) -> None:
    ada = await _user(session)
    links = SqlOneTimeTokenStore(session, AccountLink)
    accounts = SqlUserStore(session, Account)
    account = await accounts.add(_account("ada@example.org"))
    change = OneTimeTokenKind.EMAIL_CHANGE
    await links.put(change, user_id=account.id, token_hash="c1", email="new@example.org", expires_at=NOW)
    pending = await links.pending(change, account.id)
    assert pending is not None
    assert pending.email == "new@example.org"
    assert ada.id is not None


# ── Invitations ─────────────────────────────────────────────────────────────


def _invitation(email: str, token: str | None, *, inviter: int | None, **extra: Any) -> NewInvitation:
    return NewInvitation(
        email=email,
        token_hash=token,  # type: ignore[arg-type]
        expires_at=NOW + timedelta(days=14),
        locale="de-CH",
        invited_by_user_id=inviter,
        **extra,
    )


async def test_an_invitation_keeps_its_scope_and_is_found_by_its_link(session: AsyncSession) -> None:
    admin = await _user(session, "admin@example.org")
    store = SqlInvitationStore(session, Invitation)
    sent = await store.add(
        _invitation("ada@example.org", "t1", inviter=admin.id, role="reviewer", scope={"team_id": 7}, note="hi")
    )
    session.expunge_all()
    found = await store.by_token_hash("t1")
    assert found is not None
    assert (found.id, found.role, found.scope, found.note, found.locale) == (
        sent.id,
        "reviewer",
        {"team_id": 7},
        "hi",
        "de-CH",
    )
    assert await store.get(sent.id) is found
    assert await store.get(sent.id + 1) is None
    assert await store.by_token_hash("t2") is None


async def test_an_open_invitation_is_neither_accepted_nor_revoked_but_may_have_expired(session: AsyncSession) -> None:
    admin = await _user(session, "admin@example.org")
    ada = await _user(session, "ada@example.org")
    store = SqlInvitationStore(session, Invitation)
    expired = await store.add(_invitation("ada@example.org", "e", inviter=admin.id))
    expired.expires_at = NOW - timedelta(days=1)
    accepted = await store.add(_invitation("ada@example.org", "a", inviter=admin.id))
    revoked = await store.add(_invitation("ada@example.org", "r", inviter=None))
    other = await store.add(_invitation("bob@example.org", "b", inviter=admin.id))
    await store.accept(accepted, user_id=ada.id, at=NOW)
    await store.revoke(revoked, at=NOW)
    assert [row.id for row in await store.open_for("ada@example.org")] == [expired.id]
    assert [row.id for row in await store.listing()] == [other.id, revoked.id, expired.id]
    assert (accepted.accepted_at, accepted.accepted_by_user_id, revoked.revoked_at) == (NOW, ada.id, NOW)


async def test_a_re_send_gives_the_same_invitation_a_new_link(session: AsyncSession) -> None:
    store = SqlInvitationStore(session, Invitation)
    row = await store.add(_invitation("ada@example.org", "t1", inviter=None))
    again = await store.remint(row, token_hash="t2", expires_at=NOW + timedelta(days=30))
    assert again is row
    assert await store.by_token_hash("t1") is None
    assert await store.by_token_hash("t2") is row


async def test_an_allow_list_entry_carried_over_has_no_link_and_no_expiry(session: AsyncSession) -> None:
    store = SqlInvitationStore(session, Invitation)
    first = await store.add(_invitation("a@example.org", None, inviter=None))
    second = await store.add(_invitation("b@example.org", None, inviter=None))
    assert (first.token_hash, second.token_hash) == (None, None)
    assert first.role is None
    assert first.scope is None


async def test_an_erased_inviter_leaves_the_invitation_without_a_name(session: AsyncSession) -> None:
    admin = await _user(session, "admin@example.org")
    store = SqlInvitationStore(session, Invitation)
    row = await store.add(_invitation("ada@example.org", "t1", inviter=admin.id))
    await SqlUserStore(session, User).delete(admin)
    await session.refresh(row)
    assert row.invited_by_user_id is None


# ── Passkeys ────────────────────────────────────────────────────────────────


def _passkey(user_id: int, credential: bytes, name: str = "Laptop") -> NewPasskey:
    return NewPasskey(
        user_id=user_id, credential_id=credential, public_key=b"key", sign_count=0, name=name, transports="internal"
    )


async def test_a_person_s_passkeys_come_oldest_first_and_one_is_found_by_its_credential(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlPasskeyStore(session, Passkey, SpentChallenge)
    first = await store.add(_passkey(ada.id, b"c1"))
    second = await store.add(_passkey(ada.id, b"c2", "Phone"))
    assert [row.id for row in await store.for_user(ada.id)] == [first.id, second.id]
    assert await store.by_credential_id(b"c2") is second
    assert await store.by_credential_id(b"c3") is None
    assert first.transports == "internal"


async def test_a_sign_in_a_rename_and_a_removal_are_written(session: AsyncSession) -> None:
    ada = await _user(session)
    store = SqlPasskeyStore(session, Passkey, SpentChallenge)
    row = await store.add(_passkey(ada.id, b"c1"))
    await store.used(row, sign_count=5, at=NOW)
    await store.rename(row, "Work laptop")
    session.expunge_all()
    again = await store.by_credential_id(b"c1")
    assert again is not None
    assert (again.sign_count, again.last_used_at, again.name) == (5, NOW, "Work laptop")
    await store.remove(again)
    assert await store.for_user(ada.id) == []


async def test_a_challenge_is_spent_once(session: AsyncSession) -> None:
    store = SqlPasskeyStore(session, Passkey, SpentChallenge)
    assert await store.spend_challenge("c" * 64, expires_at=NOW) is True
    assert await store.spend_challenge("c" * 64, expires_at=NOW) is False
    assert await store.spend_challenge("d" * 64, expires_at=NOW) is True
    # The refused second spend left the transaction usable.
    assert len(list(await session.scalars(select(SpentChallenge)))) == 2


def test_on_postgresql_a_challenge_is_spent_by_one_statement_that_ignores_a_conflict() -> None:
    statement = sql._insert_once(SpentChallenge, {"challenge_hash": "c", "expires_at": NOW}, "postgresql")
    assert "ON CONFLICT DO NOTHING" in str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


def test_a_database_without_an_insert_that_ignores_a_conflict_is_refused() -> None:
    with pytest.raises(NotImplementedError, match="not mysql"):
        sql._insert_once(SpentChallenge, {"challenge_hash": "c", "expires_at": NOW}, "mysql")


# ── The admin log ───────────────────────────────────────────────────────────


async def test_the_log_takes_the_kit_s_record_as_it_is_and_pages_newest_first(session: AsyncSession) -> None:
    admin = await _user(session, "admin@example.org")
    ada = await _user(session, "ada@example.org")
    log = SqlAdminActionLog(session, AdminActionRecord)
    for minutes, target in ((1, ada), (2, admin), (3, ada)):
        await log.add(
            admin_action_record(
                AdminAction.DEACTIVATE,
                actor_id=admin.id,
                target_user_id=target.id,
                target_email=target.email,
                now=NOW + timedelta(minutes=minutes),
            )
        )
    rows, total = await log.page(target_user_id=None, limit=2, offset=0)
    assert total == 3
    assert [row.at for row in rows] == [NOW + timedelta(minutes=3), NOW + timedelta(minutes=2)]
    rows, total = await log.page(target_user_id=ada.id, limit=10, offset=1)
    assert total == 2
    assert [row.at for row in rows] == [NOW + timedelta(minutes=1)]
    assert rows[0].action == "deactivate"
    assert rows[0].detail == {}


async def test_an_erasure_clears_the_address_from_the_log(session: AsyncSession) -> None:
    admin = await _user(session, "admin@example.org")
    log = SqlAdminActionLog(session, AdminActionRecord)
    row = await log.add(
        admin_action_record(
            AdminAction.DEACTIVATE, actor_id=admin.id, target_user_id=None, target_email="gone@example.org", now=NOW
        )
    )
    kept = await log.add(
        admin_action_record(
            AdminAction.DEACTIVATE, actor_id=admin.id, target_user_id=None, target_email="kept@example.org", now=NOW
        )
    )
    await log.scrub_email("gone@example.org")
    assert (row.target_email, kept.target_email) == (None, "kept@example.org")


# ── After the commit ────────────────────────────────────────────────────────


async def test_the_work_runs_after_the_commit_and_sees_what_it_committed(engine: AsyncEngine) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    seen: list[int] = []
    async with maker() as session:
        await _user(session)

        async def count() -> None:
            async with maker() as other:
                seen.append(len(list(await other.scalars(select(User)))))

        SqlAfterCommit(session).later(count)
        assert seen == []
        await session.commit()
    assert seen == [1]


async def test_a_rollback_drops_the_work(session: AsyncSession) -> None:
    ran: list[str] = []

    async def work() -> None:
        ran.append("ran")

    SqlAfterCommit(session).later(work)
    await session.rollback()
    await session.commit()
    assert ran == []


async def test_a_savepoint_released_is_no_commit(session: AsyncSession) -> None:
    ran: list[str] = []

    async def work() -> None:
        ran.append("ran")

    await session.connection()
    async with session.begin_nested():
        SqlAfterCommit(session).later(work)
    assert ran == []
    await session.commit()
    assert ran == ["ran"]


async def test_work_that_fails_is_logged_and_the_rest_runs(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[str] = []

    async def fails() -> None:
        raise RuntimeError("mail server down")

    async def works() -> None:
        ran.append("ran")

    after = SqlAfterCommit(session)
    after.later(fails)
    SqlAfterCommit(session).later(works)
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.auth.sql"):
        await after.commit_now()
    assert ran == ["ran"]
    assert "Work queued for after the commit failed" in caplog.text


async def test_one_session_holds_one_pair_of_listeners_however_much_it_queues(session: AsyncSession) -> None:
    ran: list[int] = []

    async def work() -> None:
        ran.append(1)

    for _ in range(3):
        SqlAfterCommit(session).later(work)
    await session.commit()
    assert ran == [1, 1, 1]
    await session.commit()
    assert ran == [1, 1, 1]


async def test_an_instant_comes_back_the_same_instant_in_utc_whatever_zone_it_was_written_in(
    session: AsyncSession,
) -> None:
    ada = await _user(session)
    store = SqlSessionStore(session, UserSession)
    zurich = timezone(timedelta(hours=1))
    await store.start(
        NewSession(sid="z", user_id=ada.id, started_at=NOW.astimezone(zurich), expires_at=NOW.replace(tzinfo=None))
    )
    session.expunge_all()
    row = await store.get("z")
    assert row is not None
    assert (row.started_at, row.started_at.tzinfo) == (NOW, UTC)
    assert (row.expires_at, row.expires_at.tzinfo) == (NOW, UTC)
