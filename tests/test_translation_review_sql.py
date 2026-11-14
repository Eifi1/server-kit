"""The ``sql`` extra's translation review half: the ``translation_reviews`` table as a mixin
and the store over it, on SQLite in memory through aiosqlite with foreign keys on. The
contract suite runs over both schemas of ``tests/sql_tables.py``; the rest is what it cannot
see: the unique pair, an erased reviewer, and the area filter on a database whose ``LIKE``
ignores case."""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import Table, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from eifi1_server_kit.translation_review import TranslationVerdict
from eifi1_server_kit.translation_review.ports import NewVerdict
from eifi1_server_kit.translation_review.sql import SqlTranslationReviewStore
from tests.sql_tables import (
    Account,
    Base,
    OtherBase,
    TranslationReview,
    User,
    Verdict,
    person,
    run_translation_review_suite,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _ddl(model: type[Any]) -> str:
    table = cast(Table, model.__table__)
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    @event.listens_for(engine.sync_engine, "connect")
    def on(connection: Any, record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(OtherBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _verdict(key: str, reviewer_id: int, **fields: Any) -> NewVerdict:
    values: dict[str, Any] = {
        "locale": "fr",
        "key": key,
        "text": "Texte",
        "reference_text": "Text",
        "verdict": TranslationVerdict.APPROVED,
        "note": None,
        "suggestion": None,
        "reviewer_id": reviewer_id,
        "reviewed_at": NOW,
    }
    values.update(fields)
    return NewVerdict(**values)


def test_the_module_names_its_extra_without_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.translation_review.sql", raising=False)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(
        ImportError,
        match=r"translation_review\.sql needs SQLAlchemy: install the kit with its sql extra, eifi1-server-kit\[sql\]",
    ):
        importlib.import_module("eifi1_server_kit.translation_review.sql")


def test_a_verdict_is_one_per_locale_and_key_and_outlives_its_reviewer() -> None:
    ddl = _ddl(TranslationReview)
    assert "UNIQUE (locale, key)" in ddl
    assert "reviewed_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "text TEXT NOT NULL" in ddl and "verdict VARCHAR(16) NOT NULL" in ddl
    (key,) = cast(Table, TranslationReview.__table__).foreign_keys
    assert (key.target_fullname, key.ondelete) == ("users.id", "SET NULL")
    (renamed,) = cast(Table, Verdict.__table__).foreign_keys
    assert renamed.target_fullname == "accounts.id"


async def test_the_contract_suite_passes_on_both_schemas(session: AsyncSession) -> None:
    await run_translation_review_suite(session)
    await run_translation_review_suite(session)
    await run_translation_review_suite(session, renamed=True)


async def test_the_unique_pair_holds_without_the_store(session: AsyncSession) -> None:
    reviewer = await person(session, User, "rita@example.org")
    for _ in range(2):
        session.add(
            TranslationReview(
                locale="fr", key="a.b", text="", verdict="APPROVED", reviewer_id=reviewer.id, reviewed_at=NOW
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_verdict_stays_with_no_name_once_its_reviewer_is_gone(session: AsyncSession) -> None:
    reviewer = await person(session, User, "rita@example.org")
    store = SqlTranslationReviewStore(session, TranslationReview, accounts=User)
    (row,) = await store.put([_verdict("legal.title", reviewer.id)])
    assert row.verdict == "APPROVED" and type(row.verdict) is str
    await session.delete(reviewer)
    await session.flush()
    session.expire(row)
    ((found, name),) = await store.verdicts(["fr"], None)
    assert found.key == "legal.title" and found.reviewer_id is None and name is None


async def test_the_area_filter_is_in_areas_on_a_database_whose_like_ignores_case(session: AsyncSession) -> None:
    """SQLite's ``LIKE`` folds ASCII case: ``LEGAL.title`` matches ``legal.%`` there, but lies
    outside the area ``legal`` (``in_areas``, and PostgreSQL's ``LIKE``)."""
    reviewer = await person(session, Account, "rita@example.org")
    store = SqlTranslationReviewStore(session, Verdict, accounts=Account)
    keys = ["legal.title", "LEGAL.title", "legal", "legal:imprint", "kit.legal.x", "legalese.x", "other"]
    await store.put([_verdict(key, reviewer.id) for key in keys])
    in_legal = [row.key for row, _ in await store.verdicts(["fr"], ["legal"])]
    assert in_legal == ["kit.legal.x", "legal", "legal.title", "legal:imprint"]
    assert await store.verdicts(["fr"], []) == []
    assert len(await store.verdicts(["fr"], None)) == len(keys)
    assert await store.verdicts(["de-CH"], None) == []


async def test_clear_answers_how_many_existed_and_put_takes_a_pair_once(session: AsyncSession) -> None:
    reviewer = await person(session, User, "rita@example.org")
    store = SqlTranslationReviewStore(session, TranslationReview, accounts=User)
    later = _verdict("a.b", reviewer.id, verdict=TranslationVerdict.NEEDS_CHANGE, suggestion="Mieux")
    first, second = await store.put([_verdict("a.b", reviewer.id), later])
    assert first is second and second.verdict == "NEEDS_CHANGE" and second.suggestion == "Mieux"
    assert await store.clear([]) == 0
    assert await store.clear([("fr", "a.b"), ("de-CH", "a.b"), ("fr", "never")]) == 1
    assert await store.by_reviewer(reviewer.id) == []
