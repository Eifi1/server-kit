"""The feedback service on the fakes (server-services-harmonization §2.10, §2.11, §2.15, §4.1).

Every flow once, in the apps' order: the demo first, the limiter before the work, CRASH
before the lookup, the lookup before the rules, and at most one write, last.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from eifi1_server_kit.demo import DemoError, DemoErrorCode
from eifi1_server_kit.feedback import (
    DEFAULT_ATTACHMENT_URL_POLICY,
    ERASED_MARK,
    THROTTLED_DETAIL,
    AttachmentNotFoundError,
    CrashCategoryNotAssignableError,
    CrashReportCreate,
    FeedbackActor,
    FeedbackCategory,
    FeedbackCreate,
    FeedbackForbiddenError,
    FeedbackNotFoundError,
    FeedbackService,
    FeedbackStatus,
    FeedbackThrottledError,
    FeedbackUpdate,
    NewFeedback,
    StoredAttachment,
    attachment_line,
    crash_fingerprint,
    download_attachment,
    throttle_upload,
    upload_attachment,
    utc_now,
)
from eifi1_server_kit.feedback.service import ErasedFeedback
from eifi1_server_kit.feedback.testing import (
    InMemoryAttachmentStorage,
    InMemoryFeedbackRepository,
    MemoryFeedbackRow,
    fixed_clock,
)
from eifi1_server_kit.limiter import SlidingWindowRateLimiter
from eifi1_server_kit.uploads import EmptyUploadError, NotAnImageError, UnsupportedUploadTypeError
from tests.feedback._layer2 import (
    ADMIN,
    AUTHOR,
    DEMO,
    EMAILS,
    NOW,
    PDF,
    POLICY,
    STRANGER,
    TEXT,
    SpyRepository,
    Upload,
    png,
)


class Create(FeedbackCreate):
    attachment_url_policy = POLICY


def _service(repository: InMemoryFeedbackRepository | None = None, **options: Any) -> FeedbackService[Any]:
    options.setdefault("policy", POLICY)
    options.setdefault("now", fixed_clock(NOW))
    return FeedbackService(repository or SpyRepository(emails=EMAILS, now=fixed_clock(NOW)), **options)


def _limiter(max_hits: int = 20) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(max_hits=max_hits, window_seconds=3600)


def _crash(message: str = "x is undefined", **fields: Any) -> CrashReportCreate:
    return CrashReportCreate(name="TypeError", message=message, route="/accounts", version="1.0.0", **fields)


async def _filed(service: FeedbackService[Any], by: FeedbackActor = AUTHOR, **fields: Any) -> MemoryFeedbackRow:
    values: dict[str, Any] = {"title": "Sync is slow", "body": "Opening the register takes 4s"}
    values.update(fields)
    row: MemoryFeedbackRow = await service.create(by, Create(**values))
    return row


def _repository(service: FeedbackService[Any]) -> SpyRepository:
    repository = service.repository
    assert isinstance(repository, SpyRepository)
    return repository


# --- the clock ---------------------------------------------------------------------------


def test_the_default_clock_is_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


# --- create ------------------------------------------------------------------------------


async def test_create_files_in_the_servers_status_with_the_sessions_identity() -> None:
    """§2.13: the three ``user_*`` keys are the session's whatever the client sent; the
    rest of the context is the client's. OPEN for anyone, READY for an admin (§8.2)."""
    service = _service()
    row = await _filed(
        service,
        context={"route": "/accounts", "user_id": 99, "user_email": "spoof@example.test", "user_display_name": "Eve"},
    )
    assert row.status is FeedbackStatus.OPEN
    assert row.user_id == AUTHOR.id
    assert row.context == {
        "route": "/accounts",
        "user_id": AUTHOR.id,
        "user_email": AUTHOR.email,
        "user_display_name": AUTHOR.display_name,
    }
    assert row.screenshot_url is None and row.attachment_urls is None  # never []
    assert row.category is FeedbackCategory.OTHER
    assert _repository(service).calls == ["add"]

    admins = await _filed(service, by=ADMIN)
    assert admins.status is FeedbackStatus.READY


async def test_a_create_without_a_context_stores_the_three_keys() -> None:
    """The one visible change for keksdose (§2.13): ``context: null`` is stamped too."""
    row = await _filed(_service())
    assert row.context == {"user_id": 1, "user_email": "author@example.test", "user_display_name": "Ada Author"}


async def test_create_keeps_the_named_files() -> None:
    shot, file = POLICY.url_for("0123456789ab.png"), POLICY.url_for("ba9876543210.pdf")
    row = await _filed(_service(), screenshot_url=shot, attachment_urls=[file, file])
    assert row.screenshot_url == shot
    assert row.attachment_urls == [file]


async def test_create_refuses_a_demo_and_crash_by_hand_before_any_write() -> None:
    service = _service()
    with pytest.raises(DemoError) as refused:
        await _filed(service, by=DEMO)
    assert refused.value.code is DemoErrorCode.DEMO_REFUSED and "feedback" in str(refused.value)
    with pytest.raises(CrashCategoryNotAssignableError):
        await _filed(service, by=ADMIN, category=FeedbackCategory.CRASH)
    assert _repository(service).calls == []


# --- report_crash ------------------------------------------------------------------------


async def test_a_new_crash_is_filed_open_with_the_services_clock() -> None:
    service = _service()
    answer = await service.report_crash(ADMIN, _crash(), limiter=_limiter())
    fingerprint = crash_fingerprint(_crash(), ADMIN.id)
    assert answer.stored and not answer.duplicate
    assert answer.reference == fingerprint[:8]
    row = _repository(service).rows[answer.feedback_id or 0]
    # OPEN whoever's session it came from, an admin's included (§8.2).
    assert row.status is FeedbackStatus.OPEN and row.category is FeedbackCategory.CRASH
    assert row.crash_fingerprint == fingerprint
    assert row.title == "[crash] TypeError: x is undefined"
    assert f"User:        {ADMIN.email} (#{ADMIN.id})" in row.body
    assert row.context is not None
    assert row.context["first_seen_at"] == NOW.isoformat()
    assert row.context["user_display_name"] == ADMIN.display_name
    assert _repository(service).calls == ["open_crash", "add"]


async def test_a_repeat_folds_into_the_open_row_and_a_settled_one_files_anew() -> None:
    service = _service()
    first = await service.report_crash(AUTHOR, _crash())
    later = NOW + timedelta(minutes=5)
    again = await service.report_crash(AUTHOR, _crash(occurred_at=later))
    assert again.duplicate and again.feedback_id == first.feedback_id and again.reference == first.reference
    repository = _repository(service)
    row = repository.rows[first.feedback_id or 0]
    assert row.context is not None
    assert row.context["occurrences"] == 2 and row.context["last_seen_at"] == later.isoformat()
    assert repository.actors == [AUTHOR]  # the fold is the request's write

    row.status = FeedbackStatus.DONE
    regression = await service.report_crash(AUTHOR, _crash())
    assert regression.stored and not regression.duplicate and regression.feedback_id != first.feedback_id


async def test_a_terminal_row_an_adapter_hands_back_is_not_folded_into() -> None:
    """``decide_crash`` keeps the rule even when an adapter forgets the status filter."""

    class Careless(SpyRepository):
        async def open_crash(self, fingerprint: str) -> MemoryFeedbackRow | None:
            return next((row for row in self.rows.values() if row.crash_fingerprint == fingerprint), None)

    service = _service(Careless(now=fixed_clock(NOW)))
    first = await service.report_crash(AUTHOR, _crash())
    _repository(service).rows[first.feedback_id or 0].status = FeedbackStatus.WONT_DO
    second = await service.report_crash(AUTHOR, _crash())
    assert not second.duplicate and second.feedback_id != first.feedback_id


async def test_a_demos_crash_is_not_stored_and_charges_nothing() -> None:
    service, limiter = _service(), _limiter()
    assert (await service.report_crash(DEMO, _crash(), limiter=limiter)).stored is False
    assert limiter.count(str(DEMO.id)) == 0
    assert _repository(service).calls == []


async def test_the_crash_window_is_charged_before_the_lookup_so_repeats_count() -> None:
    """§2.11: a duplicate uses a slot, so ``occurrences`` stops rising once the window is spent."""
    service, limiter = _service(), _limiter(max_hits=2)
    assert (await service.report_crash(AUTHOR, _crash(), limiter=limiter)).stored
    assert (await service.report_crash(AUTHOR, _crash(), limiter=limiter)).duplicate
    calls = len(_repository(service).calls)
    spent = await service.report_crash(AUTHOR, _crash(), limiter=limiter)
    assert spent.stored is False and spent.feedback_id is None
    assert len(_repository(service).calls) == calls  # not even a read


# --- the lists ---------------------------------------------------------------------------


async def test_list_mine_is_the_actors_own_newest_first_the_higher_id_on_a_tie() -> None:
    service = _service()
    first = await _filed(service)
    await _filed(service, by=STRANGER)
    second = await _filed(service)
    assert [row.id for row in await service.list_mine(AUTHOR)] == [second.id, first.id]


async def test_the_inbox_is_an_admins_with_each_authors_address() -> None:
    service = _service()
    mine = await _filed(service)
    theirs = await _filed(service, by=STRANGER)
    theirs.status = FeedbackStatus.DONE
    with pytest.raises(FeedbackForbiddenError, match=r"^Insufficient role$"):
        await service.inbox(AUTHOR)
    assert [(row.id, address) for row, address in await service.inbox(ADMIN)] == [
        (theirs.id, STRANGER.email),
        (mine.id, AUTHOR.email),
    ]
    assert [row.id for row, _ in await service.inbox(ADMIN, FeedbackStatus.DONE)] == [theirs.id]
    mine.user_id = None  # erased
    assert (mine.id, None) in [(row.id, address) for row, address in await service.inbox(ADMIN)]


# --- update ------------------------------------------------------------------------------


async def test_an_author_edits_their_own_and_the_write_is_the_last_step() -> None:
    service = _service()
    row = await _filed(service)
    repository = _repository(service)
    repository.calls.clear()
    updated = await service.update(AUTHOR, row.id, FeedbackUpdate(title="Sync is very slow"))
    assert updated.title == "Sync is very slow"
    assert repository.calls == ["get", "apply"] and repository.actors == [AUTHOR]


async def test_an_update_refuses_a_demo_then_crash_then_a_missing_row() -> None:
    """keksdose's order (§2.10): the demo first, CRASH before the lookup — so a missing
    row with CRASH in the payload is a 422, not a 404 — then the lookup."""
    service = _service()
    with pytest.raises(DemoError):
        await service.update(DEMO, 404, FeedbackUpdate(category=FeedbackCategory.CRASH))
    with pytest.raises(CrashCategoryNotAssignableError):
        await service.update(ADMIN, 404, FeedbackUpdate(category=FeedbackCategory.CRASH))
    assert _repository(service).calls == []
    with pytest.raises(FeedbackNotFoundError) as missing:
        await service.update(ADMIN, 404, FeedbackUpdate(title="x"))
    assert missing.value.status_code == 404 and str(missing.value) == "Feedback not found"


async def test_a_foreign_row_is_the_apps_choice_403_or_404() -> None:
    forbidden = _service()
    row = await _filed(forbidden)
    with pytest.raises(FeedbackForbiddenError, match=r"^Not the author$"):
        await forbidden.update(STRANGER, row.id, FeedbackUpdate(title="mine now"))

    hidden = _service(_repository(forbidden), foreign_row="not_found")
    with pytest.raises(FeedbackNotFoundError):
        await hidden.update(STRANGER, row.id, FeedbackUpdate(title="mine now"))
    # The author's own rule break still speaks (403), and an admin is never refused this way.
    with pytest.raises(FeedbackForbiddenError, match=r"^Author cannot change: status$"):
        await hidden.update(AUTHOR, row.id, FeedbackUpdate(status=FeedbackStatus.DONE))
    done = await hidden.update(ADMIN, row.id, FeedbackUpdate(status=FeedbackStatus.DONE))
    assert done.status is FeedbackStatus.DONE
    # A row whose author was erased is foreign to everyone but an admin.
    row.user_id = None
    with pytest.raises(FeedbackNotFoundError):
        await hidden.update(AUTHOR, row.id, FeedbackUpdate(body="more"))
    assert _repository(forbidden).actors == [ADMIN]


async def test_a_row_the_adapter_hides_is_a_404_whatever_the_setting() -> None:
    repository = SpyRepository(
        now=fixed_clock(NOW), hidden=lambda row, viewer: row.user_id != viewer.id and not viewer.is_admin
    )
    service = _service(repository)
    row = await _filed(service)
    with pytest.raises(FeedbackNotFoundError):
        await service.update(STRANGER, row.id, FeedbackUpdate(title="x"))


async def test_resolved_at_and_the_rework_use_the_rules_and_the_clock() -> None:
    service = _service()
    row = await _filed(service)
    done = await service.update(ADMIN, row.id, FeedbackUpdate(status=FeedbackStatus.DONE))
    assert done.resolved_at == NOW and done.outcome is None
    reworked = await service.update(AUTHOR, row.id, FeedbackUpdate(body=row.body + "\n\n--- REWORK ---\nStill slow"))
    assert reworked.status is FeedbackStatus.OPEN and reworked.resolved_at is None


# --- erase_author ------------------------------------------------------------------------


async def test_erase_author_takes_the_person_out_and_answers_the_keys_to_delete() -> None:
    """§2.15: every row anonymised with ``actor=None``; the body's file lines stripped; a key
    another report still names is kept; the keys are bare and sorted."""
    service = _service()
    shared, own, rework = (POLICY.url_for(key) for key in ("aaaaaaaaaaaa.png", "bbbbbbbbbbbb.pdf", "cccccccccccc.txt"))
    first = await _filed(service, screenshot_url=shared, attachment_urls=[own])
    second = await _filed(
        service,
        title=f"Crash for {AUTHOR.email}",
        body=f"Signed, Ada Author\n\n--- REWORK ---\nagain\n{attachment_line(rework)}",
    )
    other = await _filed(service, by=STRANGER, body=f"Same here\n{attachment_line(shared)}")
    repository = _repository(service)
    repository.calls.clear()

    erased = await service.erase_author(AUTHOR.id, (needle for needle in [AUTHOR.email, AUTHOR.display_name]))

    assert erased.reports == 2
    assert erased.keys == ("bbbbbbbbbbbb.pdf", "cccccccccccc.txt")
    assert repository.calls == ["by_author", "apply", "apply", "urls_named_except"]
    assert repository.actors == [None, None]
    for row in (first, second):
        assert row.user_id is None and row.screenshot_url is None and row.attachment_urls is None
    assert second.title == f"Crash for {ERASED_MARK}"
    assert second.body == f"Signed, {ERASED_MARK}\n\n--- REWORK ---\nagain"
    assert other.user_id == STRANGER.id and other.body.endswith(shared)


async def test_erase_author_without_files_asks_for_no_other_rows() -> None:
    service = _service()
    await _filed(service)
    repository = _repository(service)
    repository.calls.clear()
    erased = await service.erase_author(AUTHOR.id, [AUTHOR.email])
    assert erased.reports == 1 and erased.keys == ()
    assert repository.calls == ["by_author", "apply"]
    nobody = await service.erase_author(STRANGER.id, [])
    assert nobody.reports == 0 and nobody.keys == ()


# --- uploads -----------------------------------------------------------------------------


def test_throttle_upload_raises_the_429_with_its_retry_after() -> None:
    limiter = _limiter(max_hits=1)
    throttle_upload(limiter, AUTHOR.id)
    with pytest.raises(FeedbackThrottledError) as refused:
        throttle_upload(limiter, AUTHOR.id)
    error = refused.value
    assert error.status_code == 429 and str(error) == THROTTLED_DETAIL
    assert 3590 < error.retry_after <= 3600
    assert error.headers == {"Retry-After": str(int(error.retry_after) + 1)}
    throttle_upload(limiter, STRANGER.id)  # per user


async def _upload(
    data: bytes,
    declared: str | None = "image/png",
    *,
    by: FeedbackActor = AUTHOR,
    storage: InMemoryAttachmentStorage | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
    **options: Any,
) -> str:
    return await upload_attachment(
        by,
        Upload(data),
        declared_type=declared,
        filename="screen.png",
        storage=storage or InMemoryAttachmentStorage(),
        policy=POLICY,
        limiter=limiter,
        **options,
    )


async def test_an_upload_is_stored_by_its_bytes_and_answers_its_url() -> None:
    storage, limiter = InMemoryAttachmentStorage(), _limiter()
    picture = png()
    url = await _upload(picture, "image/jpeg", storage=storage, limiter=limiter)  # the bytes decide
    key = POLICY.key_of(url)
    assert key is not None and key.endswith(".png")
    stored, uploader = storage.files[key]
    assert stored == StoredAttachment(data=picture, media_type="image/png", filename="screen.png")
    assert uploader == AUTHOR.id
    assert limiter.count(str(AUTHOR.id)) == 1
    # No limiter: nothing to charge, and the file is stored all the same.
    document = await _upload(PDF, "application/pdf", storage=storage)
    assert POLICY.is_own_url(document) and document.endswith(".pdf")
    assert limiter.count(str(AUTHOR.id)) == 1


async def test_an_upload_refuses_a_demo_before_the_window_and_charges_before_reading() -> None:
    """§2.11: a refused file uses a slot; a spent window reads nothing; a demo charges nothing."""
    limiter = _limiter(max_hits=1)
    with pytest.raises(DemoError) as refused:
        await _upload(png(), by=DEMO, limiter=limiter)
    assert "uploads" in str(refused.value) and limiter.count(str(DEMO.id)) == 0

    with pytest.raises(EmptyUploadError):
        await _upload(b"", limiter=limiter)
    assert limiter.count(str(AUTHOR.id)) == 1

    upload = Upload(png())
    with pytest.raises(FeedbackThrottledError):
        await upload_attachment(
            AUTHOR,
            upload,
            declared_type="image/png",
            filename=None,
            storage=InMemoryAttachmentStorage(),
            policy=POLICY,
            limiter=limiter,
        )
    assert upload.reads == 0


async def test_the_image_guard_and_the_utf8_rule_are_the_apps_choices() -> None:
    cut = png((64, 64))[:40]
    assert await _upload(cut)  # the signature passes…
    with pytest.raises(NotAnImageError):
        await _upload(cut, decode_images=True)  # …the parse does not (kk, ka)
    latin1 = "Débattement".encode("latin-1")
    assert await _upload(latin1, "text/plain")
    with pytest.raises(UnsupportedUploadTypeError):
        await _upload(latin1, "text/plain", require_utf8_text=True)  # KS


# --- downloads ---------------------------------------------------------------------------


async def _stored(storage: InMemoryAttachmentStorage, data: bytes, declared: str, *, filename: str | None) -> str:
    url = await upload_attachment(
        AUTHOR, Upload(data), declared_type=declared, filename=filename, storage=storage, policy=POLICY, limiter=None
    )
    key = POLICY.key_of(url)
    assert key is not None
    return key


async def test_a_picture_is_served_inline_by_its_bytes_with_nosniff() -> None:
    storage = InMemoryAttachmentStorage()
    picture = png()
    key = await _stored(storage, picture, "image/png", filename="Bildschirm – Konto.png")
    found = await download_attachment(STRANGER, key, storage=storage, policy=POLICY)
    assert found.data == picture and found.media_type == "image/png"
    assert found.headers == {
        "Content-Disposition": "inline; filename=\"Bildschirm _ Konto.png\"; filename*=UTF-8''Bildschirm%20%E2%80%93%20Konto.png",
        "X-Content-Type-Options": "nosniff",
    }


async def test_a_pdf_and_text_are_saved_never_shown() -> None:
    storage = InMemoryAttachmentStorage()
    pdf = await _stored(storage, PDF, "application/pdf", filename=None)
    found = await download_attachment(AUTHOR, pdf, storage=storage, policy=POLICY)
    assert found.media_type == "application/pdf"
    assert found.headers["Content-Disposition"].startswith('attachment; filename="attachment.pdf"')

    # keksdose's and kastlan's stores answer the type from the key's extension, with its charset.
    storage.files["0123456789ab.txt"] = (StoredAttachment(TEXT, "text/plain; charset=utf-8", None), AUTHOR.id)
    text = await download_attachment(AUTHOR, "0123456789ab.txt", storage=storage, policy=POLICY)
    assert text.media_type == "text/plain; charset=utf-8"
    assert text.headers["Content-Disposition"].startswith('attachment; filename="attachment.txt"')


async def test_the_bytes_decide_not_the_stored_word() -> None:
    storage = InMemoryAttachmentStorage()
    # Stored as a picture, and it is text: not a picture, and not text either (never said so).
    storage.files["0123456789ab.png"] = (StoredAttachment(b"<script>", "image/png", "x.png"), AUTHOR.id)
    lying = await download_attachment(AUTHOR, "0123456789ab.png", storage=storage, policy=POLICY)
    assert lying.media_type == "application/octet-stream"
    assert lying.headers["Content-Disposition"].startswith("attachment;")
    # Stored as text and not UTF-8: text by default, unknown under Kurvenschmiede's rule.
    storage.files["ba9876543210.txt"] = (StoredAttachment("é".encode("latin-1"), "text/plain", None), AUTHOR.id)
    loose = await download_attachment(AUTHOR, "ba9876543210.txt", storage=storage, policy=POLICY)
    assert loose.media_type == "text/plain; charset=utf-8"
    strict = await download_attachment(
        AUTHOR, "ba9876543210.txt", storage=storage, policy=POLICY, require_utf8_text=True
    )
    assert strict.media_type == "application/octet-stream"


async def test_a_key_without_an_extension_is_served_as_attachment() -> None:
    storage = InMemoryAttachmentStorage()
    storage.files["opaque-key"] = (StoredAttachment(PDF, None, None), AUTHOR.id)
    found = await download_attachment(AUTHOR, "opaque-key", storage=storage, policy=DEFAULT_ATTACHMENT_URL_POLICY)
    assert found.headers["Content-Disposition"].startswith('attachment; filename="attachment";')


@pytest.fixture
def unread() -> Iterator[InMemoryAttachmentStorage]:
    """A storage that fails the test if it is read."""

    class Unread(InMemoryAttachmentStorage):
        async def read(self, key: str, *, viewer: FeedbackActor) -> StoredAttachment | None:
            raise AssertionError("read a key outside the pattern")

    yield Unread()


async def test_a_key_outside_the_pattern_never_reaches_the_store(unread: InMemoryAttachmentStorage) -> None:
    for key in ("../secret.png", "0123456789ab.png\n", "0123456789ab.svg"):
        with pytest.raises(AttachmentNotFoundError) as missing:
            await download_attachment(AUTHOR, key, storage=unread, policy=POLICY)
        assert missing.value.status_code == 404 and str(missing.value) == "Attachment not found"


async def test_a_missing_file_or_one_not_this_viewers_is_a_404() -> None:
    storage = InMemoryAttachmentStorage(uploader_only=True)
    key = await _stored(storage, PDF, "application/pdf", filename="report.pdf")
    assert (await download_attachment(AUTHOR, key, storage=storage, policy=POLICY)).data == PDF
    for viewer, wanted in ((STRANGER, key), (AUTHOR, "ffffffffffff.png")):
        with pytest.raises(AttachmentNotFoundError):
            await download_attachment(viewer, wanted, storage=storage, policy=POLICY)


def test_new_feedback_has_no_files_by_default() -> None:
    new = NewFeedback(
        user_id=1, title="t", body="", category=FeedbackCategory.BUG, status=FeedbackStatus.OPEN, context=None
    )
    assert new.screenshot_url is None and new.attachment_urls is None and new.crash_fingerprint is None


async def test_erase_author_takes_the_person_out_of_comments_votes_and_replies() -> None:
    """§9.12: comments scrubbed and un-named (hidden ones too), votes deleted, an erased
    admin's replies un-named and scrubbed, ``hidden_by`` cleared; the counts answered."""
    from eifi1_server_kit.feedback import FeedbackCommentCreate, FeedbackCommentHide, FeedbackReplyCreate
    from eifi1_server_kit.feedback.testing import (
        InMemoryCommentStore,
        InMemoryPeopleLookup,
        InMemoryReplyStore,
        InMemoryVoteStore,
    )

    comments, votes, replies = InMemoryCommentStore(), InMemoryVoteStore(), InMemoryReplyStore()
    repository = InMemoryFeedbackRepository(emails=EMAILS, votes=votes, comments=comments)
    service = _service(
        repository,
        comments=comments,
        votes=votes,
        replies=replies,
        people=InMemoryPeopleLookup(),
        public_viewer=lambda viewer: True,
    )
    row = await _filed(service, by=STRANGER)
    said = await service.comment(AUTHOR, row.id, FeedbackCommentCreate(body=f"Me too, {AUTHOR.email}"))
    hidden = await service.comment(AUTHOR, row.id, FeedbackCommentCreate(body="rude"))
    kept = await service.comment(STRANGER, row.id, FeedbackCommentCreate(body="Thanks"))
    await service.hide_comment(ADMIN, row.id, hidden.id, FeedbackCommentHide(hidden=True))
    await service.vote(AUTHOR, row.id)
    await service.reply(ADMIN, row.id, FeedbackReplyCreate(body=f"Answered by {ADMIN.email}"))

    erased = await service.erase_author(AUTHOR.id, [AUTHOR.email])
    assert (erased.reports, erased.comments, erased.votes, erased.replies) == (0, 2, 1, 0)
    assert comments.rows[said.id].user_id is None and comments.rows[said.id].body == f"Me too, {ERASED_MARK}"
    assert comments.rows[hidden.id].user_id is None and comments.rows[kept.id].user_id == STRANGER.id
    assert set(votes.rows) == {(row.id, STRANGER.id)}

    admin = await service.erase_author(ADMIN.id, [ADMIN.email])
    assert (admin.comments, admin.votes, admin.replies) == (0, 0, 1)
    [reply] = replies.rows.values()
    assert reply.author_id is None and reply.body == f"Answered by {ERASED_MARK}"
    assert comments.rows[hidden.id].hidden_by is None and comments.rows[hidden.id].hidden_at is not None
    # 0.10's answer, where the app gives no stores.
    assert await _service().erase_author(AUTHOR.id, []) == ErasedFeedback(reports=0, keys=())
