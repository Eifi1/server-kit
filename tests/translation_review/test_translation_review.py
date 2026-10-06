"""The translation review contract — the pure parts of keksdose's
``tests/api/test_translation_review_tokens.py`` and its review service's grant rules."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eifi1_server_kit.translation_review import (
    BATCH_MAX,
    KEKSDOSE_REVIEW_TOKEN_PREFIX,
    KIT_AREA,
    LIKE_ESCAPE,
    MAX_LIVE_REVIEW_TOKENS,
    REVIEW_TOKEN_RANDOM_LENGTH,
    ReviewGrant,
    TranslationAccessError,
    TranslationAreaError,
    TranslationLocaleError,
    TranslationReviewBatch,
    TranslationReviewClear,
    TranslationReviewOut,
    TranslationReviewsResponse,
    TranslationReviewTokenResponse,
    TranslationReviewWrite,
    TranslationVerdict,
    area_like_patterns,
    assert_allowed,
    can_review_kit,
    hash_review_token,
    in_areas,
    is_kit_key,
    is_well_formed_review_token,
    looks_like_review_token,
    new_review_token,
    normalize_areas,
    normalize_locales,
    require_kit_keys,
    review_grant,
    review_token_expiry,
    review_token_is_live,
    review_token_pattern,
    tokens_to_retire,
)

#: keksdose's vocabulary (``server_text.py:40``, ``translation_review_service.py:54``) — a parameter here.
LOCALES = ("de-CH", "en", "fr", "it")
AREAS = ("legal",)
NOW = datetime(2026, 10, 4, 9, 12, tzinfo=UTC)


@dataclass
class _User:
    translation_review_locales: list[str] | None = None
    translation_review_areas: list[str] | None = None


def _item(locale: str, key: str, verdict: str = "APPROVED") -> dict[str, str]:
    return {"locale": locale, "key": key, "text": f"{key} in {locale}", "verdict": verdict}


# ── wire shapes ─────────────────────────────────────────────────────────────


def test_the_wire_shapes() -> None:
    batch = TranslationReviewBatch.model_validate({"items": [_item("fr", "kit.form.save", "NEEDS_CHANGE")]})
    assert batch.items[0].verdict is TranslationVerdict.NEEDS_CHANGE and batch.items[0].note is None
    with pytest.raises(ValidationError):
        TranslationReviewBatch.model_validate({"items": []})
    with pytest.raises(ValidationError):
        TranslationReviewBatch.model_validate({"items": [_item("fr", "k")] * (BATCH_MAX + 1)})
    with pytest.raises(ValidationError):
        TranslationReviewWrite.model_validate(_item("fr", "k", "MAYBE"))
    with pytest.raises(ValidationError):
        TranslationReviewWrite.model_validate(_item("x", "k"))
    assert TranslationReviewClear.model_validate({"items": [{"locale": "fr", "key": "kit.a"}]}).items[0].key == "kit.a"
    out = TranslationReviewOut(
        locale="fr",
        key="kit.a",
        text="a",
        reference_text=None,
        verdict=TranslationVerdict.APPROVED,
        note=None,
        suggestion=None,
        reviewer_name=None,
        reviewed_at=NOW,
    )
    listing = TranslationReviewsResponse(locales=["fr"], areas=[KIT_AREA], reviews=[out])
    assert listing.model_dump(mode="json")["reviews"][0]["verdict"] == "APPROVED"
    assert TranslationReviewsResponse(locales=[], reviews=[]).areas is None
    assert TranslationReviewTokenResponse(token="kdr_x", expires_at=NOW).token == "kdr_x"


# ── key scope ───────────────────────────────────────────────────────────────


def test_a_review_token_reaches_the_kits_keys_and_nothing_else() -> None:
    """keksdose ``test_translation_review_tokens.py:82``: one app key in a batch refuses the
    whole batch."""
    assert is_kit_key("kit.dataTable.empty") and not is_kit_key("common.save") and not is_kit_key("kitchen.x")
    require_kit_keys(["kit.form.save", "kit.form.cancel"])
    require_kit_keys([])
    with pytest.raises(TranslationAccessError, match="kit's wording only"):
        require_kit_keys(["kit.form.cancel", "budget.rta"])


def test_areas() -> None:
    assert in_areas("legal.terms.title", ["legal"]) and in_areas("legal", ["legal"])
    assert not in_areas("legalese.x", ["legal"]) and not in_areas("common.save", ["legal"])
    assert in_areas("anything", None)


def test_in_areas_reaches_the_kits_wording_for_an_area() -> None:
    # ui-kit 0.28: the legal pages' shared sections are kit words under kit.legal.*
    assert in_areas("kit.legal.sections.warranty.title", ["legal"])
    assert not in_areas("kit.legalese.x", ["legal"]) and not in_areas("kit.legal", ["legal"])
    assert not in_areas("kit.feedbackStatus.open", ["legal"])
    assert normalize_areas([" Legal "], AREAS) == ["legal"]
    assert normalize_areas([], AREAS) is None and normalize_areas(None, AREAS) is None
    with pytest.raises(TranslationAreaError, match="Unknown area"):
        normalize_areas(["marketing"], AREAS)


def test_area_like_patterns_are_in_areas_in_sql() -> None:
    """Kurvenschmiede's finding: a listing filtered in SQL must match exactly what
    ``in_areas`` allows — run both over the same keys, through a real ``LIKE``."""
    assert area_like_patterns(["legal"]) == ["legal", "legal.%", "kit.legal.%"]
    assert area_like_patterns(None) is None and area_like_patterns([]) == []

    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA case_sensitive_like = ON")  # PostgreSQL's LIKE, which in_areas mirrors

    def sql_matches(key: str, patterns: list[str]) -> bool:
        return any(db.execute("SELECT ? LIKE ? ESCAPE ?", (key, p, LIKE_ESCAPE)).fetchone()[0] for p in patterns)

    keys = [
        "legal",
        "legal.terms.title",
        "legalese.x",
        "kit.legal.sections.warranty.title",
        "kit.legal",
        "kit.legalese.x",
        "kit.feedbackStatus.open",
        "Legal.terms",
        "price_list.title",
        "priceXlist.title",
        "kit.price_list.a",
        "100%.x",
        "100x.x",
    ]
    for areas in (["legal"], ["legal", "price_list"], ["100%"], ["kit"], []):
        patterns = area_like_patterns(areas)
        assert patterns is not None
        for key in keys:
            assert sql_matches(key, patterns) == in_areas(key, areas), (key, areas)


def test_locales_are_exact_but_case_insensitive() -> None:
    """keksdose ``translation_review_service.py:61``: ``"DE-ch"`` is ``"de-CH"``; ``"de"`` is not."""
    assert normalize_locales(["it", "DE-ch", "it"], LOCALES) == ["de-CH", "it"]
    with pytest.raises(TranslationLocaleError, match="Unknown locale 'de'"):
        normalize_locales(["de"], LOCALES)


# ── grants ──────────────────────────────────────────────────────────────────


def test_the_grant_follows_the_role() -> None:
    """keksdose ``allowed_locales`` / ``allowed_areas``: admin every locale and area, a reviewer
    the granted ones in UI order, anyone else nothing — a leftover list grants nothing."""
    user = _User(["it", "fr", "xx"], ["legal"])
    assert review_grant(user, is_admin=True, is_reviewer=False, locale_codes=LOCALES) == ReviewGrant(LOCALES, None)
    assert review_grant(user, is_admin=False, is_reviewer=True, locale_codes=LOCALES) == ReviewGrant(
        ("fr", "it"), ("legal",)
    )
    assert review_grant(user, is_admin=False, is_reviewer=False, locale_codes=LOCALES) == ReviewGrant((), None)
    assert review_grant(_User(["fr"], []), is_admin=False, is_reviewer=True, locale_codes=LOCALES).areas is None


def test_who_may_review_the_kit() -> None:
    """keksdose ``test_translation_review_tokens.py:59``: a member no; a legal-only reviewer no
    (the legal pages hold no kit key); a reviewer of every area and an admin yes. Generalised:
    an app with a ``kit`` area may grant it alone."""
    assert not can_review_kit(ReviewGrant((), None))
    assert not can_review_kit(ReviewGrant(("fr",), ("legal",)))
    assert can_review_kit(ReviewGrant(("fr",), None))
    assert can_review_kit(ReviewGrant(LOCALES, None))
    assert can_review_kit(ReviewGrant(("fr",), ("legal", "kit")))
    assert can_review_kit(ReviewGrant(("fr",), ("kit.dataTable",)))
    assert not can_review_kit(ReviewGrant(("fr",), ("kitchen",)))


def test_every_pair_inside_the_grant_or_nothing() -> None:
    """keksdose ``_assert_allowed`` / ``test_translation_review_tokens.py:108``: never wider than
    the reviewer — their locale only."""
    grant = ReviewGrant(("fr",), ("legal",))
    assert_allowed(grant, [("fr", "legal.terms.title")], LOCALES)
    with pytest.raises(TranslationLocaleError):
        assert_allowed(grant, [("xx", "legal.a")], LOCALES)
    with pytest.raises(TranslationAccessError, match="Not granted for locale 'de-CH'"):
        assert_allowed(grant, [("fr", "legal.a"), ("de-CH", "legal.a")], LOCALES)
    with pytest.raises(TranslationAccessError, match="limited to legal"):
        assert_allowed(grant, [("fr", "common.save")], LOCALES)
    assert_allowed(ReviewGrant(("fr",), None), [("fr", "common.save")], LOCALES)


# ── tokens ──────────────────────────────────────────────────────────────────


def test_the_token_format() -> None:
    """keksdose ``test_translation_review_tokens.py:51``: ``kdr_`` + ``token_urlsafe(32)``."""
    raw = new_review_token(KEKSDOSE_REVIEW_TOKEN_PREFIX)
    assert raw.startswith("kdr_") and len(raw) == 4 + REVIEW_TOKEN_RANDOM_LENGTH == 47
    assert re.fullmatch(r"kdr_[A-Za-z0-9_-]{43}", raw)
    assert is_well_formed_review_token(raw, "kdr_") and looks_like_review_token(raw, "kdr_")
    assert not is_well_formed_review_token(raw + "x", "kdr_") and not is_well_formed_review_token(raw, "ksr_")
    assert new_review_token("kdr_") != raw, "random every time"
    # The prefix is each app's.
    assert new_review_token("ksr_").startswith("ksr_")
    assert review_token_pattern("ka_").pattern.startswith("^ka_")
    for bad in ("", "kdr", "k-r_", "kdr__x"):
        with pytest.raises(ValueError, match="prefix"):
            new_review_token(bad)


def test_only_the_hash_is_stored() -> None:
    """keksdose ``test_translation_review_tokens.py:72``."""
    raw = new_review_token("kdr_")
    stored = hash_review_token(raw)
    assert stored != raw and len(stored) == 64 and raw not in stored
    assert stored == hash_review_token(raw)
    assert hash_review_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_a_new_token_past_the_cap_retires_the_oldest() -> None:
    """keksdose ``test_translation_review_tokens.py:130``."""
    assert MAX_LIVE_REVIEW_TOKENS == 5
    assert tokens_to_retire(["a", "b", "c", "d", "e"]) == ["a"]
    assert tokens_to_retire(["a", "b", "c", "d", "e", "f", "g"]) == ["a", "b", "c"]
    assert tokens_to_retire(["a", "b"]) == []
    assert tokens_to_retire([], cap=1) == []


def test_it_stops_at_expiry_revocation_and_a_password_reset() -> None:
    """keksdose ``test_translation_review_tokens.py:121`` and ``:138``."""
    created = NOW - timedelta(hours=1)
    expires = review_token_expiry(created)
    assert expires == created + timedelta(hours=12)

    def live(
        now: datetime = NOW,
        *,
        revoked_at: datetime | None = None,
        sessions_invalid_before: datetime | None = None,
        naive: bool = False,
    ) -> bool:
        def at(moment: datetime) -> datetime:
            return moment.replace(tzinfo=None) if naive else moment

        return review_token_is_live(
            now=now,
            expires_at=at(expires),
            revoked_at=revoked_at,
            created_at=at(created),
            sessions_invalid_before=sessions_invalid_before,
        )

    assert live()
    assert not live(expires), "expiry is exclusive"
    assert not live(revoked_at=NOW)
    assert not live(sessions_invalid_before=created + timedelta(seconds=1)), "minted before a password reset"
    assert live(sessions_invalid_before=created)
    # SQLite hands back naive datetimes: read as UTC.
    assert live(naive=True)


def test_the_lifetime_is_bounded() -> None:
    assert review_token_expiry(NOW, 72) == NOW + timedelta(hours=72)
    for hours in (0, 73):
        with pytest.raises(ValueError, match="hours"):
            review_token_expiry(NOW, hours)
