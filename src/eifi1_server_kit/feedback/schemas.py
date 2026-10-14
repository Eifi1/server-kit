"""The feedback contract's wire shapes (§3.1, §3.4, §3.6).

Lifted from keksdose ``backend/keksdose/domain/schemas/feedback.py`` (cited per class).
Every class is meant to be SUBCLASSED by an app — to narrow the attachment key, to add
response fields (kastlan's ``company_id``), to tighten a cap::

    class FeedbackCreate(kit.FeedbackCreate):
        attachment_url_policy = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)

    class FeedbackResponse(EntityResponseMixin, kit.FeedbackResponse):
        ...  # kastlan's identity block (company_id) comes with EntityResponseMixin

The knobs are class variables, read through ``cls`` by the validators, so a subclass
changes them without re-declaring a validator.

What changed against keksdose, all by the contract or the apps' reviews:

* ``screenshot_url`` is checked like ``attachment_urls`` (§3.4 MUST — keksdose left it a
  free string);
* create and update FORBID unknown fields (Kurvenschmiede: a stale ``note`` or
  ``page_path`` is a 422, not silently dropped); the crash payload still ignores them —
  it must never answer a crashed page with an error;
* ``context`` is capped at :data:`~eifi1_server_kit.feedback.context.MAX_CONTEXT_SIZE`
  and ``body`` at :data:`MAX_BODY_LENGTH` (Kurvenschmiede's caps, recommended for all).

Round 0.38 (0.11, §9.3, §9.7–§9.9): the report gains optional public-feedback fields;
``outcome`` is frozen (a PATCH naming it is a 422 "outcome is read-only; post a reply");
replies, comments and votes have their own shapes, and the public list and detail are
SEPARATE models (:class:`FeedbackPublicListItem`, :class:`FeedbackPublicResponse`), so a
field added to :class:`FeedbackResponse` later never reaches the public by default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.context import MAX_CONTEXT_SIZE, context_size
from eifi1_server_kit.feedback.enums import FeedbackCategory, FeedbackStatus
from eifi1_server_kit.feedback.errors import FeedbackValidationError
from eifi1_server_kit.feedback.public import COMMENT_MAX, REPLY_MAX

#: How many files one report carries besides its one screenshot (keksdose
#: ``schemas/feedback.py:14`` ``MAX_ATTACHMENT_URLS``; the kit's dialog stops at it).
MAX_ATTACHMENT_URLS = 5
#: ``feedback.title`` is ``String(255)`` in every app.
MAX_TITLE_LENGTH = 255
#: A report's text and every rework appended to it: generous, because each rework adds to
#: it; bounded, because it is a column somebody else's request writes (Kurvenschmiede
#: ``schemas/feedback.py:61`` ``MAX_BODY``).
MAX_BODY_LENGTH = 50_000
#: keksdose's ``screenshot_url`` column is ``String(500)``.
MAX_SCREENSHOT_URL_LENGTH = 500

#: The columns a row cannot be without: an explicit ``null`` for one is refused rather
#: than written (keksdose ``schemas/feedback.py:80`` ``_NOT_NULLABLE``). ``public`` joined
#: in 0.11 (§9.3: NOT NULL). ``outcome`` is not here: since 0.11 it is refused whole.
NOT_NULLABLE_FIELDS: tuple[str, ...] = ("status", "title", "body", "category", "public")

#: Why a PATCH naming ``outcome`` is refused (§9.7): the answer is a list of replies now.
OUTCOME_READ_ONLY = "outcome is read-only; post a reply"

#: How the public list may be sorted (§9.6): ``newest`` (the default), ``votes`` (most,
#: then newest) or ``updated``.
PublicSort = Literal["newest", "votes", "updated"]


class FeedbackCreate(BaseModel):
    """``POST /feedback`` (keksdose ``schemas/feedback.py:25``).

    The client uploads every file first, so a row never names a file that did not arrive;
    both URL fields must be this app's own (:attr:`attachment_url_policy`). CRASH is
    refused by :func:`~eifi1_server_kit.feedback.reject_manual_crash` in the service, not
    here, so the 422 carries a sentence rather than a validation list (as keksdose does).
    """

    model_config = ConfigDict(extra="forbid")

    #: Which URLs may be named. Narrow it in the app's subclass.
    attachment_url_policy: ClassVar[AttachmentUrlPolicy] = DEFAULT_ATTACHMENT_URL_POLICY
    #: The cap on ``context`` (``None`` = uncapped).
    max_context_size: ClassVar[int | None] = MAX_CONTEXT_SIZE

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    #: Optional (Marcel, 2026-09-27): a title alone is a complete report. Stored as "",
    #: never NULL; whitespace-only counts as none.
    body: str = Field(default="", max_length=MAX_BODY_LENGTH)
    category: FeedbackCategory = FeedbackCategory.OTHER
    context: dict[str, Any] | None = None
    #: The ONE captured screenshot (keksdose dev #578), or none.
    screenshot_url: str | None = Field(default=None, max_length=MAX_SCREENSHOT_URL_LENGTH)
    #: Every other file, in the order added; de-duplicated, order kept.
    attachment_urls: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENT_URLS)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        # With the body optional the title is the whole report (keksdose :60-67).
        if not value.strip():
            raise FeedbackValidationError("title cannot be blank")
        return value

    @field_validator("body")
    @classmethod
    def _blank_body_is_empty(cls, value: str) -> str:
        return value if value.strip() else ""

    @field_validator("context")
    @classmethod
    def _context_is_small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        cap = cls.max_context_size
        if cap is not None and context_size(value) > cap:
            raise FeedbackValidationError(f"context holds at most {cap} characters of JSON")
        return value

    @field_validator("screenshot_url")
    @classmethod
    def _screenshot_url_is_ours(cls, value: str | None) -> str | None:
        return None if value is None else cls.attachment_url_policy.validate_url(value)

    @field_validator("attachment_urls")
    @classmethod
    def _attachment_urls_are_ours(cls, urls: list[str]) -> list[str]:
        return cls.attachment_url_policy.validate_urls(urls)


class FeedbackUpdate(BaseModel):
    """``PATCH /feedback/{id}`` (keksdose ``schemas/feedback.py:83``).

    Every field is optional because this is a PATCH: ``None`` means "not sent", never "set
    it to null". Apply ``model_dump(exclude_unset=True)`` through
    :func:`~eifi1_server_kit.feedback.plan_update`.
    """

    model_config = ConfigDict(extra="forbid")

    status: FeedbackStatus | None = None
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    body: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    category: FeedbackCategory | None = None
    #: Frozen since 0.11 (§9.7): kept so an app's subclass still names it, but any PATCH
    #: that names it, ``null`` included, is a 422 :data:`OUTCOME_READ_ONLY`.
    outcome: str | None = None
    #: "Visible to others" (§9.3): an admin any time; the author while the report is open,
    #: or at any status on a ``filed_private`` report.
    public: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_explicit_nulls(cls, data: object) -> object:
        """The explicit-null guard (keksdose ``schemas/feedback.py:90-114``).

        An explicit ``{"title": null}`` was assigned straight onto a NOT NULL column and
        came back as a 500 from the flush. A blank title is refused like the create route
        refuses it; a blank body is stored as "". ``outcome`` in any form is refused
        (§9.7): nothing writes it any more.
        """
        if not isinstance(data, dict):
            return data
        if "outcome" in data:
            raise FeedbackValidationError(OUTCOME_READ_ONLY)
        for name in NOT_NULLABLE_FIELDS:
            if name in data and data[name] is None:
                raise FeedbackValidationError(f"{name} cannot be null")
        title = data.get("title")
        if isinstance(title, str) and not title.strip():
            raise FeedbackValidationError("title cannot be blank")
        body = data.get("body")
        if isinstance(body, str) and not body.strip():
            data = {**data, "body": ""}
        return data


class FeedbackPublicReplyResponse(BaseModel):
    """One reply as the public detail shows it (§9.10): never ``status_from``."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: int
    #: Others: the first name; an admin: the full name; ``None``: the app's word for its
    #: admin ("Admin") — a loop's reply, a migrated outcome, an erased admin.
    author_name: str | None = None
    body: str
    #: The status the reply moved the report to; ``None``: no status change.
    status_to: FeedbackStatus | None = None
    created_at: datetime


class FeedbackReplyResponse(FeedbackPublicReplyResponse):
    """One reply on the admin's and the author's report (§9.7)."""

    #: The status before the move: the admin view only (``None`` elsewhere).
    status_from: FeedbackStatus | None = None


class FeedbackResponse(BaseModel):
    """One report on the wire (contract §3.1; keksdose ``schemas/feedback.py:117``).

    Extra response fields are allowed and ignored by the kit — subclass to add them.
    """

    model_config = ConfigDict(from_attributes=True, json_schema_serialization_defaults_required=True)

    id: int
    #: NULL = the author's account was erased; the client renders ``<deleted user>``.
    user_id: int | None
    #: Joined for the admin list only; ``None`` on ``/my``, create and update.
    user_email: str | None = None
    title: str
    #: ``""`` when none; rework rounds are appended here.
    body: str
    category: FeedbackCategory
    status: FeedbackStatus
    context: dict[str, Any] | None
    screenshot_url: str | None
    #: ``None`` = nothing attached (and what an older server answers by omission).
    attachment_urls: list[str] | None = None
    #: The team's answer; ``None`` = none written.
    outcome: str | None
    #: Set on ENTERING DONE / WONT_DO, cleared on leaving them.
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Public feedback (§9.3), all optional: an app without public parts sends them null.
    #: The admin: the full name; the author: their own full name. ``None``: no name.
    author_name: str | None = None
    #: The viewer wrote it.
    mine: bool | None = None
    #: "Visible to others"; a CRASH is never public, whatever this says.
    public: bool | None = None
    #: Made public by the round-0.38 migration: its author may switch it off at any status.
    filed_private: bool | None = None
    #: The people who want it (the author's own vote included) and whether the viewer is one.
    votes: int | None = None
    voted: bool | None = None
    #: The comments: an admin counts the hidden ones too.
    comment_count: int | None = None
    #: The answers, oldest first; ``status_from`` for an admin only.
    replies: list[FeedbackReplyResponse] | None = None


def _trimmed(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


class FeedbackReplyCreate(BaseModel):
    """``POST /feedback/{id}/replies`` (§9.7), admin only: the answer, 1–:data:`REPLY_MAX`
    characters after trimming, and the status it moves the report to, if any."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=REPLY_MAX)
    status: FeedbackStatus | None = None

    @field_validator("body", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        return _trimmed(value)


class FeedbackCommentCreate(BaseModel):
    """``POST /feedback/{id}/comments`` (§9.8): plain text, 1–:data:`COMMENT_MAX`
    characters after trimming, no files."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=COMMENT_MAX)

    @field_validator("body", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        return _trimmed(value)


class FeedbackCommentHide(BaseModel):
    """``PATCH /feedback/{id}/comments/{comment_id}`` (§9.8), admin only."""

    model_config = ConfigDict(extra="forbid")

    hidden: bool


class FeedbackCommentResponse(BaseModel):
    """One comment (§9.8, §9.10): the same shape for every viewer."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: int
    #: Others: the first name; the writer and an admin: the full name; ``None``: "A user"
    #: (an erased writer, an incomplete name, or a hidden comment for others).
    author_name: str | None = None
    mine: bool = False
    #: keksdose's migrated comments whose writer is an admin.
    staff: bool = False
    #: ``None`` when hidden, but for its writer and an admin.
    body: str | None
    hidden: bool = False
    created_at: datetime


class FeedbackVoteResponse(BaseModel):
    """``PUT`` / ``DELETE /feedback/{id}/vote`` (§9.6): the count after, and the viewer's."""

    votes: int
    voted: bool


class FeedbackPublicListItem(BaseModel):
    """One row of the public list (§9.3, §9.9): titles only, no body — a SEPARATE model,
    so nothing added to :class:`FeedbackResponse` reaches the public by default."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: int
    title: str
    category: FeedbackCategory
    status: FeedbackStatus
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    #: The first name, or ``None`` ("A user").
    author_name: str | None = None
    mine: bool = False
    votes: int = 0
    voted: bool = False
    #: Visible comments only.
    comment_count: int = 0


class FeedbackPublicResponse(FeedbackPublicListItem):
    """The public detail (§9.3, §9.9): the public body and the replies; the comments are
    fetched beside it (``GET /feedback/{id}/comments``)."""

    #: :func:`~eifi1_server_kit.feedback.public_body`: no file lines, no attachment URL.
    body: str
    replies: list[FeedbackPublicReplyResponse] = Field(default_factory=list)


class FeedbackAttachmentResponse(BaseModel):
    """``POST /feedback/attachments`` → ``{"url": "/api/v1/feedback/attachments/<key>"}``."""

    url: str


def _field_cap(model: type[BaseModel], name: str | None) -> int | None:
    """The ``max_length`` declared on a field, read off its annotated-types metadata."""
    info = model.model_fields.get(name or "")
    metadata = info.metadata if info is not None else []
    return next((m.max_length for m in metadata if hasattr(m, "max_length")), None)


class CrashReportCreate(BaseModel):
    """``POST /feedback/crash`` — a crash the client's error boundary files (keksdose
    ``schemas/feedback.py:152``, feedback #160), with the contract's ``origin`` (≤ 200) and
    ``environment`` (≤ 20), §3.6.

    Every string is capped, and anything over its cap is TRUNCATED, never refused: the
    sender is a page that has just crashed, and a 422 would be a crash nobody ever sees.
    Unknown fields are ignored for the same reason.
    """

    name: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    stack: str | None = Field(default=None, max_length=8000)
    component_stack: str | None = Field(default=None, max_length=8000)
    #: "app": the whole shell went down; "page": one route's subtree did.
    boundary: Literal["app", "page"] = "page"
    url: str | None = Field(default=None, max_length=2000)
    route: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=50)
    viewport: str | None = Field(default=None, max_length=50)
    ua: str | None = Field(default=None, max_length=500)
    #: WHICH copy of the app crashed — without them a dev crash reads as production.
    origin: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, max_length=20)
    online: bool | None = None
    occurred_at: datetime | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _repair_client_text(cls, value: object, info: ValidationInfo) -> object:
        """Make every inbound string storable, and trim it rather than refuse it.

        keksdose ``schemas/feedback.py:184-220``, verbatim in effect: a browser's
        ``JSON.stringify`` writes a lone surrogate as an escape ``json.loads`` accepts, and
        the ``str`` it yields is not UTF-8 encodable — Pydantic would refuse it (a 422), and
        past that the fingerprint's encode and a Postgres ``text`` INSERT (which refuses
        NUL too) would fail. ``mode="before"`` is the only hook ahead of Pydantic's own
        checks. The cap is read back off the field, so there is no second table of limits.
        """
        if not isinstance(value, str):
            return value
        cleaned = value.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
        cap = _field_cap(cls, info.field_name)
        if cap is not None and len(cleaned) > cap:
            cleaned = cleaned[: cap - 1] + "…"
        return cleaned


class CrashReportResponse(BaseModel):
    """Always a 202 (keksdose ``schemas/feedback.py:223``): stored, folded or suppressed is
    a flag, never an error status — the caller is a crashed page."""

    stored: bool
    duplicate: bool = False
    feedback_id: int | None = None
    #: The fingerprint's first 8 characters, quotable from the fallback UI.
    reference: str | None = None
