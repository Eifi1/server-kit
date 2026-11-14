"""The ``sql`` extra's housekeeping (0.30): the public instant type and the Alembic hook that
writes it as a plain column type, the pruning of rows nobody needs any more, and the contract
suites' roles in the policy's spelling, and the README's unit of work. On SQLite in memory, as
``test_auth_sql``; a schema of its own, so the two files share nothing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy
from alembic import op
from alembic.autogenerate import compare_metadata, produce_migrations, render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, String, create_engine, func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from eifi1_server_kit.auth import sql
from eifi1_server_kit.auth.ports import NewAccount, NewInvitation, NewSession
from eifi1_server_kit.auth.service import AccountLinks, AccountPolicy
from eifi1_server_kit.auth.sql import (
    AccountColumns,
    AccountPrune,
    AdminActionColumns,
    InvitationColumns,
    OneTimeTokenColumns,
    PasskeyColumns,
    SessionColumns,
    SpentChallengeColumns,
    SqlInvitationStore,
    SqlOneTimeTokenStore,
    SqlPasskeyStore,
    SqlRosterStore,
    SqlSessionStore,
    SqlUserStore,
    UtcDateTime,
    alembic_render_item,
    prune_account_rows,
)
from eifi1_server_kit.auth.testing import check_invitation_store, check_user_store
from eifi1_server_kit.user_admin.service import AdminPolicy
from eifi1_server_kit.user_admin.testing import check_roster_store
from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.clock import utc_now

#: A fixed instant in the past, so every horizon the tests prune at has passed.
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
#: What the tests prune before: thirty days back.
BEFORE = NOW - timedelta(days=30)


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


class StrictBase(DeclarativeBase):
    """An app whose database refuses a role it does not have, as Staunlabor's does."""


_ROLES = "role IN ('ADMIN', 'MEMBER', 'REVIEWER')"


class StrictUser(AccountColumns, StrictBase):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_ROLES, name="ck_users_role"),)


class StrictInvitation(InvitationColumns, StrictBase):
    __tablename__ = "invitations"
    __table_args__ = (CheckConstraint(f"role IS NULL OR {_ROLES}", name="ck_invitations_role"),)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


# ── The instant type, and Alembic ───────────────────────────────────────────


def test_the_instant_type_is_public_and_its_old_name_still_answers() -> None:
    assert "UtcDateTime" in sql.__all__
    assert sql._UtcDateTime is UtcDateTime
    assert isinstance(User.__table__.c.created_at.type, UtcDateTime)
    assert isinstance(UserSession.__table__.c.expires_at.type, UtcDateTime)


def test_the_alembic_hook_writes_an_instant_as_an_aware_date_time_and_leaves_the_rest() -> None:
    context = SimpleNamespace(opts={"sqlalchemy_module_prefix": "sa."})
    assert alembic_render_item("type", UtcDateTime(), context) == "sa.DateTime(timezone=True)"
    assert alembic_render_item("type", String(8), context) is False
    assert alembic_render_item("column", UserSession.__table__.c.expires_at, context) is False
    # The app's own prefix, as its env.py configured Alembic.
    bare = SimpleNamespace(opts={"sqlalchemy_module_prefix": None})
    assert alembic_render_item("type", UtcDateTime(), bare) == "DateTime(timezone=True)"


def _autogenerate(render_item: Any) -> str:
    """The upgrade Alembic's autogenerate writes for the models against an empty database,
    rendered as an app's ``env.py`` on PostgreSQL renders it (its JSONB as ``sa.Text()``)."""
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            script = produce_migrations(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert script.upgrade_ops is not None
    on_postgresql = MigrationContext.configure(dialect_name="postgresql")
    return render_python_code(script.upgrade_ops, render_item=render_item, migration_context=on_postgresql)


def test_without_the_hook_autogenerate_names_the_kit_s_module() -> None:
    """What the pilot found: a migration that imports nothing and names the kit's type."""
    assert "eifi1_server_kit.auth.sql." in _autogenerate(None)


def test_with_the_hook_autogenerate_writes_a_migration_that_runs_without_the_kit() -> None:
    code = _autogenerate(alembic_render_item)
    assert "eifi1_server_kit" not in code
    assert "sa.DateTime(timezone=True)" in code
    # What a revision file has in scope: the template's imports, and the dialect the JSON
    # columns' variant names. Nothing of the kit.
    namespace: dict[str, Any] = {"sa": sqlalchemy, "op": op, "postgresql": postgresql}
    exec("def upgrade() -> None:\n" + code, namespace)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                namespace["upgrade"]()
            assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables)
            compared = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(compared, Base.metadata) == []
    finally:
        engine.dispose()


# ── Pruning ─────────────────────────────────────────────────────────────────


async def _user(session: AsyncSession, email: str = "ada@example.org") -> User:
    new = NewAccount(
        email=email, password_hash="hash", first_name="Ada", last_name="Lovelace", locale="en", role="MEMBER"
    )
    return await SqlUserStore(session, User).add(new)


async def _session_row(
    store: SqlSessionStore[UserSession], sid: str, user_id: int, *, last_active: datetime, expires_at: datetime
) -> UserSession:
    row = await store.start(NewSession(sid=sid, user_id=user_id, started_at=last_active, expires_at=expires_at))
    await store.touch(row, last_active=last_active, expires_at=expires_at)
    return row


async def _sessions(session: AsyncSession) -> SqlSessionStore[UserSession]:
    """Five sessions about the horizon: two of them gone by it, three not."""
    ada = await _user(session)
    store = SqlSessionStore(session, UserSession)
    day = timedelta(days=1)
    # Active and unexpired, however long ago it was last used: in use.
    await _session_row(store, "live", ada.id, last_active=NOW - 60 * day, expires_at=NOW + day)
    # Ended, its last activity before the horizon: gone.
    ended = await _session_row(store, "ended-long-ago", ada.id, last_active=NOW - 40 * day, expires_at=NOW + day)
    await store.end(ended)
    # Ended, but active after the horizon: kept until it is that old.
    recent = await _session_row(store, "ended-lately", ada.id, last_active=NOW - day, expires_at=NOW + 29 * day)
    await store.end(recent)
    # Never ended, but expired before the horizon: gone.
    await _session_row(store, "expired-long-ago", ada.id, last_active=NOW - 50 * day, expires_at=NOW - 31 * day)
    # Expired after the horizon: kept.
    await _session_row(store, "expired-lately", ada.id, last_active=NOW - 20 * day, expires_at=NOW - 10 * day)
    return store


async def _sids(session: AsyncSession) -> set[str]:
    return set(await session.scalars(select(UserSession.sid)))


async def test_pruning_sessions_deletes_the_ended_and_the_expired_before_the_horizon(session: AsyncSession) -> None:
    store = await _sessions(session)
    assert await store.prune(before=BEFORE) == 2
    assert await _sids(session) == {"live", "ended-lately", "expired-lately"}
    # The rows the session held are gone from it too, not only from the table.
    assert await store.get("ended-long-ago") is None
    assert await store.prune(before=BEFORE) == 0


async def test_pruning_flushes_first_so_a_change_not_yet_written_counts(engine: AsyncEngine) -> None:
    async with async_sessionmaker(engine, autoflush=False)() as session:
        store = await _sessions(session)
        live = await store.get("live")
        assert live is not None
        live.is_active = False  # ended in this unit of work, not flushed, and no autoflush
        assert await store.prune(before=BEFORE) == 3
        assert "live" not in await _sids(session)


async def test_pruning_commits_nothing(engine: AsyncEngine) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _sessions(session)
        await session.commit()
        assert await SqlSessionStore(session, UserSession).prune(before=BEFORE) == 2
        await session.rollback()
        assert len(await _sids(session)) == 5


async def test_a_horizon_that_has_not_passed_is_refused(session: AsyncSession) -> None:
    """A row that expires after now is still in use: a session, a link, a challenge that
    refuses a replay. A horizon ahead of the clock is a slip of the sign, never a wish."""
    store = await _sessions(session)
    ahead = utc_now() + timedelta(minutes=1)
    with pytest.raises(ValueError, match="has not passed"):
        await store.prune(before=ahead)
    with pytest.raises(ValueError, match="has not passed"):
        await prune_account_rows(before=ahead, sessions=store)
    assert len(await _sids(session)) == 5


async def test_a_naive_horizon_is_taken_as_utc(session: AsyncSession) -> None:
    store = await _sessions(session)
    assert await store.prune(before=BEFORE.replace(tzinfo=None)) == 2


async def test_a_frozen_clock_ahead_of_the_real_one_admits_a_horizon_up_to_it(session: AsyncSession) -> None:
    """An app's test with a frozen clock passes it as ``now``: its horizon is the test's."""
    store = await _sessions(session)
    frozen = utc_now() + timedelta(days=400)
    with pytest.raises(ValueError, match="has not passed"):
        await store.prune(before=frozen)  # the real clock has not got there
    assert await store.prune(before=frozen - timedelta(days=30), now=frozen) == 5
    assert await store.prune(before=frozen, now=frozen.replace(tzinfo=None)) == 0  # naive: UTC


async def test_the_refusal_is_relative_to_the_clock_given(session: AsyncSession) -> None:
    store = await _sessions(session)
    earlier = NOW - timedelta(days=60)
    with pytest.raises(ValueError, match="has not passed"):
        await store.prune(before=BEFORE, now=earlier)  # passed by the real clock, not by this one
    with pytest.raises(ValueError, match="has not passed"):
        await prune_account_rows(before=BEFORE, now=earlier, sessions=store)
    assert len(await _sids(session)) == 5


async def test_the_job_hands_its_clock_to_every_store(session: AsyncSession) -> None:
    """Each store checks the horizon against the job's ``now``, not against the real clock."""
    sessions = await _sessions(session)
    passkeys = SqlPasskeyStore(session, Passkey, SpentChallenge)
    await passkeys.spend_challenge("c" * 64, expires_at=NOW)
    links = SqlOneTimeTokenStore(session, OneTimeToken)
    ada = await _user(session, "links@example.org")
    await links.put(OneTimeTokenKind.VERIFICATION, user_id=ada.id, token_hash="v", email=ada.email, expires_at=NOW)
    invitations, _ = await _invitations(session)
    frozen = utc_now() + timedelta(days=400)
    pruned = await prune_account_rows(
        before=frozen, now=frozen, sessions=sessions, passkeys=passkeys, links=links, invitations=invitations
    )
    # Everything has expired by then; the accepted invitation and the allow-list entry stay.
    assert pruned == AccountPrune(sessions=5, challenges=1, links=1, invitations=5)


async def test_pruning_challenges_deletes_the_expired_ones_which_may_then_be_spent_again(
    session: AsyncSession,
) -> None:
    store = SqlPasskeyStore(session, Passkey, SpentChallenge)
    assert await store.spend_challenge("old" * 21, expires_at=NOW - timedelta(days=2))
    assert await store.spend_challenge("new" * 21, expires_at=NOW - timedelta(minutes=5))
    assert await store.prune(before=NOW - timedelta(days=1)) == 1
    assert await store.spend_challenge("old" * 21, expires_at=NOW) is True
    assert await store.spend_challenge("new" * 21, expires_at=NOW) is False


async def test_pruning_links_deletes_the_ones_expired_before_the_horizon(session: AsyncSession) -> None:
    ada = await _user(session)
    bob = await _user(session, "bob@example.org")
    store = SqlOneTimeTokenStore(session, OneTimeToken)
    reset, verify = OneTimeTokenKind.PASSWORD_RESET, OneTimeTokenKind.VERIFICATION
    await store.put(reset, user_id=ada.id, token_hash="r1", email=ada.email, expires_at=BEFORE - timedelta(days=1))
    await store.put(verify, user_id=ada.id, token_hash="v1", email=ada.email, expires_at=BEFORE + timedelta(days=1))
    await store.put(reset, user_id=bob.id, token_hash="r2", email=bob.email, expires_at=BEFORE - timedelta(hours=1))
    assert await store.prune(before=BEFORE) == 2
    assert await store.pending(reset, ada.id) is None
    assert await store.pending(verify, ada.id) is not None
    assert await store.pending(reset, bob.id) is None


async def _invitations(session: AsyncSession) -> tuple[SqlInvitationStore[Invitation], dict[str, int]]:
    """One invitation per state, each named; three of them gone by the horizon."""
    admin = await _user(session, "admin@example.org")
    accepter = await _user(session, "accepter@example.org")
    store = SqlInvitationStore(session, Invitation)
    day = timedelta(days=1)

    async def invite(name: str, expires_at: datetime | None) -> Invitation:
        row = await store.add(
            NewInvitation(
                email=f"{name}@example.org",
                token_hash=None if expires_at is None else name,  # type: ignore[arg-type]
                expires_at=expires_at,  # type: ignore[arg-type]
                locale="de-CH",
                invited_by_user_id=admin.id,
            )
        )
        return row

    rows = {
        "open": await invite("open", NOW + 14 * day),
        "expired-long-ago": await invite("expired-long-ago", BEFORE - 10 * day),
        "expired-lately": await invite("expired-lately", BEFORE + 10 * day),
        "revoked-long-ago": await invite("revoked-long-ago", NOW + 14 * day),
        "revoked-lately": await invite("revoked-lately", BEFORE - 5 * day),
        "accepted-long-ago": await invite("accepted-long-ago", BEFORE - 10 * day),
        "allow-list": await invite("allow-list", None),
    }
    await store.revoke(rows["revoked-long-ago"], at=BEFORE - day)
    # Revoked after the horizon, but expired before it: gone all the same.
    await store.revoke(rows["revoked-lately"], at=NOW - day)
    await store.accept(rows["accepted-long-ago"], user_id=accepter.id, at=BEFORE - 20 * day)
    return store, {name: row.id for name, row in rows.items()}


async def test_pruning_invitations_deletes_the_unaccepted_ones_expired_or_revoked_before_the_horizon(
    session: AsyncSession,
) -> None:
    store, ids = await _invitations(session)
    assert await store.prune(before=BEFORE) == 3
    kept = {row.email.split("@")[0] for row in await session.scalars(select(Invitation))}
    # An accepted invitation is no prune's: it records how its account came in. An
    # allow-list entry never expires.
    assert kept == {"open", "expired-lately", "accepted-long-ago", "allow-list"}
    assert await store.get(ids["revoked-long-ago"]) is None


async def test_the_job_prunes_every_store_it_is_given_and_counts_each(session: AsyncSession) -> None:
    sessions = await _sessions(session)
    passkeys = SqlPasskeyStore(session, Passkey, SpentChallenge)
    await passkeys.spend_challenge("c" * 64, expires_at=BEFORE - timedelta(days=1))
    links = SqlOneTimeTokenStore(session, OneTimeToken)
    ada = await _user(session, "links@example.org")
    await links.put(
        OneTimeTokenKind.EMAIL_CHANGE, user_id=ada.id, token_hash="e", email="new@example.org", expires_at=BEFORE
    )
    invitations, _ = await _invitations(session)
    pruned = await prune_account_rows(
        before=BEFORE + timedelta(seconds=1),
        sessions=sessions,
        passkeys=passkeys,
        links=links,
        invitations=invitations,
    )
    assert pruned == AccountPrune(sessions=2, challenges=1, links=1, invitations=3)
    assert pruned.total == 7


async def test_the_job_without_stores_prunes_nothing(session: AsyncSession) -> None:
    await _sessions(session)
    assert await prune_account_rows(before=BEFORE) == AccountPrune()
    assert AccountPrune().total == 0
    assert await session.scalar(select(func.count()).select_from(UserSession)) == 5


# ── The contract suites' roles ──────────────────────────────────────────────


@pytest.fixture
async def strict() -> AsyncIterator[AsyncSession]:
    """A session on the schema whose role columns hold only the upper-case names."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(StrictBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _policy() -> AccountPolicy:
    return AccountPolicy(app="app", app_name="App", links=AccountLinks(base_url="https://app.example"), contact="a@b.c")


def test_the_suites_roles_default_to_the_policy_s() -> None:
    policy = _policy()
    users = signature(check_user_store).parameters
    assert (users["role"].default, users["other_role"].default) == (policy.default_role, policy.admin_role)
    assert signature(check_invitation_store).parameters["role"].default == policy.default_role


async def test_a_role_column_that_holds_only_the_policy_s_names_keeps_the_contract(strict: AsyncSession) -> None:
    await check_user_store(SqlUserStore(strict, StrictUser))
    new = NewAccount(
        email="inviter@example.org", password_hash="h", first_name="Ada", last_name="", locale="en", role="ADMIN"
    )
    inviter = await SqlUserStore(strict, StrictUser).add(new)
    store = SqlInvitationStore(strict, StrictInvitation)
    await check_invitation_store(store, inviter_id=inviter.id, user_id=inviter.id)



async def test_the_roster_suite_s_roles_are_the_policy_s_too(strict: AsyncSession) -> None:
    """0.31 (Staunlabor): the README's call, with the suite's defaults, on a role column that
    holds only the upper-case names."""
    admin = AdminPolicy()
    roster = signature(check_roster_store).parameters
    assert (roster["admin_role"].default, roster["member_role"].default, roster["reviewer_role"].default) == (
        _policy().admin_role,
        _policy().default_role,
        admin.reviewer_role,
    )
    await check_roster_store(SqlRosterStore(strict, StrictUser), SqlUserStore(strict, StrictUser))


# ── The README's unit of work ───────────────────────────────────────────────


def _app(engine: AsyncEngine, *, function_scoped: bool) -> FastAPI:
    """An app whose request session commits when the route returned, as "Adopting the account
    layer" writes it, function-scoped or with FastAPI's default scope."""
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def unit_of_work() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    dependency = Depends(unit_of_work, scope="function") if function_scoped else Depends(unit_of_work)
    app = FastAPI()

    @app.post("/accounts", status_code=201)
    async def add(session: AsyncSession = dependency) -> dict[str, int]:
        return {"id": (await _user(session)).id}

    return app


async def test_only_a_function_scoped_unit_of_work_answers_a_failed_commit(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Function-scoped, the failed commit is the answer's 500; with the default scope it comes
    after the answer, which already said 201 for an account that is not there."""

    async def refuse(_self: AsyncSession) -> None:
        raise RuntimeError("the database went away at the commit")

    monkeypatch.setattr(AsyncSession, "commit", refuse)
    for function_scoped, status in ((True, 500), (False, 201)):
        # As a server answers: what the app raises goes to its log, not into the caller.
        transport = ASGITransport(app=_app(engine, function_scoped=function_scoped), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/accounts")).status_code == status
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0
