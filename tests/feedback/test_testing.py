"""``feedback.testing``: the three conformance checks pass on the fakes and fail on a broken one.

server-services-harmonization §2.14: an app runs ``check_repository`` and ``check_storage``
once against its own adapters, and ``check_round_trip`` over its own client.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

import pytest

from eifi1_server_kit.feedback import (
    UUID32_SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    FeedbackActor,
    FeedbackCategory,
    FeedbackStatus,
    StoredAttachment,
)
from eifi1_server_kit.feedback.testing import (
    InMemoryAttachmentStorage,
    InMemoryFeedbackRepository,
    MemoryFeedbackRow,
    _png,
    actor,
    check_repository,
    check_round_trip,
    check_storage,
    fixed_clock,
)
from eifi1_server_kit.uploads import CheckedUpload, ensure_decodable_image
from tests.feedback._layer2 import AUTHOR, NOW, POLICY, STRANGER

EMAILS = {AUTHOR.id: AUTHOR.email, STRANGER.id: STRANGER.email}


async def _check(repository: InMemoryFeedbackRepository, storage: InMemoryAttachmentStorage | None = None) -> None:
    await check_repository(
        repository, storage=storage or InMemoryAttachmentStorage(), policy=POLICY, author=AUTHOR, other=STRANGER
    )


def test_actor_and_the_clock() -> None:
    assert actor() == FeedbackActor(id=1, email="author@example.test", display_name="Ada Author", is_admin=False)
    assert actor(id=7, is_admin=True).is_admin and actor(is_demo=True).is_demo
    clock = fixed_clock(NOW)
    assert clock() == clock() == NOW


def test_the_checks_picture_decodes_and_carries_its_note() -> None:
    picture = _png("abc123")
    ensure_decodable_image(picture, "image/png")
    assert b"abc123" in picture and _png("abc123") != _png("abc124")


def test_the_memory_row_refuses_a_column_that_does_not_exist() -> None:
    row = MemoryFeedbackRow(
        id=1,
        user_id=1,
        title="t",
        body="",
        category=FeedbackCategory.BUG,
        status=FeedbackStatus.OPEN,
        context=None,
        screenshot_url=None,
        attachment_urls=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert row.outcome is None and row.resolved_at is None and row.crash_fingerprint is None
    with pytest.raises(AttributeError):
        setattr(row, "user_email", "x")  # noqa: B010 - the point is a name the class does not have


# --- check_repository --------------------------------------------------------------------


async def test_check_repository_passes_on_the_fake_with_ties_and_without() -> None:
    """A fixed clock makes every ``created_at`` a tie, as one Postgres transaction's
    ``now()`` does; the real clock does not."""
    await _check(InMemoryFeedbackRepository(emails=EMAILS, now=fixed_clock(NOW), policy=POLICY))
    await _check(InMemoryFeedbackRepository(emails=EMAILS, policy=POLICY))
    # Twice on the same "database": it asserts only on what it adds.
    shared, storage = InMemoryFeedbackRepository(emails=EMAILS, policy=POLICY), InMemoryAttachmentStorage()
    await _check(shared, storage)
    await _check(shared, storage)


async def test_check_repository_fails_without_the_id_tie_break() -> None:
    class NoTieBreak(InMemoryFeedbackRepository):
        async def list_mine(self, user_id: int) -> Sequence[MemoryFeedbackRow]:
            rows = [row for row in self.rows.values() if row.user_id == user_id]
            return sorted(rows, key=lambda row: row.created_at, reverse=True)

    with pytest.raises(AssertionError, match="list_mine: newest first, the higher id first on a tie"):
        await _check(NoTieBreak(emails=EMAILS, now=fixed_clock(NOW), policy=POLICY))


async def test_check_repository_fails_on_a_repository_that_misses_the_bodys_urls() -> None:
    class ColumnsOnly(InMemoryFeedbackRepository):
        async def urls_named_except(self, row_ids: Collection[int]) -> set[str]:
            return {
                url
                for row in self.rows.values()
                if row.id not in row_ids
                for url in (row.screenshot_url, *(row.attachment_urls or ()))
                if url
            }

    with pytest.raises(AssertionError, match="urls_named_except: a URL another row's body names"):
        await _check(ColumnsOnly(emails=EMAILS, policy=POLICY))


async def test_check_repository_fails_on_a_crash_lookup_that_keeps_settled_rows() -> None:
    class KeepsDone(InMemoryFeedbackRepository):
        async def open_crash(self, fingerprint: str) -> MemoryFeedbackRow | None:
            found = [row for row in self.rows.values() if row.crash_fingerprint == fingerprint]
            return found[0] if found and found[0].status is not FeedbackStatus.WONT_DO else None

    with pytest.raises(AssertionError, match="open_crash: not a DONE row"):
        await _check(KeepsDone(emails=EMAILS, policy=POLICY))


async def test_the_checks_fail_on_a_storage_whose_key_the_policy_refuses() -> None:
    kastlans = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)
    with pytest.raises(AssertionError, match="is one the policy accepts"):
        await check_storage(InMemoryAttachmentStorage(), policy=kastlans, uploader=AUTHOR)
    with pytest.raises(AssertionError, match="is one the policy accepts"):
        await check_repository(
            InMemoryFeedbackRepository(emails=EMAILS, policy=kastlans),
            storage=InMemoryAttachmentStorage(),
            policy=kastlans,
            author=AUTHOR,
            other=STRANGER,
        )


# --- check_storage -----------------------------------------------------------------------


async def test_check_storage_passes_on_the_fake_and_its_uploader_only_mode() -> None:
    for storage in (InMemoryAttachmentStorage(), InMemoryAttachmentStorage(uploader_only=True)):
        await check_storage(storage, policy=POLICY, uploader=AUTHOR)


async def test_check_storage_fails_on_a_storage_that_loses_files_or_their_type() -> None:
    class Forgetful(InMemoryAttachmentStorage):
        async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
            return None

    with pytest.raises(AssertionError, match="read: the png file's bytes come back"):
        await check_storage(Forgetful(), policy=POLICY, uploader=AUTHOR)

    class Untyped(InMemoryAttachmentStorage):
        async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
            found = await super().read(key, viewer=viewer)
            return None if found is None else StoredAttachment(found.data, None, found.filename)

    with pytest.raises(AssertionError, match="read: the txt file serves as text/plain"):
        await check_storage(Untyped(), policy=POLICY, uploader=AUTHOR)

    class Inventive(InMemoryAttachmentStorage):
        async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
            found = await super().read(key, viewer=viewer)
            return found or StoredAttachment(b"%PDF-", "application/pdf", None)

    with pytest.raises(AssertionError, match="read: a key nobody saved is None"):
        await check_storage(Inventive(), policy=POLICY, uploader=AUTHOR)


class _Rekeyed(InMemoryAttachmentStorage):
    """A storage that keeps the fake's files under keys of another shape."""

    def rekey(self, key: str) -> str:
        raise NotImplementedError

    async def save(self, data: bytes, upload: CheckedUpload, *, uploader: FeedbackActor, filename: str | None) -> str:
        key = self.rekey(await super().save(data, upload, uploader=uploader, filename=filename))
        self.files[key] = (StoredAttachment(data, upload.media_type, filename), uploader.id)
        return key


async def test_check_storage_builds_an_absent_key_of_any_shape() -> None:
    """A key without an extension is turned whole; where the policy does not take the
    turned key, that one assertion is skipped rather than failing a storage that keeps
    its promises."""

    class Bare(_Rekeyed):
        def rekey(self, key: str) -> str:
            return key.split(".")[0]

    await check_storage(Bare(), policy=AttachmentUrlPolicy(r"[a-f0-9]{12}"), uploader=AUTHOR)

    class LeadingA(_Rekeyed):
        def rekey(self, key: str) -> str:
            return "a" + key[1:]

    # "a…" turns into "b…", which this policy refuses.
    await check_storage(LeadingA(), policy=AttachmentUrlPolicy(r"a[a-f0-9]{11}\.(?:png|pdf|txt)"), uploader=AUTHOR)


# --- check_round_trip --------------------------------------------------------------------


async def test_check_round_trip_fails_where_the_prefix_is_not_the_mount() -> None:
    """kastlan's case (ka 2): its upload URL came from ``Location`` until now."""
    from tests.feedback.test_router import World

    world = World()
    async with world.client:
        elsewhere = AttachmentUrlPolicy(POLICY.key_pattern, prefix="/api/v2/feedback/attachments/")
        with pytest.raises(AssertionError, match="upload: 201, not 404"):
            await check_round_trip(world.client, policy=elsewhere, headers=World.signed_in(AUTHOR))
