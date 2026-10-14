"""The ports the translation review runs on: who reviews, the rows, and the store.

Layer 2 (``docs/server-services-harmonization.md`` §6.2.2–§6.2.3 in ``Eifi1/ui-kit``). Each
app implements :class:`TranslationReviewStore` over its own session and builds a
:class:`TranslationReviewer` from its own ``User`` — with the grant layer 1's
:func:`~eifi1_server_kit.translation_review.review_grant` gives it, since the roles are
the app's. The kit never sees the app's models, and no SQLAlchemy or FastAPI is imported
here.

The store is async, as the three apps' sessions are, and runs in the caller's
transaction: **every write flushes and none commits** (§6.2.9). An app's row is read
through :class:`TranslationReviewRow` as it is (kastlan's and keksdose's plain columns,
Kurvenschmiede's sealed ones), and every write goes through the store.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eifi1_server_kit.translation_review.schemas import TranslationVerdict
from eifi1_server_kit.translation_review.scope import ReviewGrant

__all__ = [
    "NewVerdict",
    "ReviewerName",
    "TranslationReviewRow",
    "TranslationReviewStore",
    "TranslationReviewer",
]


@dataclass(frozen=True, slots=True)
class TranslationReviewer:
    """Who is reviewing (§6.2.2): the viewer of a list, the reviewer of a save or a clear.

    One app dependency builds it from the app's own ``get_current_user``. ``grant`` is the
    app's ``review_grant(…)``. keksdose's review token is this same view with
    ``kit_only=True``: its list is the area ``kit``, a write naming any other key is
    refused whole, and a token on a grant with an area limit is refused its list.
    """

    #: The ``reviewer_id`` of what they write.
    id: int
    first_name: str | None
    last_name: str | None
    #: The reader's language: the order of ``reviewer_name`` (§6.2.5).
    locale: str | None
    #: The app's ``review_grant(…)``: locales and areas.
    grant: ReviewGrant
    #: Signed in with a review token (keksdose): the kit's keys only.
    kit_only: bool = False


class TranslationReviewRow(Protocol):
    """A verdict as the kit reads it (§6.2.3): read-only members, so an app's mapped row
    satisfies it as it is. Its id never leaves the server, so there is none here."""

    @property
    def locale(self) -> str: ...
    @property
    def key(self) -> str: ...
    @property
    def text(self) -> str: ...
    @property
    def reference_text(self) -> str | None: ...
    @property
    def verdict(self) -> TranslationVerdict | str: ...
    @property
    def note(self) -> str | None: ...
    @property
    def suggestion(self) -> str | None: ...
    @property
    def reviewed_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ReviewerName:
    """A reviewer's current name, as the list's outer join reads it."""

    first_name: str
    last_name: str


@dataclass(frozen=True, slots=True)
class NewVerdict:
    """One ``(locale, key)``'s row as a save leaves it (§6.2.3): the store finds or adds the
    row and assigns every field."""

    locale: str
    key: str
    text: str
    reference_text: str | None
    verdict: TranslationVerdict
    #: Blank is ``None``.
    note: str | None
    #: ``None`` unless ``NEEDS_CHANGE``.
    suggestion: str | None
    reviewer_id: int
    reviewed_at: datetime


class TranslationReviewStore[RowT: TranslationReviewRow](Protocol):
    """The app's verdict rows, on the request's session (§6.2.3). Async; ``put`` and
    ``clear`` flush and never commit."""

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[RowT, ReviewerName | None]]:
        """Every verdict in locales, and inside areas when given (None: every area), by
        locale, then key, each with its reviewer's current name: an outer join, None once
        the account is gone. The area filter comes from area_like_patterns, never by hand."""

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[RowT]:
        """The row of each (locale, key), found or added, takes every field. Flush; the
        rows in the order given. The kit sends each pair once."""

    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        """Delete the rows of these (locale, key) pairs and flush: how many existed."""

    async def by_reviewer(self, reviewer_id: int) -> Sequence[RowT]:
        """The export's rows (§6.2.10), oldest reviewed_at first."""
