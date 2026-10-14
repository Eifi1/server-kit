"""The translation review service on the fake (server-services-harmonization §6.2–§6.3).

H1's behaviour is tested once, here and over HTTP in ``test_router.py``: the grant as the
guard, a review token's reach, the refusals' order, the last item, the cleaning, the
reader's order of the name, the second filter and the export.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime

import pytest

from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.translation_review import (
    NO_KIT_REVIEW_ACCESS,
    NO_REVIEW_ACCESS,
    VERDICTS_EXPORT_KEY,
    ReviewerName,
    ReviewGrant,
    TranslationAccessError,
    TranslationLocaleError,
    TranslationReviewService,
    TranslationReviewWrite,
    TranslationVerdict,
)
from eifi1_server_kit.translation_review.testing import MemoryVerdict, reviewer
from eifi1_server_kit.user_admin import assert_no_secrets
from tests.translation_review._layer2 import (
    ADMIN,
    FRENCH,
    LAWYER,
    LIMITED_TOKEN,
    LOCALES,
    NOBODY,
    NOW,
    TOKEN,
    SpyStore,
    row,
)

APPROVED = TranslationVerdict.APPROVED
NEEDS_CHANGE = TranslationVerdict.NEEDS_CHANGE


def _service(store: SpyStore) -> TranslationReviewService[MemoryVerdict]:
    return TranslationReviewService(store, locale_codes=LOCALES, now=lambda: NOW)


def _write(locale: str, key: str, verdict: str = "APPROVED", **fields: str | None) -> TranslationReviewWrite:
    return TranslationReviewWrite.model_validate(
        {"locale": locale, "key": key, "text": f"{key} in {locale}", "verdict": verdict, **fields}
    )


def _world() -> SpyStore:
    """keksdose's three kinds of key and kastlan's ``ns:key``, in two locales, by three people."""
    return SpyStore(
        row("fr", "common.save", by=FRENCH.id, minutes=5),
        row("fr", "legal.terms.title", by=LAWYER.id, minutes=4),
        row("fr", "legal:imprint.title", by=LAWYER.id, minutes=3),
        row("fr", "kit.legal.sections.warranty.title", by=LAWYER.id, minutes=2),
        row("fr", "kit.form.save", by=ADMIN.id, minutes=1),
        row("it", "common.save", verdict=NEEDS_CHANGE, by=ADMIN.id, suggestion="Salva", note="Troppo lungo"),
        row("de-CH", "legalese.x", by=None),
    )


# --- scope ------------------------------------------------------------------------------


def test_scope_is_the_grant() -> None:
    service = _service(SpyStore())
    assert service.scope(ADMIN) == ReviewGrant(LOCALES, None)
    assert service.scope(LAWYER) == ReviewGrant(("de-CH", "fr"), ("legal",))
    assert service.locale_codes == LOCALES


def test_scope_refuses_a_grant_without_a_locale() -> None:
    with pytest.raises(TranslationAccessError) as refused:
        _service(SpyStore()).scope(NOBODY)
    assert str(refused.value) == NO_REVIEW_ACCESS and refused.value.status_code == 403


def test_a_review_token_is_narrowed_to_the_kit() -> None:
    """keksdose router ``:92``: a token's list is the area ``kit``."""
    assert _service(SpyStore()).scope(TOKEN) == ReviewGrant(LOCALES, ("kit",))


def test_a_token_on_a_grant_with_an_area_limit_is_refused_not_narrowed() -> None:
    """T2 (§6.2.2): keksdose refuses it at mint and at sign-in
    (``translation_review_token_service.py:59-71``, ``:133``); the kit's ``scope`` refuses
    it for every app, even where an area holds the kit's keys."""
    service = _service(SpyStore())
    for token in (LIMITED_TOKEN, reviewer(grant=ReviewGrant(("fr",), ("kit",)), kit_only=True)):
        with pytest.raises(TranslationAccessError) as refused:
            service.scope(token)
        assert str(refused.value) == NO_KIT_REVIEW_ACCESS and refused.value.status_code == 403
    # The same grant without the token is fine: the refusal is the token's.
    assert service.scope(LAWYER).areas == ("legal",)


def test_scope_checks_the_locale_first() -> None:
    """§6.3's order: no locale, then a token's area limit."""
    token = reviewer(grant=ReviewGrant((), ("legal",)), kit_only=True)
    with pytest.raises(TranslationAccessError, match=NO_REVIEW_ACCESS):
        _service(SpyStore()).scope(token)


# --- list -------------------------------------------------------------------------------


async def test_the_list_by_locale_then_key_with_the_names_parts() -> None:
    store = _world()
    listed = await _service(store).list_reviews(ADMIN)
    assert listed.locales == list(LOCALES) and listed.areas is None
    assert [(review.locale, review.key) for review in listed.reviews] == [
        ("de-CH", "legalese.x"),
        ("fr", "common.save"),
        ("fr", "kit.form.save"),
        ("fr", "kit.legal.sections.warranty.title"),
        ("fr", "legal.terms.title"),
        ("fr", "legal:imprint.title"),
        ("it", "common.save"),
    ]
    by_key = {(review.locale, review.key): review for review in listed.reviews}
    french = by_key["fr", "common.save"]
    assert (french.reviewer_name, french.reviewer_first_name, french.reviewer_last_name) == (
        "Odile Lecteur",
        "Odile",
        "Lecteur",
    )
    italian = by_key["it", "common.save"]
    assert italian.verdict is NEEDS_CHANGE and italian.suggestion == "Salva" and italian.note == "Troppo lungo"
    assert store.calls == ["verdicts"]


async def test_an_erased_reviewers_verdict_names_nobody() -> None:
    """The foreign key's ``SET NULL`` (§6.2.10): the verdict stays, the person goes."""
    erased = (await _service(_world()).list_reviews(ADMIN)).reviews[0]
    assert erased.key == "legalese.x"
    assert (erased.reviewer_name, erased.reviewer_first_name, erased.reviewer_last_name) == (None, None, None)


async def test_the_name_is_in_the_readers_order() -> None:
    """T3 (§6.2.5): ``full_name(first, last, reader's locale)`` — the lawyer reads in
    Hungarian, family name first; the admin in English."""
    store = _world()
    for viewer, expected in ((ADMIN, "Lajos Kovács"), (LAWYER, "Kovács Lajos")):
        listed = await _service(store).list_reviews(viewer)
        names = {review.key: review.reviewer_name for review in listed.reviews if review.locale == "fr"}
        assert names["legal.terms.title"] == expected
    chinese = SpyStore(row("fr", "common.save", by=ADMIN.id))
    chinese.names[ADMIN.id] = ReviewerName("小明", "王")
    listed = await _service(chinese).list_reviews(reviewer(locale="zh"))
    assert listed.reviews[0].reviewer_name == "王小明"


async def test_a_reviewer_sees_their_locales_only() -> None:
    listed = await _service(_world()).list_reviews(FRENCH)
    assert listed.locales == ["fr"] and listed.areas is None
    assert {review.locale for review in listed.reviews} == {"fr"}
    assert len(listed.reviews) == 5


async def test_an_area_limit_holds_all_four_arms() -> None:
    """T4: ``legal``, ``legal.…``, ``legal:…`` and ``kit.legal.…`` — never ``legalese``."""
    listed = await _service(_world()).list_reviews(LAWYER)
    assert listed.locales == ["de-CH", "fr"] and listed.areas == ["legal"]
    assert [review.key for review in listed.reviews] == [
        "kit.legal.sections.warranty.title",
        "legal.terms.title",
        "legal:imprint.title",
    ]


async def test_a_token_lists_the_kits_verdicts_only() -> None:
    listed = await _service(_world()).list_reviews(TOKEN)
    assert listed.areas == ["kit"]
    assert [review.key for review in listed.reviews] == ["kit.form.save", "kit.legal.sections.warranty.title"]


class LeakyStore(SpyStore):
    """A store that ignores the locales and the areas it is asked for."""

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[MemoryVerdict, ReviewerName | None]]:
        return await super().verdicts(LOCALES, None)


async def test_a_store_that_answers_too_much_never_shows_a_key_outside_the_grant() -> None:
    """§6.2.4: the service filters the answer again by locale and ``in_areas``."""
    store = LeakyStore(*_world().rows.values())
    listed = await _service(store).list_reviews(LAWYER)
    assert {review.locale for review in listed.reviews} == {"fr"}
    assert [review.key for review in listed.reviews] == [
        "kit.legal.sections.warranty.title",
        "legal.terms.title",
        "legal:imprint.title",
    ]


@pytest.mark.parametrize(("viewer", "message"), [(NOBODY, NO_REVIEW_ACCESS), (LIMITED_TOKEN, NO_KIT_REVIEW_ACCESS)])
async def test_a_refused_list_reads_nothing(viewer: object, message: str) -> None:
    store = _world()
    with pytest.raises(TranslationAccessError) as refused:
        await _service(store).list_reviews(viewer)  # type: ignore[arg-type]
    assert str(refused.value) == message
    assert store.calls == []


# --- save -------------------------------------------------------------------------------


async def test_a_save_writes_each_pair_once_the_last_item_winning() -> None:
    store = SpyStore(row("fr", "common.save", by=FRENCH.id, minutes=60, note="Old"))
    saved = await _service(store).save(
        FRENCH,
        [
            _write("fr", "common.save", "NEEDS_CHANGE", suggestion="Garder"),
            _write("fr", "common.cancel"),
            _write("fr", "common.save", "APPROVED", note="  Fine now  ", suggestion="stale draft"),
        ],
    )
    assert [(review.key, review.verdict) for review in saved] == [
        ("common.save", APPROVED),
        ("common.cancel", APPROVED),
    ]
    assert store.calls == ["put"] and store.writes == ["put"]
    stored = store.rows["fr", "common.save"]
    assert stored.note == "Fine now", "trimmed"
    assert stored.suggestion is None, "a suggestion only stands beside NEEDS_CHANGE"
    assert stored.reviewer_id == FRENCH.id and stored.reviewed_at == NOW
    assert len(store.rows) == 2


async def test_a_save_cleans_blank_text_to_none() -> None:
    store = SpyStore()
    await _service(store).save(
        FRENCH,
        [
            _write("fr", "a.one", "NEEDS_CHANGE", note="   ", suggestion="  Mieux  "),
            _write("fr", "a.two", "NEEDS_CHANGE", note=None, suggestion="   "),
        ],
    )
    one, two = store.rows["fr", "a.one"], store.rows["fr", "a.two"]
    assert (one.note, one.suggestion) == (None, "Mieux")
    assert (two.note, two.suggestion) == (None, None)


async def test_a_save_answers_with_the_viewers_own_name_in_their_order() -> None:
    saved = await _service(SpyStore()).save(LAWYER, [_write("fr", "legal:imprint.title")])
    assert (saved[0].reviewer_name, saved[0].reviewer_first_name, saved[0].reviewer_last_name) == (
        "Kovács Lajos",
        "Lajos",
        "Kovács",
    )
    nameless = await _service(SpyStore()).save(reviewer(first_name=None, last_name=None), [_write("fr", "a")])
    assert nameless[0].reviewer_name is None


async def test_a_token_writes_the_kits_keys() -> None:
    store = SpyStore()
    await _service(store).save(TOKEN, [_write("fr", "kit.form.save"), _write("it", "kit.legal.a")])
    assert set(store.rows) == {("fr", "kit.form.save"), ("it", "kit.legal.a")}


async def test_a_write_needs_no_t2_check() -> None:
    """§6.2.2: ``assert_allowed`` holds a token to its grant's areas, so one on a legal-only
    grant writes ``kit.legal.*`` and nothing else of the kit's; only ``scope`` refuses it."""
    store = SpyStore()
    await _service(store).save(LIMITED_TOKEN, [_write("fr", "kit.legal.sections.a")])
    assert set(store.rows) == {("fr", "kit.legal.sections.a")}
    assert await _service(store).clear(LIMITED_TOKEN, [("fr", "kit.legal.sections.a")]) == 1


@pytest.mark.parametrize(
    ("viewer", "pairs", "error", "message"),
    [
        # A token: one key outside the kit refuses the whole batch…
        (TOKEN, [("fr", "kit.a"), ("fr", "common.save")], TranslationAccessError, "kit's wording only"),
        # …and its 403 comes before the grant's (a locale the token's reviewer lacks).
        (
            reviewer(grant=ReviewGrant(("fr",), None), kit_only=True),
            [("it", "common.save")],
            TranslationAccessError,
            "kit's wording only",
        ),
        # …and before an unknown locale's 422.
        (TOKEN, [("xx", "common.save")], TranslationAccessError, "kit's wording only"),
        # A token on a grant with an area limit is held to its areas by the grant.
        (LIMITED_TOKEN, [("fr", "kit.legal.a"), ("fr", "kit.form.save")], TranslationAccessError, "limited to legal"),
        # The grant: a locale the app does not ship is a 422…
        (FRENCH, [("xx", "common.save")], TranslationLocaleError, "Unknown locale 'xx'"),
        # …a locale outside the grant a 403, the first offending pair deciding…
        (FRENCH, [("fr", "a"), ("it", "a"), ("xx", "a")], TranslationAccessError, "Not granted for locale 'it'"),
        (FRENCH, [("fr", "a"), ("xx", "a"), ("it", "a")], TranslationLocaleError, "Unknown locale 'xx'"),
        # …and a key outside the areas a 403.
        (LAWYER, [("fr", "legal.a"), ("fr", "legalese.x")], TranslationAccessError, "limited to legal"),
        # A demo, or a member: nothing.
        (NOBODY, [("de-CH", "common.save")], TranslationAccessError, "Not granted for locale 'de-CH'"),
    ],
)
async def test_every_pair_is_checked_before_anything_is_written(
    viewer: object, pairs: list[tuple[str, str]], error: type[Exception], message: str
) -> None:
    for flow in ("save", "clear"):
        store = SpyStore(row("fr", "common.save"))
        service = _service(store)
        with pytest.raises(error, match=message):
            if flow == "save":
                await service.save(viewer, [_write(locale, key) for locale, key in pairs])  # type: ignore[arg-type]
            else:
                await service.clear(viewer, pairs)  # type: ignore[arg-type]
        assert store.calls == [], flow
        assert list(store.rows) == [("fr", "common.save")], flow


# --- clear ------------------------------------------------------------------------------


async def test_a_clear_answers_how_many_existed() -> None:
    store = _world()
    cleared = await _service(store).clear(
        FRENCH, [("fr", "common.save"), ("fr", "never.reviewed"), ("fr", "common.save")]
    )
    assert cleared == 1
    assert ("fr", "common.save") not in store.rows
    assert store.calls == ["clear"] and store.writes == ["clear"]


class RecordingStore(SpyStore):
    """The fake, keeping what each clear was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[list[tuple[str, str]]] = []

    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        self.sent.append(list(pairs))
        return await super().clear(pairs)


async def test_a_clear_sends_each_pair_once() -> None:
    store = RecordingStore()
    await _service(store).clear(ADMIN, [("fr", "a"), ("it", "a"), ("fr", "a")])
    assert store.sent == [[("fr", "a"), ("it", "a")]]


# --- export -----------------------------------------------------------------------------


async def test_the_export_is_keksdoses_shape_oldest_first() -> None:
    """T1 (§6.2.10): ``locale``, ``string``, ``text``, ``verdict``, ``suggestion``, ``note``,
    ``reviewed_at`` — keksdose ``account_export_service.py:375-387`` — the reviewer's own
    rows, oldest first."""
    assert VERDICTS_EXPORT_KEY == "translation_verdicts"
    store = _world()
    exported = await _service(store).export(LAWYER.id)
    assert [list(item) for item in exported] == [
        ["locale", "string", "text", "verdict", "suggestion", "note", "reviewed_at"]
    ] * 3
    assert [item["string"] for item in exported] == [
        "legal.terms.title",
        "legal:imprint.title",
        "kit.legal.sections.warranty.title",
    ]
    assert exported[0]["verdict"] == "APPROVED" and isinstance(exported[0]["verdict"], str)
    assert [item["reviewed_at"] for item in exported] == sorted(item["reviewed_at"] for item in exported)
    assert store.calls == ["by_reviewer"]

    italian = (await _service(store).export(ADMIN.id))[-1]
    assert italian == {
        "locale": "it",
        "string": "common.save",
        "text": "common.save in it",
        "verdict": "NEEDS_CHANGE",
        "suggestion": "Salva",
        "note": "Troppo lungo",
        "reviewed_at": NOW,
    }
    # `string`, not `key`: the kit's export check passes it (6.1's finding).
    assert_no_secrets({VERDICTS_EXPORT_KEY: [*exported, italian]})
    assert await _service(store).export(NOBODY.id) == []


# --- the clock --------------------------------------------------------------------------


async def test_the_default_clock_is_utc_now() -> None:
    store = SpyStore()
    service = TranslationReviewService(store, locale_codes=LOCALES)
    assert service.now is utc_now
    before = datetime.now(UTC)
    await service.save(ADMIN, [_write("fr", "a")])
    stamped = store.rows["fr", "a"].reviewed_at
    assert stamped.tzinfo is not None and before <= stamped <= datetime.now(UTC)
