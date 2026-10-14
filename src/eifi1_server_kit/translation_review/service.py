"""The translation review service: H1's three flows and the export, written once against the store.

Layer 2 (``docs/server-services-harmonization.md`` §6 in ``Eifi1/ui-kit``). What the three
apps each wrote around layer 1's rules — the grant's 403, a batch's last item and its
cleaning, the area filter, the reviewer's name, the export's rows — as one service over
the app's :class:`~eifi1_server_kit.translation_review.TranslationReviewStore`. No
FastAPI: an app's own route (kastlan's ``GET /translations/documents``) calls
:meth:`TranslationReviewService.scope` for the same 403::

    service = TranslationReviewService(store, locale_codes=UI_LOCALE_CODES)
    grant = service.scope(viewer)

**The grant is the guard** (§6.2.6): every route is "any signed-in", and the viewer's
grant decides each request. A demo is granted nothing (``review_grant(…, is_demo=True)``).

**A review token** (keksdose; ``kit_only``) reaches the kit's keys only: its list is the
area ``kit``, and a write or a clear naming any other key is refused whole. A token on a
grant with an area limit is refused its list, not narrowed (§6.2.2, T2); its writes are
held to its areas by ``assert_allowed`` like anyone's.

**The kit never commits** (§6.2.9). Each flow makes at most one store write (``put`` or
``clear``), and it is the flow's last step; who commits is the app's.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from eifi1_server_kit.auth.accounts import full_name
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.translation_review.ports import (
    NewVerdict,
    TranslationReviewer,
    TranslationReviewRow,
    TranslationReviewStore,
)
from eifi1_server_kit.translation_review.schemas import (
    TranslationReviewOut,
    TranslationReviewsResponse,
    TranslationReviewWrite,
    TranslationVerdict,
)
from eifi1_server_kit.translation_review.scope import (
    KIT_AREA,
    NO_KIT_REVIEW_ACCESS,
    NO_REVIEW_ACCESS,
    ReviewGrant,
    TranslationAccessError,
    assert_allowed,
    in_areas,
    require_kit_keys,
)

__all__ = ["VERDICTS_EXPORT_KEY", "TranslationReviewService"]

#: Where an app's account export puts :meth:`TranslationReviewService.export`:
#: ``data["translation_verdicts"]`` (keksdose's key, every app's since 0.10; §6.2.10).
VERDICTS_EXPORT_KEY = "translation_verdicts"


def _clean(value: str | None) -> str | None:
    """Blank optional text is ``None``, so "no note" has one spelling."""
    if value is None:
        return None
    return value.strip() or None


def _out(
    row: TranslationReviewRow, first_name: str | None, last_name: str | None, reader: str | None
) -> TranslationReviewOut:
    """One verdict on the wire, the reviewer's name in the reader's order (§6.2.5)."""
    return TranslationReviewOut(
        locale=row.locale,
        key=row.key,
        text=row.text,
        reference_text=row.reference_text,
        verdict=TranslationVerdict(row.verdict),
        note=row.note,
        suggestion=row.suggestion,
        reviewer_name=full_name(first_name, last_name, reader) or None,
        reviewer_first_name=first_name,
        reviewer_last_name=last_name,
        reviewed_at=row.reviewed_at,
    )


class TranslationReviewService[RowT: TranslationReviewRow]:
    """H1's flows over the app's store (§6.3). Build one per request, on the request's store."""

    def __init__(
        self, store: TranslationReviewStore[RowT], *, locale_codes: Sequence[str], now: Clock = utc_now
    ) -> None:
        self.store = store
        #: The app's UI locales, in its order: an unknown one in a write is a 422.
        self.locale_codes = tuple(locale_codes)
        self.now = now

    def scope(self, viewer: TranslationReviewer) -> ReviewGrant:
        """What ``viewer`` may list: their grant; for a review token, the grant with its
        areas ``("kit",)``.

        :class:`~eifi1_server_kit.translation_review.TranslationAccessError` (403), in this
        order: a grant without a locale (:data:`NO_REVIEW_ACCESS`); a review token on a grant
        with an area limit (:data:`NO_KIT_REVIEW_ACCESS`, T2) — a token lists every ``kit.``
        verdict, more than a legal-only grant may see. keksdose refuses such a token at mint
        and on every sign-in (``translation_review_token_service.py:59-71``, ``:133``); the
        kit refuses it for every app."""
        grant = viewer.grant
        if not grant.locales:
            raise TranslationAccessError(NO_REVIEW_ACCESS)
        if not viewer.kit_only:
            return grant
        if grant.areas is not None:
            raise TranslationAccessError(NO_KIT_REVIEW_ACCESS)
        return dataclasses.replace(grant, areas=(KIT_AREA,))

    def _check(self, viewer: TranslationReviewer, pairs: Sequence[tuple[str, str]]) -> None:
        """A write's or a clear's refusals, in §6.2.8's order: a token naming a key outside
        the kit (403), then every pair inside the grant (422 for a locale the app does not
        ship, else 403)."""
        if viewer.kit_only:
            require_kit_keys(key for _, key in pairs)
        assert_allowed(viewer.grant, pairs, self.locale_codes)

    async def list_reviews(self, viewer: TranslationReviewer) -> TranslationReviewsResponse:
        """``GET /translations/reviews``: every verdict the viewer may see, by locale, then key.

        The store filters by area; the answer is filtered again by locale and
        :func:`~eifi1_server_kit.translation_review.in_areas`, so a store that answers too
        much never shows a key outside the grant."""
        grant = self.scope(viewer)
        allowed = set(grant.locales)
        reviews = [
            _out(
                row,
                name.first_name if name is not None else None,
                name.last_name if name is not None else None,
                viewer.locale,
            )
            for row, name in await self.store.verdicts(grant.locales, grant.areas)
            if row.locale in allowed and in_areas(row.key, grant.areas)
        ]
        areas = list(grant.areas) if grant.areas is not None else None
        return TranslationReviewsResponse(locales=list(grant.locales), areas=areas, reviews=reviews)

    async def save(
        self, viewer: TranslationReviewer, items: Sequence[TranslationReviewWrite]
    ) -> list[TranslationReviewOut]:
        """``PUT /translations/reviews``: a verdict on each item, replacing what was said before.

        All or nothing: every pair is checked before anything is written. The last item per
        ``(locale, key)`` wins, in first-seen order. A blank ``note`` or ``suggestion`` is
        ``None``, and ``suggestion`` is dropped unless the verdict is ``NEEDS_CHANGE``. The
        rows come back with the viewer's own name."""
        latest: dict[tuple[str, str], TranslationReviewWrite] = {}
        for item in items:
            latest[(item.locale, item.key)] = item
        self._check(viewer, list(latest))
        at = self.now()
        rows = await self.store.put(
            [
                NewVerdict(
                    locale=item.locale,
                    key=item.key,
                    text=item.text,
                    reference_text=item.reference_text,
                    verdict=item.verdict,
                    note=_clean(item.note),
                    suggestion=_clean(item.suggestion) if item.verdict is TranslationVerdict.NEEDS_CHANGE else None,
                    reviewer_id=viewer.id,
                    reviewed_at=at,
                )
                for item in latest.values()
            ]
        )
        return [_out(row, viewer.first_name, viewer.last_name, viewer.locale) for row in rows]

    async def clear(self, viewer: TranslationReviewer, keys: Sequence[tuple[str, str]]) -> int:
        """``POST /translations/reviews/clear``: the pairs back to unreviewed, after the same
        checks as a save. Answers how many verdicts existed."""
        pairs = list(dict.fromkeys(keys))
        self._check(viewer, pairs)
        return await self.store.clear(pairs)

    async def export(self, reviewer_id: int) -> list[dict[str, Any]]:
        """The account export's rows for ``data[VERDICTS_EXPORT_KEY]`` (§6.2.10, T1): keksdose's
        shape (``account_export_service.py:375-387``), oldest first — ``locale``, ``string``
        (the key: a bare ``key`` reads as a secret to
        :func:`~eifi1_server_kit.user_admin.assert_no_secrets`), ``text``, ``verdict`` (its
        value), ``suggestion``, ``note`` (in that order, ``:383-384``), ``reviewed_at``."""
        return [
            {
                "locale": row.locale,
                "string": row.key,
                "text": row.text,
                "verdict": TranslationVerdict(row.verdict).value,
                "suggestion": row.suggestion,
                "note": row.note,
                "reviewed_at": row.reviewed_at,
            }
            for row in await self.store.by_reviewer(reviewer_id)
        ]
