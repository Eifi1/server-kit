"""The wire shapes (contract §3.1, §3.4, §3.6), with keksdose's schema tests ported."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from pydantic import ValidationError

from eifi1_server_kit.feedback import (
    MAX_ATTACHMENT_URLS,
    MAX_BODY_LENGTH,
    MAX_CONTEXT_SIZE,
    UUID32_SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    CrashReportCreate,
    CrashReportResponse,
    FeedbackAttachmentResponse,
    FeedbackCategory,
    FeedbackCreate,
    FeedbackResponse,
    FeedbackStatus,
    FeedbackUpdate,
)

URL_A = "/api/v1/feedback/attachments/aaaaaaaaaaaa.png"
URL_B = "/api/v1/feedback/attachments/bbbbbbbbbbbb.pdf"
SHOT = "/api/v1/feedback/attachments/cccccccccccc.webp"


# ── FeedbackCreate ──────────────────────────────────────────────────────────


def test_a_title_alone_is_a_complete_report() -> None:
    """keksdose ``test_feedback.py:379``: omitted, empty and whitespace-only bodies are all
    "" (never NULL); a blank TITLE is refused, since it is now the whole report."""
    for extra in ({}, {"body": ""}, {"body": "  \n "}):
        assert FeedbackCreate.model_validate({"title": "Dark mode please", **extra}).body == ""
    for title in ("", "   "):
        with pytest.raises(ValidationError):
            FeedbackCreate(title=title)
    created = FeedbackCreate(title="t")
    assert created.category is FeedbackCategory.OTHER
    assert created.attachment_urls == [] and created.screenshot_url is None and created.context is None


def test_attachment_urls_are_capped_and_deduplicated() -> None:
    """keksdose ``test_feedback_service.py:392``: the cap, and content-addressed keys — the
    same picture pasted twice is one attachment, in place."""
    too_many = [f"/api/v1/feedback/attachments/{i:012x}.png" for i in range(MAX_ATTACHMENT_URLS + 1)]
    with pytest.raises(ValidationError):
        FeedbackCreate(title="t", attachment_urls=too_many)
    assert FeedbackCreate(title="t", attachment_urls=[URL_A, URL_B, URL_A]).attachment_urls == [URL_A, URL_B]


@pytest.mark.parametrize(
    "bad",
    ["https://evil.example/x.png", "/api/v1/feedback/attachments/../../etc/passwd", ""],
)
def test_both_url_fields_refuse_anything_but_our_own_route(bad: str) -> None:
    """keksdose ``test_feedback_service.py:378`` for ``attachment_urls``, and the contract's
    MUST for ``screenshot_url`` (§3.4) — keksdose left that one unvalidated."""
    with pytest.raises(ValidationError):
        FeedbackCreate(title="t", attachment_urls=[bad])
    with pytest.raises(ValidationError, match="not a feedback attachment URL"):
        FeedbackCreate(title="t", screenshot_url=bad)


def test_the_screenshot_keeps_its_own_field() -> None:
    """keksdose ``test_feedback_service.py:404`` (dev #578): two URLs in, two kept, in order,
    with the screenshot in its own field."""
    payload = FeedbackCreate(title="two photos", screenshot_url=SHOT, attachment_urls=[URL_A, URL_B])
    assert payload.screenshot_url == SHOT
    assert payload.attachment_urls == [URL_A, URL_B]


def test_unknown_fields_are_refused_on_create_and_update() -> None:
    """Kurvenschmiede: a stale ``note`` or ``page_path`` is a 422, not silently dropped."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FeedbackCreate.model_validate({"title": "t", "page_path": "/x"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FeedbackUpdate.model_validate({"note": "still broken"})


def test_the_context_is_capped() -> None:
    """Kurvenschmiede ``schemas/feedback.py:144``: the ten keys fit in a fraction of 8 KB."""
    ten_keys = {
        "url": "https://keksdose.app/reports?q=x",
        "route": "/reports",
        "origin": "https://keksdose.app",
        "environment": "prod",
        "user_id": 7,
        "user_email": "a@b.ch",
        "user_display_name": "A",
        "viewport": "406x816",
        "ua": "Mozilla/5.0",
        "version": "0.4.61",
    }
    assert FeedbackCreate(title="t", context=ten_keys).context == ten_keys
    with pytest.raises(ValidationError, match="context holds at most"):
        FeedbackCreate(title="t", context={"blob": "x" * MAX_CONTEXT_SIZE})
    # Measured as Kurvenschmiede measures it: characters of json.dumps(ensure_ascii=False).
    exact = {"k": "x" * (MAX_CONTEXT_SIZE - len(json.dumps({"k": ""})))}
    assert FeedbackCreate(title="t", context=exact).context == exact


def test_the_body_is_capped() -> None:
    assert len(FeedbackCreate(title="t", body="x" * MAX_BODY_LENGTH).body) == MAX_BODY_LENGTH
    with pytest.raises(ValidationError):
        FeedbackCreate(title="t", body="x" * (MAX_BODY_LENGTH + 1))
    with pytest.raises(ValidationError):
        FeedbackUpdate(body="x" * (MAX_BODY_LENGTH + 1))


def test_an_app_narrows_or_loosens_the_rules_in_a_subclass() -> None:
    """The knobs are class variables, read through ``cls`` (kastlan's key, no cap)."""

    class KastlanCreate(FeedbackCreate):
        attachment_url_policy: ClassVar[AttachmentUrlPolicy] = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)
        max_context_size: ClassVar[int | None] = None

    kastlan_url = f"/api/v1/feedback/attachments/{'0' * 32}_{'a' * 12}.png"
    payload = KastlanCreate(title="t", screenshot_url=kastlan_url, attachment_urls=[kastlan_url])
    assert payload.attachment_urls == [kastlan_url]
    with pytest.raises(ValidationError):
        KastlanCreate(title="t", attachment_urls=[URL_A])
    assert KastlanCreate(title="t", context={"blob": "x" * 20_000}).context is not None
    # The base class is untouched by the subclass.
    with pytest.raises(ValidationError):
        FeedbackCreate(title="t", context={"blob": "x" * 20_000})


# ── FeedbackUpdate ──────────────────────────────────────────────────────────


def test_a_patch_cannot_null_or_blank_the_columns_a_row_needs() -> None:
    """keksdose ``test_feedback.py:263``: an explicit null for a NOT NULL column was a 500
    from the flush; it is a 422 naming the field. ``outcome: null`` clears a real field."""
    for name in ("title", "body", "status", "category"):
        with pytest.raises(ValidationError, match=f"{name} cannot be null"):
            FeedbackUpdate.model_validate({name: None})
    with pytest.raises(ValidationError, match="title cannot be blank"):
        FeedbackUpdate.model_validate({"title": "   "})
    # The body is optional: blanking it is a real edit and stores "".
    blank = FeedbackUpdate.model_validate({"body": "   "})
    assert blank.body == "" and blank.model_dump(exclude_unset=True) == {"body": ""}
    cleared = FeedbackUpdate.model_validate({"outcome": None})
    assert cleared.model_dump(exclude_unset=True) == {"outcome": None}
    # None means "not sent": nothing set, nothing dumped.
    assert FeedbackUpdate().model_dump(exclude_unset=True) == {}


def test_an_update_parses_the_contract_values() -> None:
    update = FeedbackUpdate.model_validate({"status": "NEEDS_LIVE_TEST", "category": "IDEA"})
    assert update.status is FeedbackStatus.NEEDS_LIVE_TEST and update.category is FeedbackCategory.IDEA
    with pytest.raises(ValidationError):
        FeedbackUpdate.model_validate({"status": "ARCHIVED"})
    # A non-dict input reaches pydantic's own refusal, not the null guard.
    with pytest.raises(ValidationError):
        FeedbackUpdate.model_validate("nonsense")


# ── FeedbackResponse ────────────────────────────────────────────────────────


class _Row:
    """An ORM-ish row (``from_attributes``)."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


def _row(**overrides: object) -> _Row:
    now = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "id": 412,
        "user_id": None,
        "title": "Chart jumps on save",
        "body": "",
        "category": FeedbackCategory.BUG,
        "status": FeedbackStatus.OPEN,
        "context": None,
        "screenshot_url": None,
        "attachment_urls": None,
        "outcome": None,
        "resolved_at": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return _Row(**values)


def test_the_response_reads_a_row_and_keeps_its_nulls() -> None:
    """keksdose ``schemas/feedback.py:117``: ``user_id`` null = an erased author; no attachment
    is null, never ``[]``; ``user_email`` only where the admin list joined it."""
    out = FeedbackResponse.model_validate(_row())
    dumped = out.model_dump(mode="json")
    assert dumped["user_id"] is None and dumped["user_email"] is None
    assert dumped["attachment_urls"] is None and dumped["outcome"] is None
    assert dumped["category"] == "BUG" and dumped["status"] == "OPEN"
    assert dumped["created_at"] == "2026-10-04T09:12:00Z"


def test_an_app_adds_response_fields_in_a_subclass() -> None:
    """kastlan's ``FeedbackResponse`` adds ``company_id`` and ``user_name`` (§3.1: extra fields
    are allowed and ignored by the kit)."""

    class KastlanResponse(FeedbackResponse):
        company_id: int | None = None
        user_name: str | None = None

    out = KastlanResponse.model_validate(_row(company_id=3, user_id=7))
    out.user_name = "Erika Muster"
    assert out.model_dump()["company_id"] == 3 and out.user_name == "Erika Muster"


# ── crash payload ───────────────────────────────────────────────────────────


def test_crash_payload_trims_an_oversized_string_instead_of_rejecting_it() -> None:
    """keksdose ``test_feedback_service.py:297``: a refused crash report is a crash nobody
    sees, so the caps truncate; inside its cap a string passes untouched."""
    payload = CrashReportCreate(
        message="x" * 5000, stack="y" * 9000, ua="u" * 900, origin="o" * 300, environment="e" * 30
    )
    assert len(payload.message) == 2000 and payload.message.endswith("…")
    assert len(payload.stack or "") == 8000
    assert len(payload.ua or "") == 500
    assert len(payload.origin or "") == 200
    assert len(payload.environment or "") == 20
    assert CrashReportCreate(message="fx.getRate is not a function").message == "fx.getRate is not a function"


def test_crash_payload_repairs_a_lone_surrogate_and_a_nul() -> None:
    """keksdose ``test_feedback_service.py:274``: constructing the payload at all is half the
    assertion — Pydantic refuses an unpaired surrogate unless the before-validator gets in first."""
    payload = CrashReportCreate(message="boom \ud800 tail", name="TypeError\udfff", route="/a\x00b")
    assert payload.message == "boom ? tail" and payload.name == "TypeError?"
    assert payload.route == "/ab"
    payload.message.encode("utf-8")


def test_crash_payload_survives_the_raw_json_a_browser_sends() -> None:
    """keksdose ``test_feedback_crash.py:216``: ``ensure_ascii`` escapes the surrogate, so the
    wire bytes are the ASCII ``\\ud800`` a real browser sends."""
    raw = json.dumps({"message": "boom \ud800 tail", "name": "TypeError\udfff", "boundary": "app"})
    # FastAPI parses a JSON body with `json.loads`, then validates the dict — and that is the
    # path to pin: Pydantic's own JSON parser refuses a lone surrogate escape outright, so an
    # app must never switch a crash route to `model_validate_json`.
    payload = CrashReportCreate.model_validate(json.loads(raw.encode("ascii")))
    with pytest.raises(ValidationError, match="Invalid JSON"):
        CrashReportCreate.model_validate_json(raw.encode("ascii"))
    assert "\ud800" not in payload.message and payload.boundary == "app"


def test_crash_payload_ignores_unknown_fields_and_keeps_the_contract_ones() -> None:
    """Never a 422 at a crashed page — but the contract's ``origin`` / ``environment`` stay (§3.6)."""
    payload = CrashReportCreate.model_validate(
        {
            "message": "x",
            "origin": "https://dev.keksdose.app",
            "environment": "dev",
            "occurred_at": "2026-10-04T09:12:00Z",
            "something_new": 1,
        }
    )
    assert payload.origin == "https://dev.keksdose.app" and payload.environment == "dev"
    assert payload.occurred_at == datetime(2026, 10, 4, 9, 12, tzinfo=UTC)
    assert payload.boundary == "page" and payload.online is None
    with pytest.raises(ValidationError):
        CrashReportCreate(message="")


def test_the_small_response_shapes() -> None:
    assert CrashReportResponse(stored=False).model_dump() == {
        "stored": False,
        "duplicate": False,
        "feedback_id": None,
        "reference": None,
    }
    assert FeedbackAttachmentResponse(url=URL_A).url == URL_A


def test_a_plain_assignment_overrides_the_policy_too() -> None:
    """The README's form — no ``ClassVar`` annotation in the app's subclass."""

    class Create(FeedbackCreate):
        attachment_url_policy = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)

    own = f"/api/v1/feedback/attachments/{'0' * 32}_{'a' * 12}.png"
    assert Create(title="t", screenshot_url=own).screenshot_url == own
    with pytest.raises(ValidationError):
        Create(title="t", screenshot_url=URL_A)
