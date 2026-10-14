"""``translation_review.testing``: the checks pass on the fake and fail on a broken store.

server-services-harmonization §6.2.11: an app runs ``check_store`` once against its own
store adapter, and ``check_round_trip`` over its own client.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

import pytest

from eifi1_server_kit.cors import TRANSLATION_REVIEW_ROUTES
from eifi1_server_kit.translation_review import (
    KIT_KEY_PREFIX,
    NewVerdict,
    ReviewerName,
    ReviewGrant,
    TranslationReviewer,
    TranslationVerdict,
)
from eifi1_server_kit.translation_review.testing import (
    InMemoryTranslationReviewStore,
    MemoryVerdict,
    check_round_trip,
    check_store,
    reviewer,
)
from tests.translation_review._layer2 import NOW

NAME = ReviewerName("Ada", "Reviewer")


async def _check(store: InMemoryTranslationReviewStore) -> None:
    await check_store(store, reviewer_id=7, name=NAME, locale="fr")


def _fake() -> InMemoryTranslationReviewStore:
    return InMemoryTranslationReviewStore(names={7: NAME})


def test_reviewer() -> None:
    assert reviewer() == TranslationReviewer(
        id=1,
        first_name="Ada",
        last_name="Reviewer",
        locale="en",
        grant=ReviewGrant(("de-CH", "en", "fr", "it"), None),
        kit_only=False,
    )
    assert reviewer(id=9, kit_only=True).kit_only and reviewer(locale="hu").locale == "hu"


def test_the_memory_row_refuses_a_column_that_does_not_exist() -> None:
    stored = MemoryVerdict(
        locale="fr",
        key="a",
        text="a",
        reference_text=None,
        verdict=TranslationVerdict.APPROVED,
        note=None,
        suggestion=None,
        reviewer_id=None,
        reviewed_at=NOW,
    )
    with pytest.raises(AttributeError):
        setattr(stored, "reviewer_name", "x")  # noqa: B010 - the point is a name the class does not have


# --- check_store -------------------------------------------------------------------------


async def test_check_store_passes_on_the_fake_and_leaves_nothing() -> None:
    store = _fake()
    store.rows["fr", "common.save"] = MemoryVerdict(
        locale="fr",
        key="common.save",
        text="Enregistrer",
        reference_text=None,
        verdict=TranslationVerdict.APPROVED,
        note=None,
        suggestion=None,
        reviewer_id=7,
        reviewed_at=NOW,
    )
    await _check(store)
    assert list(store.rows) == [("fr", "common.save")], "the app's own rows stay; the check's are gone"
    assert store.writes == ["put", "put", "put", "clear", "clear"]


class NaiveStore(InMemoryTranslationReviewStore):
    """SQLite hands back naive datetimes: still the same moment."""

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        rows = await super().put(verdicts)
        for stored in rows:
            stored.reviewed_at = stored.reviewed_at.replace(tzinfo=None)
        return rows


async def test_check_store_reads_a_naive_datetime_as_utc() -> None:
    await _check(NaiveStore(names={7: NAME}))


# --- broken stores ------------------------------------------------------------------------


class NoColonArm(InMemoryTranslationReviewStore):
    """0.9's filter: three arms, so kastlan's ``legal:imprint.title`` falls outside ``legal``."""

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        def inside(key: str) -> bool:
            return areas is None or any(
                key == area or key.startswith(f"{area}.") or key.startswith(f"{KIT_KEY_PREFIX}{area}.")
                for area in areas
            )

        answer = await super().verdicts(locales, None)
        return [(stored, name) for stored, name in answer if inside(stored.key)]


class TwoRowsPerPair(InMemoryTranslationReviewStore):
    """Adds a row on every put, so a pair judged twice has two rows."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.every: list[MemoryVerdict] = []

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        added = [MemoryVerdict(**dataclasses.asdict(new)) for new in verdicts]
        self.every += added
        return added

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        rows = sorted((stored for stored in self.every if stored.locale in locales), key=lambda r: (r.locale, r.key))
        return [(stored, self.names.get(stored.reviewer_id or 0)) for stored in rows]


class UnescapedLike(InMemoryTranslationReviewStore):
    """A ``LIKE`` built by hand: ``_`` matches any one character."""

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        answer = await super().verdicts(locales, None)
        if areas is None:
            return answer
        rules = [re.compile(r"(kit\.)?" + re.escape(area).replace("_", ".") + r"($|[.:])") for area in areas]
        return [(stored, name) for stored, name in answer if any(rule.match(stored.key) for rule in rules)]


class NewestFirst(InMemoryTranslationReviewStore):
    async def by_reviewer(self, reviewer_id: int) -> Sequence[MemoryVerdict]:
        return list(reversed(await super().by_reviewer(reviewer_id)))


class ClearAnswersWhatItWasSent(InMemoryTranslationReviewStore):
    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        await super().clear(pairs)
        return len(pairs)


class NoNames(InMemoryTranslationReviewStore):
    """An inner join's mistake turned around: the name never joined."""

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        return [(stored, None) for stored, _ in await super().verdicts(locales, areas)]


class EmptyTextIsNull(InMemoryTranslationReviewStore):
    """A column that stores an empty text as NULL, read back as ``"None"``'s cousin."""

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        rows = await super().put(verdicts)
        for stored in rows:
            stored.text = stored.text or "(empty)"
        return rows


class OutOfOrder(InMemoryTranslationReviewStore):
    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        return list(reversed(await super().put(verdicts)))


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (NoColonArm, r"lies in the area"),
        (TwoRowsPerPair, r"still one row for the pair"),
        (UnescapedLike, r"'_' in an area is escaped"),
        (NewestFirst, r"oldest reviewed_at first"),
        (ClearAnswersWhatItWasSent, r"answers how many existed \(2\), not 3"),
        (NoNames, r"paired with its reviewer's name"),
        (EmptyTextIsNull, r"put: every field reads back alike"),
        (OutOfOrder, r"put: the rows in the order given"),
    ],
)
async def test_check_store_fails_on_a_broken_store(broken: type[InMemoryTranslationReviewStore], message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        await _check(broken(names={7: NAME}))


# --- check_round_trip ----------------------------------------------------------------------


class _Answer:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        self.text = repr(body)

    def json(self) -> Any:
        return self.body


class _Client:
    """A client whose router answers ``answers`` in turn."""

    def __init__(self, *answers: _Answer) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, str, Mapping[str, str]]] = []

    async def _next(self, method: str, url: str, headers: Mapping[str, str]) -> _Answer:
        self.sent.append((method, url, headers))
        return self.answers.pop(0)

    async def get(self, url: str, *, headers: Mapping[str, str]) -> _Answer:
        return await self._next("GET", url, headers)

    async def put(self, url: str, *, json: Any, headers: Mapping[str, str]) -> _Answer:
        return await self._next("PUT", url, headers)

    async def post(self, url: str, *, json: Any, headers: Mapping[str, str]) -> _Answer:
        return await self._next("POST", url, headers)


async def test_check_round_trip_names_what_fails() -> None:
    with pytest.raises(AssertionError, match="GET: 200, not 403"):
        await check_round_trip(_Client(_Answer(403, {})), headers={}, locale="fr")
    with pytest.raises(AssertionError, match="'it' is among the caller's locales"):
        await check_round_trip(_Client(_Answer(200, {"locales": ["fr"]})), headers={}, locale="it")
    listed = _Answer(200, {"locales": ["fr"], "reviews": []})
    with pytest.raises(AssertionError, match="PUT: answers the verdict"):
        await check_round_trip(_Client(listed, _Answer(200, {"reviews": []})), headers={}, locale="fr")


async def test_check_round_trip_uses_the_cors_paths() -> None:
    """The paths are ``TRANSLATION_REVIEW_ROUTES``'s, and the headers go with every request."""
    client = _Client(_Answer(403, {}))
    with pytest.raises(AssertionError):
        await check_round_trip(client, headers={"Authorization": "Bearer x"}, locale="fr")
    method, url, headers = client.sent[0]
    assert (method, headers) == ("GET", {"Authorization": "Bearer x"})
    assert url in TRANSLATION_REVIEW_ROUTES and "GET" in TRANSLATION_REVIEW_ROUTES[url].methods
