"""Test helpers for the translation review: an in-memory store and two conformance checks.

``docs/server-services-harmonization.md`` §6.2.11 in ``Eifi1/ui-kit``. In the wheel and NOT
re-exported from :mod:`eifi1_server_kit.translation_review`: import it as
``eifi1_server_kit.translation_review.testing``. No FastAPI and no httpx.

* :class:`InMemoryTranslationReviewStore` — the fake the kit's own tests run the service
  and the router on. It records every write in
  :attr:`InMemoryTranslationReviewStore.writes`, so a test asserts the one-write rule
  (§6.2.9);
* :func:`reviewer`;
* :func:`check_store` — what an app runs once against its own store adapter, on its test
  database as it is: it writes under a fresh area, asserts only on its own keys and
  clears them;
* :func:`check_round_trip` — a list, a verdict, the list again and a clear, over the app's
  own client at its mount.

Synthetic data only: every key carries a fresh token, so two runs on the same database
never share a row.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from eifi1_server_kit.translation_review.ports import (
    NewVerdict,
    ReviewerName,
    TranslationReviewer,
    TranslationReviewRow,
    TranslationReviewStore,
)
from eifi1_server_kit.translation_review.schemas import TEXT_MAX, TranslationVerdict
from eifi1_server_kit.translation_review.scope import ReviewGrant, in_areas

__all__ = [
    "InMemoryTranslationReviewStore",
    "MemoryVerdict",
    "check_round_trip",
    "check_store",
    "reviewer",
]

#: The review paths at the standard mount: ``cors.TRANSLATION_REVIEW_ROUTES``'s own.
_REVIEWS = "/api/v1/translations/reviews"
_CLEAR = "/api/v1/translations/reviews/clear"


@dataclass(slots=True)
class MemoryVerdict:
    """One verdict in memory: the row's members and ``reviewer_id``, mutable. Satisfies
    :class:`~eifi1_server_kit.translation_review.TranslationReviewRow`. Slotted, so a write
    naming a column that does not exist fails loudly. ``reviewer_id`` ``None`` is an
    erased reviewer (the foreign key's ``SET NULL``)."""

    locale: str
    key: str
    text: str
    reference_text: str | None
    verdict: TranslationVerdict
    note: str | None
    suggestion: str | None
    reviewer_id: int | None
    reviewed_at: datetime


class InMemoryTranslationReviewStore:
    """A :class:`~eifi1_server_kit.translation_review.TranslationReviewStore` of
    :class:`MemoryVerdict`. ``names`` are the reviewers' current names (the list's join)."""

    def __init__(self, *, names: Mapping[int, ReviewerName] | None = None) -> None:
        #: Every row, by ``(locale, key)``.
        self.rows: dict[tuple[str, str], MemoryVerdict] = {}
        #: Every write, in order.
        self.writes: list[Literal["put", "clear"]] = []
        self.names = dict(names or {})

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        rows = sorted(
            (row for row in self.rows.values() if row.locale in locales and in_areas(row.key, areas)),
            key=lambda row: (row.locale, row.key),
        )
        return [(row, self.names.get(row.reviewer_id) if row.reviewer_id is not None else None) for row in rows]

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[MemoryVerdict]:
        written: list[MemoryVerdict] = []
        for new in verdicts:
            fields = {field.name: getattr(new, field.name) for field in dataclasses.fields(new)}
            row = self.rows.get((new.locale, new.key))
            if row is None:
                row = self.rows[(new.locale, new.key)] = MemoryVerdict(**fields)
            else:
                for name, value in fields.items():
                    setattr(row, name, value)
            written.append(row)
        self.writes.append("put")
        return written

    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        cleared = sum(self.rows.pop(pair, None) is not None for pair in pairs)
        self.writes.append("clear")
        return cleared

    async def by_reviewer(self, reviewer_id: int) -> Sequence[MemoryVerdict]:
        return sorted((row for row in self.rows.values() if row.reviewer_id == reviewer_id), key=_reviewed_at)


def reviewer(**overrides: Any) -> TranslationReviewer:
    """A :class:`~eifi1_server_kit.translation_review.TranslationReviewer`: id 1, "Ada"
    "Reviewer", reading in ``en``, granted every locale of ``("de-CH", "en", "fr", "it")``
    and every area, not a review token — each overridable by name."""
    base = TranslationReviewer(
        id=1,
        first_name="Ada",
        last_name="Reviewer",
        locale="en",
        grant=ReviewGrant(locales=("de-CH", "en", "fr", "it"), areas=None),
    )
    return dataclasses.replace(base, **overrides)


def _check(condition: bool, message: str) -> None:
    """``assert`` that survives ``python -O``."""
    if not condition:
        raise AssertionError(message)


def _aware(moment: datetime) -> datetime:
    """A naive datetime read as UTC — what SQLite hands back."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _reviewed_at(row: TranslationReviewRow) -> datetime:
    return _aware(row.reviewed_at)


def _same(row: TranslationReviewRow, new: NewVerdict) -> bool:
    """Does ``row`` read back as ``new`` wrote it?"""
    return (
        row.locale == new.locale
        and row.key == new.key
        and row.text == new.text
        and row.reference_text == new.reference_text
        and TranslationVerdict(row.verdict) is new.verdict
        and row.note == new.note
        and row.suggestion == new.suggestion
        and _aware(row.reviewed_at) == new.reviewed_at
    )


async def check_store(store: TranslationReviewStore[Any], *, reviewer_id: int, name: ReviewerName, locale: str) -> None:
    """Does the app's store keep the port's promises (§6.2.11)? Raises
    :class:`AssertionError` naming the first that fails.

    For one existing account (``reviewer_id``, whose current name is ``name``) and one of
    the app's locales. It writes under a fresh area ``chk_<token>`` — the ``_`` tests the
    ``LIKE`` escape — asserts only on its own keys, and clears them at the end:

    1. ``put`` of ``<area>.one`` (APPROVED) and ``<area>.two`` (NEEDS_CHANGE, an empty
       ``text``, a suggestion, a 40 000-character ``reference_text``): the rows come back
       in order and read back alike;
    2. ``verdicts([locale], None)`` holds both, by key, each paired with ``name``;
    3. a second ``put`` of ``<area>.one``: still one row for the pair, with the new verdict;
    4. ``verdicts([locale], [area])`` holds ``<area>``, ``<area>:x`` and ``kit.<area>.x``,
       and neither ``chkX<token>.x`` nor ``other.<area>.x``;
    5. ``by_reviewer`` holds them all, oldest first;
    6. ``clear`` of ``.one``, ``.two`` and a pair never written answers 2, and they are gone.
    """
    token = secrets.token_hex(4)
    area = f"chk_{token}"
    start = datetime.now(UTC).replace(microsecond=0)

    def verdict(key: str, kind: TranslationVerdict, at: datetime, **fields: Any) -> NewVerdict:
        values: dict[str, Any] = {
            "locale": locale,
            "key": key,
            "text": f"Checked wording ({token})",
            "reference_text": None,
            "verdict": kind,
            "note": None,
            "suggestion": None,
            "reviewer_id": reviewer_id,
            "reviewed_at": at,
        }
        values.update(fields)
        return NewVerdict(**values)

    def mine(rows: Sequence[tuple[TranslationReviewRow, ReviewerName | None]]) -> list[str]:
        keys = {key for _, key in written}
        return [row.key for row, _ in rows if row.locale == locale and row.key in keys]

    one, two = f"{area}.one", f"{area}.two"
    written: set[tuple[str, str]] = {(locale, one), (locale, two)}

    # 1. Two verdicts in one put: in order, and alike.
    first = [
        verdict(one, TranslationVerdict.APPROVED, start, note=f"Fine as it is ({token})"),
        verdict(
            two,
            TranslationVerdict.NEEDS_CHANGE,
            start,
            text="",
            suggestion=f"Better wording ({token})",
            reference_text=(f"Référence {token} " * 3_000)[:TEXT_MAX],
        ),
    ]
    rows = list(await store.put(first))
    _check([row.key for row in rows] == [one, two], "put: the rows in the order given")
    _check(all(_same(row, new) for row, new in zip(rows, first, strict=True)), "put: every field reads back alike")

    # 2. The list: both, by key, each with the reviewer's name.
    listed = list(await store.verdicts([locale], None))
    _check(mine(listed) == [one, two], "verdicts: both rows, by key")
    names = [found for row, found in listed if row.locale == locale and row.key in {one, two}]
    _check(
        all(
            found is not None and (found.first_name, found.last_name) == (name.first_name, name.last_name)
            for found in names
        ),
        "verdicts: each row paired with its reviewer's name",
    )
    _check(
        all(
            _same(row, new) for row, new in zip([row for row, _ in listed if row.key in {one, two}], first, strict=True)
        ),
        "verdicts: every field reads back alike",
    )

    # 3. A second verdict on the same pair replaces the first.
    again = verdict(one, TranslationVerdict.NEEDS_CHANGE, start + timedelta(seconds=1), suggestion="Shorter")
    replaced = list(await store.put([again]))
    _check(len(replaced) == 1 and _same(replaced[0], again), "put: a pair already written takes the new fields")
    pairs = [row for row, _ in await store.verdicts([locale], None) if row.locale == locale and row.key == one]
    _check(len(pairs) == 1, "put: still one row for the pair")
    _check(TranslationVerdict(pairs[0].verdict) is TranslationVerdict.NEEDS_CHANGE, "put: the pair has the new verdict")

    # 4. The area filter: the four arms, the escape, and nothing that merely contains the area.
    inside = [area, f"{area}:x", f"kit.{area}.x"]
    outside = [f"chkX{token}.x", f"other.{area}.x"]
    later = start + timedelta(seconds=2)
    await store.put([verdict(key, TranslationVerdict.APPROVED, later) for key in inside + outside])
    written |= {(locale, key) for key in inside + outside}
    in_area = set(mine(await store.verdicts([locale], [area])))
    for key in [*inside, one, two]:
        _check(key in in_area, f"verdicts(areas): {key!r} lies in the area {area!r}")
    _check(f"chkX{token}.x" not in in_area, "verdicts(areas): '_' in an area is escaped in the LIKE pattern")
    _check(f"other.{area}.x" not in in_area, "verdicts(areas): a key that only contains the area lies outside it")

    # 5. The export's rows: all of them, oldest first.
    exported = [row for row in await store.by_reviewer(reviewer_id) if (row.locale, row.key) in written]
    _check({(row.locale, row.key) for row in exported} == written, "by_reviewer: every row the reviewer wrote")
    moments = [_aware(row.reviewed_at) for row in exported]
    _check(moments == sorted(moments), "by_reviewer: oldest reviewed_at first")
    _check([row.key for row in exported][:2] == [two, one], "by_reviewer: the older verdict first")

    # 6. A clear answers how many existed, and they are gone; then the rest of the check's rows.
    cleared = await store.clear([(locale, one), (locale, two), (locale, f"{area}.never")])
    _check(cleared == 2, f"clear: answers how many existed (2), not {cleared}")
    _check(not set(mine(await store.verdicts([locale], None))) & {one, two}, "clear: the rows are gone")
    rest = written - {(locale, one), (locale, two)}
    _check(await store.clear(rest) == len(rest), "clear: every pair it names")
    _check(mine(await store.verdicts([locale], None)) == [], "clear: nothing of the check's is left")


async def check_round_trip(client: Any, *, headers: Mapping[str, str], locale: str) -> None:
    """A list, a verdict, the list again and a clear, through the app's router at
    ``/api/v1`` (§6.2.11). Raises :class:`AssertionError` naming the first thing that fails.

    ``GET /api/v1/translations/reviews`` is 200 with ``locale`` among ``locales``; a
    ``PUT`` of one verdict on ``kit.check.<token>`` answers it; the next ``GET`` holds it;
    ``POST /api/v1/translations/reviews/clear`` answers ``cleared: 1``. ``client`` is an
    ``httpx.AsyncClient`` (``ASGITransport`` over the app) or anything with its ``get``,
    ``put`` and ``post``; ``headers`` sign in a person who may review ``locale`` and the
    kit's wording (an admin, or keksdose's review token).
    """
    sent = dict(headers)
    key = f"kit.check.{secrets.token_hex(4)}"

    listed = await client.get(_REVIEWS, headers=sent)
    _check(listed.status_code == 200, f"GET: 200, not {listed.status_code} {listed.text}")
    _check(locale in listed.json().get("locales", []), f"GET: {locale!r} is among the caller's locales")

    item = {"locale": locale, "key": key, "text": "Checked wording", "verdict": TranslationVerdict.APPROVED.value}
    saved = await client.put(_REVIEWS, json={"items": [item]}, headers=sent)
    _check(saved.status_code == 200, f"PUT: 200, not {saved.status_code} {saved.text}")
    answered = saved.json().get("reviews", [])
    _check(
        [(review.get("locale"), review.get("key"), review.get("verdict")) for review in answered]
        == [(locale, key, item["verdict"])],
        "PUT: answers the verdict",
    )

    again = await client.get(_REVIEWS, headers=sent)
    _check(
        any(review.get("locale") == locale and review.get("key") == key for review in again.json().get("reviews", [])),
        "GET: the list holds the verdict",
    )

    cleared = await client.post(_CLEAR, json={"items": [{"locale": locale, "key": key}]}, headers=sent)
    _check(cleared.status_code == 200, f"clear: 200, not {cleared.status_code} {cleared.text}")
    _check(cleared.json() == {"cleared": 1}, f"clear: answers cleared 1, not {cleared.json()}")
