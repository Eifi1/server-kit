"""What the translation review's layer-2 tests share: the people, the rows and a spying store."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta

from eifi1_server_kit.translation_review import (
    NewVerdict,
    ReviewerName,
    ReviewGrant,
    TranslationReviewRow,
    TranslationReviewStore,
    TranslationVerdict,
)
from eifi1_server_kit.translation_review.testing import InMemoryTranslationReviewStore, MemoryVerdict, reviewer

NOW = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
#: keksdose's UI locales, in its order.
LOCALES = ("de-CH", "en", "fr", "it")

#: An admin: every locale, every area; reads in English.
ADMIN = reviewer()
#: A native speaker granted French.
FRENCH = reviewer(id=2, first_name="Odile", last_name="Lecteur", locale="fr", grant=ReviewGrant(("fr",), None))
#: A lawyer limited to the legal wording in two locales; reads in Hungarian (family name first).
LAWYER = reviewer(
    id=3, first_name="Lajos", last_name="Kovács", locale="hu", grant=ReviewGrant(("de-CH", "fr"), ("legal",))
)
#: A member, or a demo: granted nothing.
NOBODY = reviewer(id=4, first_name="Max", last_name="Muster", locale="de-CH", grant=ReviewGrant((), None))
#: keksdose's review token, signed in as the admin.
TOKEN = dataclasses.replace(ADMIN, kit_only=True)
#: A review token of a reviewer limited to areas: keksdose refuses it at mint (T2).
LIMITED_TOKEN = dataclasses.replace(LAWYER, kit_only=True)

NAMES = {
    ADMIN.id: ReviewerName("Ada", "Reviewer"),
    FRENCH.id: ReviewerName("Odile", "Lecteur"),
    LAWYER.id: ReviewerName("Lajos", "Kovács"),
    NOBODY.id: ReviewerName("Max", "Muster"),
}

WRITES = frozenset({"put", "clear"})


def row(
    locale: str,
    key: str,
    *,
    verdict: TranslationVerdict = TranslationVerdict.APPROVED,
    by: int | None = ADMIN.id,
    minutes: int = 0,
    note: str | None = None,
    suggestion: str | None = None,
) -> MemoryVerdict:
    """A stored verdict, ``minutes`` before :data:`NOW`."""
    return MemoryVerdict(
        locale=locale,
        key=key,
        text=f"{key} in {locale}",
        reference_text=None,
        verdict=verdict,
        note=note,
        suggestion=suggestion,
        reviewer_id=by,
        reviewed_at=NOW - timedelta(minutes=minutes),
    )


class SpyStore(InMemoryTranslationReviewStore):
    """The fake, logging every call in order."""

    def __init__(self, *rows: MemoryVerdict) -> None:
        super().__init__(names=NAMES)
        self.calls: list[str] = []
        for stored in rows:
            self.rows[(stored.locale, stored.key)] = stored

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        self.calls.append("verdicts")
        return await super().verdicts(locales, areas)

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        self.calls.append("put")
        return await super().put(verdicts)

    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        self.calls.append("clear")
        return await super().clear(pairs)

    async def by_reviewer(self, reviewer_id: int) -> Sequence[MemoryVerdict]:
        self.calls.append("by_reviewer")
        return await super().by_reviewer(reviewer_id)

    def one_write_last(self) -> bool:
        """At most one write, and, if there is one, it is the last call."""
        writes = [call for call in self.calls if call in WRITES]
        return len(writes) <= 1 and (not writes or self.calls[-1] in WRITES)


def _the_fake_satisfies_the_port(
    stored: MemoryVerdict, store: InMemoryTranslationReviewStore
) -> tuple[TranslationReviewRow, TranslationReviewStore[MemoryVerdict]]:
    """mypy checks that the fake is the port."""
    return stored, store
