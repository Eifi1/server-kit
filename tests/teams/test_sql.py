"""The teams' ``sql`` extra: the tables as mixins and the store over them, on SQLite in memory with
foreign keys on. The mixins are Kurvenschmiede's tables column for column — Alembic's own
comparison of its models with the kit's finds nothing to change — so the app adopts them with
no migration."""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex, CreateTable

from eifi1_server_kit.auth.ports import NewAccount, NewInvitation
from eifi1_server_kit.auth.sql import (
    AccountColumns,
    AdminActionColumns,
    InvitationColumns,
    SqlAdminActionLog,
    SqlInvitationStore,
    SqlUserStore,
)
from eifi1_server_kit.auth.testing import check_invitation_store
from eifi1_server_kit.teams import NewTeam, TeamRole, team_scope
from eifi1_server_kit.teams.service import TeamAccountHooks, join_team
from eifi1_server_kit.teams.sql import (
    SqlTeamInvitationStore,
    SqlTeamStore,
    TeamColumns,
    TeamInvitationColumns,
    TeamMemberColumns,
)
from eifi1_server_kit.teams.testing import check_team_store

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class User(AccountColumns, Base):
    __tablename__ = "users"


class Team(TeamColumns, Base):
    __tablename__ = "teams"


class TeamMember(TeamMemberColumns, Base):
    __tablename__ = "team_members"


class Invitation(InvitationColumns, TeamInvitationColumns, Base):
    __tablename__ = "invitations"


class AdminActionRecord(AdminActionColumns, Base):
    __tablename__ = "admin_actions"


class OtherBase(DeclarativeBase):
    pass


class _Renamed:
    """An app whose accounts and teams are called otherwise, said once."""

    __accounts_table__ = "accounts"
    __teams_table__ = "crews"


class Account(AccountColumns, OtherBase):
    __tablename__ = "accounts"


class Crew(TeamColumns, OtherBase):
    __tablename__ = "crews"
    #: A column of the app's own beside the kit's.
    colour: Mapped[str | None] = mapped_column(String(16))


class CrewMember(_Renamed, TeamMemberColumns, OtherBase):
    __tablename__ = "crew_members"


class CrewInvitation(_Renamed, InvitationColumns, TeamInvitationColumns, OtherBase):
    __tablename__ = "crew_invitations"


# ── Kurvenschmiede's own models, as they are (domain/models/team.py, invitation.py) ─────────


class _KsBase(DeclarativeBase):
    pass


class _KsRole(StrEnum):
    MEMBER = "member"
    MANAGER = "manager"


def _ks_role() -> Enum:
    return Enum(
        _KsRole,
        name="team_role",
        native_enum=False,
        length=16,
        validate_strings=True,
        values_callable=lambda roles: [role.value for role in roles],
    )


class _KsUser(AccountColumns, _KsBase):
    __tablename__ = "users"


class _KsTeam(_KsBase):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class _KsTeamMember(_KsBase):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[_KsRole] = mapped_column(_ks_role(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class _KsInvitation(InvitationColumns, _KsBase):
    __tablename__ = "invitations"

    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=True)
    team_role: Mapped[_KsRole | None] = mapped_column(_ks_role(), nullable=True)


def _metadata(*models: type[Any]) -> MetaData:
    """One metadata holding only these models' tables, as their own base declared them."""
    metadata = MetaData()
    for model in models:
        cast(Table, model.__table__).to_metadata(metadata)
    return metadata


def _ddl(model: type[Any]) -> str:
    table = cast(Table, model.__table__)
    indexes = sorted(table.indexes, key=lambda index: str(index.name))
    statements: list[Any] = [CreateTable(table), *(CreateIndex(index) for index in indexes)]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return "\n".join(str(statement.compile(dialect=dialect)) for statement in statements)


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


def _store(session: AsyncSession) -> SqlTeamStore[Team, TeamMember]:
    return SqlTeamStore(session, Team, TeamMember, users=User, invitations=Invitation)


def _invitations(session: AsyncSession) -> SqlTeamInvitationStore[Invitation]:
    return SqlTeamInvitationStore(session, Invitation, teams=Team)


async def _user(session: AsyncSession, email: str, *, last_name: str = "Lovelace") -> User:
    return await SqlUserStore(session, User).add(
        NewAccount(email=email, password_hash="x", first_name="Ada", last_name=last_name, locale="en", role="MEMBER")
    )


def _invitation(email: str, scope: dict[str, Any] | None) -> NewInvitation:
    return NewInvitation(
        email=email,
        token_hash=email.ljust(64, "0")[:64],
        expires_at=NOW + timedelta(days=14),
        locale="en",
        invited_by_user_id=None,
        scope=scope,
    )


# ── The tables ──────────────────────────────────────────────────────────────


def test_the_module_names_its_extra_without_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.teams.sql", raising=False)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(ImportError, match=r"install the kit with its sql extra, eifi1-server-kit\[sql\]"):
        importlib.import_module("eifi1_server_kit.teams.sql")


def test_the_tables_are_kurvenschmiedes_to_the_column() -> None:
    """The DDL PostgreSQL would run, table for table and index for index: what its migrations
    0032 and 0046 made, and the invitations' two columns."""
    # Line for line, in any order: a mixin's foreign keys come after its plain columns, which no
    # comparison of a database with its models reads.
    assert sorted(_ddl(Team).splitlines()) == sorted(_ddl(_KsTeam).splitlines())
    assert sorted(_ddl(TeamMember).replace(",", "").splitlines()) == sorted(
        _ddl(_KsTeamMember).replace(",", "").splitlines()
    )
    teams, members, invitations = _ddl(Team), _ddl(TeamMember), _ddl(Invitation)
    assert "CREATE UNIQUE INDEX ix_teams_name ON teams (name)" in teams
    assert "is_system BOOLEAN NOT NULL" in teams and "DEFAULT" not in teams, "no server default"
    assert "role VARCHAR(16) NOT NULL" in members and "CHECK" not in members
    assert "CONSTRAINT uq_team_members_team_user UNIQUE (team_id, user_id)" in members
    assert "FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE" in members
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in members
    assert "team_id INTEGER" in invitations and "team_role VARCHAR(16)," in invitations
    assert "FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE" in invitations
    assert "CREATE INDEX ix_invitations_team_id ON invitations (team_id)" in invitations


def test_alembic_finds_nothing_to_change_between_kurvenschmiede_s_models_and_the_kit_s() -> None:
    """``alembic revision --autogenerate`` in Kurvenschmiede after it swaps its models for the
    mixins: its database as its models made it, compared with the kit's tables — no diff."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    _metadata(_KsUser, _KsTeam, _KsTeamMember, _KsInvitation).create_all(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, _metadata(User, Team, TeamMember, Invitation)) == []
    engine.dispose()


def test_a_renamed_account_and_team_table_moves_every_foreign_key() -> None:
    members, invitations = _ddl(CrewMember), _ddl(CrewInvitation)
    assert "FOREIGN KEY(team_id) REFERENCES crews (id) ON DELETE CASCADE" in members
    assert "FOREIGN KEY(user_id) REFERENCES accounts (id) ON DELETE CASCADE" in members
    assert "FOREIGN KEY(team_id) REFERENCES crews (id) ON DELETE CASCADE" in invitations
    assert "colour VARCHAR(16)" in _ddl(Crew)


# ── The stores ──────────────────────────────────────────────────────────────


async def test_the_team_store_keeps_the_contract(session: AsyncSession) -> None:
    one, two = await _user(session, "one@example.org"), await _user(session, "two@example.org", last_name="Babbage")
    await check_team_store(_store(session), _invitations(session), user_id=one.id, other_user_id=two.id)


async def test_the_renamed_tables_keep_the_contract_too(session: AsyncSession) -> None:
    accounts = SqlUserStore(session, Account)
    one = await accounts.add(
        NewAccount(email="c1@example.org", password_hash="x", first_name="A", last_name="B", locale="en", role="M")
    )
    two = await accounts.add(
        NewAccount(email="c2@example.org", password_hash="x", first_name="A", last_name="B", locale="en", role="M")
    )
    store = SqlTeamStore(session, Crew, CrewMember, users=Account, invitations=CrewInvitation)
    invitations = SqlTeamInvitationStore(session, CrewInvitation, teams=Crew)
    await check_team_store(store, invitations, user_id=one.id, other_user_id=two.id)


async def test_the_team_invitation_store_keeps_the_invitation_contract(session: AsyncSession) -> None:
    """Its scope names team 7, which does not exist here: the scope is kept, the column not set."""
    one = await _user(session, "i1@example.org")
    await check_invitation_store(_invitations(session), inviter_id=one.id, user_id=one.id)


async def test_an_invitation_s_team_is_written_into_its_columns_too(session: AsyncSession) -> None:
    store, invitations = _store(session), _invitations(session)
    team = await store.add(NewTeam("Rig"))
    into = await invitations.add(_invitation("new@example.org", team_scope(team.id, TeamRole.MANAGER)))
    assert (into.team_id, into.team_role, into.scope) == (
        team.id,
        TeamRole.MANAGER,
        {"team_id": team.id, "team_role": "manager"},
    )
    for email, scope in (("plain@example.org", None), ("gone@example.org", team_scope(999, TeamRole.MEMBER))):
        row = await invitations.add(_invitation(email, scope))
        assert (row.team_id, row.team_role) == (None, None)
    await store.set_invitation_role(into, TeamRole.MEMBER)
    assert (into.team_role, into.scope) == (TeamRole.MEMBER, {"team_id": team.id, "team_role": "member"})


async def test_the_plain_invitation_store_never_writes_a_team_column(session: AsyncSession) -> None:
    store = _store(session)
    team = await store.add(NewTeam("Rig"))
    row = await SqlInvitationStore(session, Invitation).add(
        _invitation("new@example.org", team_scope(team.id, "member"))
    )
    assert row.team_id is None, "an app's own store would leave the team to the scope alone"


async def test_a_store_asked_about_no_teams_asks_the_database_nothing(session: AsyncSession) -> None:
    store = _store(session)
    assert (await store.names([]), await store.members([]), await store.open_invitations([])) == ({}, [], {})


async def test_a_join_and_the_log_s_names_run_on_the_tables(session: AsyncSession) -> None:
    store, invitations = _store(session), _invitations(session)
    log = SqlAdminActionLog(session, AdminActionRecord)
    ada = await _user(session, "ada@example.org")
    team = await store.add(NewTeam("Rig"))
    invitation = await invitations.add(_invitation(ada.email, team_scope(team.id, TeamRole.MEMBER)))
    assert await join_team(store, log, invitation, ada)
    assert team.version == 2
    hooks: TeamAccountHooks[Any] = TeamAccountHooks(store, log)
    gone = await store.add(NewTeam("Bench"))
    await log.add(
        {
            "at": NOW,
            "actor_id": ada.id,
            "action": "group_delete",
            "target_user_id": None,
            "target_email": None,
            "detail": {},
            "target_group_id": gone.id,
            "target_group_name": "Bench",
        }
    )
    await store.delete(gone)
    assert await hooks.group_names([team.id, gone.id]) == {team.id: "Rig", gone.id: "Bench"}
    await store.delete(team)
    assert await invitations.get(invitation.id) is None, "an accepted invitation goes with its team too"
