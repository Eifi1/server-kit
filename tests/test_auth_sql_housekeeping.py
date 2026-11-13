"""The ``sql`` extra's housekeeping (0.30): the public instant type and the Alembic hook that
writes it as a plain column type, and the pruning of rows nobody needs any more. On SQLite
in memory, as ``test_auth_sql``; a schema of its own, so the two files share nothing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy
from alembic import op
from alembic.autogenerate import compare_metadata, produce_migrations, render_python_code
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import String, create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from eifi1_server_kit.auth import sql
from eifi1_server_kit.auth.sql import (
    AccountColumns,
    AdminActionColumns,
    InvitationColumns,
    OneTimeTokenColumns,
    PasskeyColumns,
    SessionColumns,
    SpentChallengeColumns,
    UtcDateTime,
    alembic_render_item,
)


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
    with engine.connect() as connection:
        script = produce_migrations(MigrationContext.configure(connection), Base.metadata)
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
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            namespace["upgrade"]()
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables)
        compared = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(compared, Base.metadata) == []
