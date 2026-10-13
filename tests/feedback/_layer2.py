"""What the layer-2 tests share: the people, the policy, the files and a spying repository."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from eifi1_server_kit.feedback import (
    SHA12_KEY_PATTERN,
    AttachmentStorage,
    AttachmentUrlPolicy,
    FeedbackActor,
    FeedbackRepository,
    FeedbackRow,
    FeedbackStatus,
    NewFeedback,
)
from eifi1_server_kit.feedback.testing import (
    InMemoryAttachmentStorage,
    InMemoryFeedbackRepository,
    MemoryFeedbackRow,
    actor,
)

NOW = datetime(2026, 10, 13, 9, 30, tzinfo=UTC)
#: keksdose's keys at the standard mount.
POLICY = AttachmentUrlPolicy(SHA12_KEY_PATTERN)

AUTHOR = actor()
ADMIN = actor(id=2, email="admin@example.test", display_name="Ann Admin", is_admin=True)
STRANGER = actor(id=3, email="sam@example.test", display_name="Sam Stranger")
DEMO = actor(id=4, email="demo@example.test", display_name=None, is_demo=True)
EMAILS = {person.id: person.email for person in (AUTHOR, ADMIN, STRANGER, DEMO)}

WRITES = frozenset({"add", "apply"})


def png(size: tuple[int, int] = (8, 8), color: str = "red") -> bytes:
    """A real picture, made by Pillow, so the ``images`` guard passes it."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


PDF = b"%PDF-1.4\n% a report\n%%EOF\n"
TEXT = "Débattement à gauche\n".encode()


class Upload:
    """An :class:`~eifi1_server_kit.uploads.UploadSource` that counts its reads."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return self.data if size < 0 else self.data[:size]


class SpyRepository(InMemoryFeedbackRepository):
    """The fake, logging every call in order and every write's actor."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.calls: list[str] = []
        self.actors: list[FeedbackActor | None] = []

    async def get(self, feedback_id: int, *, viewer: FeedbackActor) -> MemoryFeedbackRow | None:
        self.calls.append("get")
        return await super().get(feedback_id, viewer=viewer)

    async def list_mine(self, user_id: int) -> Sequence[MemoryFeedbackRow]:
        self.calls.append("list_mine")
        return await super().list_mine(user_id)

    async def inbox(self, status: FeedbackStatus | None) -> Sequence[tuple[MemoryFeedbackRow, str | None]]:
        self.calls.append("inbox")
        return await super().inbox(status)

    async def open_crash(self, fingerprint: str) -> MemoryFeedbackRow | None:
        self.calls.append("open_crash")
        return await super().open_crash(fingerprint)

    async def add(self, new: NewFeedback) -> MemoryFeedbackRow:
        self.calls.append("add")
        return await super().add(new)

    async def apply(
        self, row: MemoryFeedbackRow, changes: Mapping[str, Any], *, actor: FeedbackActor | None
    ) -> MemoryFeedbackRow:
        self.calls.append("apply")
        self.actors.append(actor)
        return await super().apply(row, changes, actor=actor)

    async def by_author(self, user_id: int) -> Sequence[MemoryFeedbackRow]:
        self.calls.append("by_author")
        return await super().by_author(user_id)

    async def urls_named_except(self, row_ids: Collection[int]) -> set[str]:
        self.calls.append("urls_named_except")
        return await super().urls_named_except(row_ids)

    def one_write_last(self) -> bool:
        """At most one write, and, if there is one, it is the last call."""
        writes = [call for call in self.calls if call in WRITES]
        return len(writes) <= 1 and (not writes or self.calls[-1] in WRITES)


def _the_fakes_satisfy_the_ports(
    row: MemoryFeedbackRow, repository: InMemoryFeedbackRepository, storage: InMemoryAttachmentStorage
) -> tuple[FeedbackRow, FeedbackRepository[MemoryFeedbackRow], AttachmentStorage]:
    """mypy checks that the fakes are the ports."""
    return row, repository, storage
