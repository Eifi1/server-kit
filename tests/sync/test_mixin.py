"""The SQLAlchemy half (local-first §1.6) on SQLite in memory: the stamping hook, the
tombstones and their filter, on a Session subclass of its own so no other test sees them."""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import ForeignKey, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from eifi1_server_kit import sync
from eifi1_server_kit.sync import (
    INCLUDE_TOMBSTONED,
    SyncColumns,
    bulk_restamp,
    bulk_tombstone,
    install_sync_stamping,
    install_tombstone_filter,
    tombstone,
)
from eifi1_server_kit.sync.testing import InMemoryKnowledgeCounter

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
EDITED = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Folder(SyncColumns, Base):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36))
    notes: Mapped[list[Note]] = relationship(back_populates="folder")


class Note(SyncColumns, Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36))
    text: Mapped[str] = mapped_column(String(200), default="")
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id"))
    folder: Mapped[Folder | None] = relationship(back_populates="notes")


class Plain(Base):
    __tablename__ = "plain"
    id: Mapped[int] = mapped_column(primary_key=True)


class SyncSession(Session):
    pass


def _naive_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive values; compare in UTC."""
    return None if value is None else sync.as_utc(value)


@pytest.fixture
def counter() -> InMemoryKnowledgeCounter:
    return InMemoryKnowledgeCounter()


@pytest.fixture
def make(counter: InMemoryKnowledgeCounter) -> Iterator[sessionmaker[SyncSession]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    removers = [
        install_sync_stamping(
            SyncSession,
            counter=counter,
            tenant_of=lambda obj: getattr(obj, "tenant_id", None),
            is_synced=lambda obj: isinstance(obj, SyncColumns),
            now=lambda: NOW,
        ),
        install_tombstone_filter(SyncSession, synced=SyncColumns),
    ]
    yield sessionmaker(engine, class_=SyncSession, expire_on_commit=False)
    for remove in removers:
        remove()
    engine.dispose()


def test_an_insert_is_stamped_once_per_tenant_under_no_autoflush(
    make: sessionmaker[SyncSession], counter: InMemoryKnowledgeCounter
) -> None:
    with make() as session:
        session.add_all([Note(id="n1", tenant_id="t1"), Note(id="n2", tenant_id="t1"), Note(id="n3", tenant_id="t2")])
        session.add(Plain(id=1))
        session.commit()
        n1, n2, n3 = (session.get(Note, i) for i in ("n1", "n2", "n3"))
        assert n1 is not None and n2 is not None and n3 is not None
        assert (n1.knowledge, n2.knowledge, n3.knowledge) == (1, 1, 1)
        assert _naive_utc(n1.updated_at) == NOW
    assert counter.calls == [("t1", False), ("t2", False)] or counter.calls == [("t2", False), ("t1", False)]
    assert counter.current("t1") == 1 and counter.current("unknown") == 0


def test_an_explicit_edit_time_survives_the_flush(make: sessionmaker[SyncSession]) -> None:
    with make() as session:
        session.add(Note(id="n1", tenant_id="t1", updated_at=EDITED))
        session.commit()
        note = session.get(Note, "n1")
        assert note is not None and _naive_utc(note.updated_at) == EDITED
        # An update that sets the stamp itself (the /sync apply path) keeps it.
        later = EDITED + timedelta(hours=1)
        note.text, note.updated_at = "synced", later
        session.commit()
        assert _naive_utc(note.updated_at) == later and note.knowledge == 2


def test_an_update_without_a_stamp_is_restamped(make: sessionmaker[SyncSession]) -> None:
    with make() as session:
        session.add(Note(id="n1", tenant_id="t1", updated_at=EDITED))
        session.commit()
        note = session.get(Note, "n1")
        assert note is not None
        note.text = "rest write"
        session.commit()
        assert _naive_utc(note.updated_at) == NOW and note.knowledge == 2
        # Touched but unchanged: no bump.
        note.text = "rest write"
        session.commit()
        assert note.knowledge == 2


def test_a_tombstone_moves_updated_at_and_is_hidden_unless_asked(make: sessionmaker[SyncSession]) -> None:
    with make() as session:
        session.add(Folder(id="f1", tenant_id="t1"))
        session.add_all([Note(id="n1", tenant_id="t1", folder_id="f1"), Note(id="n2", tenant_id="t1", folder_id="f1")])
        session.commit()
        note = session.get(Note, "n1")
        assert note is not None
        tombstone(note, now=EDITED)
        assert note.deleted_at == EDITED and note.updated_at == EDITED
        session.commit()
        assert note.knowledge == 2
    with make() as session:
        assert [n.id for n in session.scalars(select(Note))] == ["n2"]
        every = session.scalars(select(Note).order_by(Note.id).execution_options(**{INCLUDE_TOMBSTONED: True}))
        assert [n.id for n in every] == ["n1", "n2"]
        folder = session.get(Folder, "f1")
        assert folder is not None and [n.id for n in folder.notes] == ["n2"]
        # A column load (an expired attribute) and an ORM UPDATE pass through untouched.
        session.expire(folder, ["tenant_id"])
        assert folder.tenant_id == "t1"
        session.execute(update(Note).where(Note.id == "n2").values(text="x"))


def test_tombstone_defaults_to_now() -> None:
    note = Note(id="n")
    tombstone(note)
    assert note.deleted_at is not None and note.deleted_at == note.updated_at
    assert abs(note.deleted_at - datetime.now(UTC)) < timedelta(minutes=1)


def test_rows_without_a_tenant_or_counter_are_left_alone(make: sessionmaker[SyncSession]) -> None:
    with make() as session:
        session.add(Note(id="n1", tenant_id=None))
        session.commit()
        note = session.get(Note, "n1")
        assert note is not None and note.knowledge == 0


def test_a_counter_without_the_tenant_skips_the_row() -> None:
    class NoTenant:
        def bump(self, session: Session, tenant_id: object) -> int | None:
            return None

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    class Other(Session):
        pass

    remove = install_sync_stamping(
        Other, counter=NoTenant(), tenant_of=lambda obj: "t", is_synced=lambda obj: isinstance(obj, SyncColumns)
    )
    try:
        with Other(engine) as session:
            session.add(Note(id="n1"))
            session.commit()
            note = session.get(Note, "n1")
            assert note is not None and note.knowledge == 0
    finally:
        remove()
        engine.dispose()


def test_one_counter_for_every_tenant(make: sessionmaker[SyncSession]) -> None:
    """kastlan's database-wide row: the tenant is ignored."""
    shared = InMemoryKnowledgeCounter(per_tenant=False, start=10)
    remove = install_sync_stamping(
        SyncSession, counter=shared, tenant_of=lambda obj: 1, is_synced=lambda obj: isinstance(obj, Note)
    )
    try:
        with make() as session:
            session.add(Note(id="n1", tenant_id="a"))
            session.commit()
        assert shared.current() == 11 and shared.current("anything") == 11
    finally:
        remove()


def test_the_listeners_come_off(make: sessionmaker[SyncSession], counter: InMemoryKnowledgeCounter) -> None:
    other = InMemoryKnowledgeCounter()
    remove = install_sync_stamping(
        SyncSession, counter=other, tenant_of=lambda obj: "t", is_synced=lambda obj: isinstance(obj, Note)
    )
    remove()
    with make() as session:
        session.add(Note(id="n1", tenant_id="t1"))
        session.commit()
    assert other.calls == [] and counter.calls == [("t1", False)]


def test_bulk_statements_stamp_what_the_hook_cannot(make: sessionmaker[SyncSession]) -> None:
    with make() as session:
        session.add_all([Note(id="n1", tenant_id="t1"), Note(id="n2", tenant_id="t1"), Note(id="n3", tenant_id="t1")])
        session.commit()
        moved = session.execute(bulk_restamp(Note, Note.id == "n3", values={"text": "moved"}, knowledge=7, now=EDITED))
        gone = session.execute(bulk_tombstone(Note, Note.id.in_(["n1", "n2"]), knowledge=7, now=EDITED))
        session.commit()
        assert moved.rowcount == 1 and gone.rowcount == 2  # type: ignore[attr-defined]
        rows = session.execute(
            select(Note.id, Note.knowledge, Note.text, Note.deleted_at)
            .order_by(Note.id)
            .execution_options(**{INCLUDE_TOMBSTONED: True})
        ).all()
        assert [(r[0], r[1], r[2], _naive_utc(r[3])) for r in rows] == [
            ("n1", 7, "", EDITED),
            ("n2", 7, "", EDITED),
            ("n3", 7, "moved", None),
        ]


def test_the_package_loads_the_mixin_lazily() -> None:
    from eifi1_server_kit.sync import mixin

    assert sync.SyncColumns is mixin.SyncColumns and sync.INCLUDE_TOMBSTONED == "include_tombstoned"
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = sync.nope


def test_the_package_loads_no_sqlalchemy() -> None:
    """The schemas, merge, paging and the counter port run without the extra. A fresh
    interpreter, so nothing another test imported counts."""
    probe = (
        "import sys, eifi1_server_kit.sync, eifi1_server_kit.sync.testing;"
        "print(sorted({name.split('.')[0] for name in sys.modules} & {'sqlalchemy', 'fastapi', 'httpx'}))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert loaded.strip() == "[]"


def test_the_mixin_names_its_extra_without_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import sqlalchemy`` fail as if it were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.sync.mixin", raising=False)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(ImportError, match=r"install the kit with its sync extra, eifi1-server-kit\[sync\]"):
        importlib.import_module("eifi1_server_kit.sync.mixin")
