"""The translation review's wire shapes — what the kit's ``TranslationReviewPanel`` reads and writes.

Lifted from keksdose ``backend/keksdose/domain/schemas/translations.py:12-102``. The admin
grant shapes (``AdminReviewerRequest`` / ``Response``) stay app-side: they carry the
app's own role enum.

The four request bodies refuse unknown fields (``extra="forbid"``, 0.10; Kurvenschmiede's
rule, server-services §6.2.7): a misspelt ``suggestion`` is a 422, not a verdict saved
without it.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

#: The longest string in any bundle is ~3.5k characters; ten times that leaves room
#: without accepting a megabyte (keksdose ``translations.py:12-14``).
TEXT_MAX = 40_000
#: One request may judge a whole namespace at once ("approve these 120 rows").
BATCH_MAX = 2_000
#: A reviewer's note.
NOTE_MAX = 4_000


class TranslationVerdict(str, enum.Enum):
    """What a reviewer said about one string in one locale (keksdose ``models/translation_review.py:13``).

    Two values, not a scale; "not looked at yet" is the ABSENCE of a row, and "changed since
    it was looked at" is derived by comparing ``text`` with the bundle.
    """

    APPROVED = "APPROVED"
    NEEDS_CHANGE = "NEEDS_CHANGE"


class TranslationReviewOut(BaseModel):
    """One stored verdict."""

    locale: str
    key: str
    #: The wording that was judged — compare with the bundle's current one.
    text: str
    reference_text: str | None
    verdict: TranslationVerdict
    note: str | None
    suggestion: str | None
    #: The reviewer's name in the READER's order (``auth.full_name`` with the reader's
    #: locale; server-services §6.2.5); NULL once their account has been erased.
    reviewer_name: str | None
    reviewed_at: datetime
    #: The name's two parts, for a page that writes them itself (auth §3.2: every API that
    #: returns a person returns both); NULL once the account has been erased (0.10).
    reviewer_first_name: str | None = None
    reviewer_last_name: str | None = None


class TranslationReviewsResponse(BaseModel):
    """``GET /translations/reviews``."""

    #: The locales the CALLER may review, in the UI's order. Every locale for an admin.
    locales: list[str]
    #: The areas the caller is limited to (``["legal"]``; ``["kit"]`` for a review token),
    #: NULL for every area.
    areas: list[str] | None = None
    #: Every stored verdict in those locales. Keys absent here are unreviewed.
    reviews: list[TranslationReviewOut]


class TranslationReviewWrite(BaseModel):
    """One verdict to store (``PUT /translations/reviews`` item)."""

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=2, max_length=10)
    key: str = Field(min_length=1, max_length=255)
    text: str = Field(max_length=TEXT_MAX)
    reference_text: str | None = Field(default=None, max_length=TEXT_MAX)
    verdict: TranslationVerdict
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    suggestion: str | None = Field(default=None, max_length=TEXT_MAX)


class TranslationReviewBatch(BaseModel):
    """``PUT /translations/reviews`` body."""

    model_config = ConfigDict(extra="forbid")

    items: list[TranslationReviewWrite] = Field(min_length=1, max_length=BATCH_MAX)


class TranslationReviewKey(BaseModel):
    """One (locale, key) to clear."""

    model_config = ConfigDict(extra="forbid")

    locale: str = Field(min_length=2, max_length=10)
    key: str = Field(min_length=1, max_length=255)


class TranslationReviewClear(BaseModel):
    """``POST /translations/reviews/clear`` body — back to "unreviewed", the undo for a
    verdict given by mistake."""

    model_config = ConfigDict(extra="forbid")

    items: list[TranslationReviewKey] = Field(min_length=1, max_length=BATCH_MAX)


class TranslationReviewWriteResponse(BaseModel):
    reviews: list[TranslationReviewOut]


class TranslationReviewClearResponse(BaseModel):
    cleared: int


class TranslationReviewTokenResponse(BaseModel):
    """A review token for the kit's showcase — shown ONCE; only its hash is kept."""

    token: str
    expires_at: datetime


class TranslationReviewTokenRevokeResponse(BaseModel):
    revoked: int
