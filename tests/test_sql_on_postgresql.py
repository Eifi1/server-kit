"""The ``sql`` extra's feedback and translation review stores on a real PostgreSQL, opt-in.

The kit's own runs use SQLite in memory, which takes the same statements but not the same
types: JSONB, ``timestamptz``, ``ON CONFLICT`` and ``LIKE … ESCAPE`` are PostgreSQL's in every
app. So the contract suites run here too, on both schemas of ``tests/sql_tables.py``, when
``EIFI1_TEST_POSTGRES_URL`` names a database whose role may create databases::

    EIFI1_TEST_POSTGRES_URL=postgresql+asyncpg://user:password@127.0.0.1:5432/maintenance \\
        uv run pytest tests/test_sql_on_postgresql.py

The test makes a throwaway database beside that one, builds both schemas, runs the suites,
and drops it again, also when a suite fails. Without the variable it is skipped: CI and a
laptop without PostgreSQL run the SQLite half only.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tests.sql_tables import Base, OtherBase, run_feedback_suites, run_translation_review_suite

URL = os.environ.get("EIFI1_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not URL, reason="EIFI1_TEST_POSTGRES_URL names no PostgreSQL to run on")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    maintenance = make_url(URL)
    name = f"eifi1_kit_{secrets.token_hex(4)}"
    # CREATE and DROP DATABASE run outside a transaction.
    admin = create_async_engine(maintenance, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(maintenance.set(database=name))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(OtherBase.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_every_feedback_suite_passes_on_postgresql_on_both_schemas(engine: AsyncEngine, tmp_path: Path) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await run_feedback_suites(session, tmp_path / "objects")
        await run_feedback_suites(session, tmp_path / "objects", renamed=True)


async def test_the_translation_review_suite_passes_on_postgresql_on_both_schemas(engine: AsyncEngine) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await run_translation_review_suite(session)
        await run_translation_review_suite(session, renamed=True)
