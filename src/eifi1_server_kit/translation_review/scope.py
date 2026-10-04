"""Who may review which wording: locales, areas, and the ``kit.`` scope of a review token.

Lifted from keksdose ``backend/keksdose/domain/services/translation_review_service.py:37-125``
and ``translation_review_token_service.py:33-60``, with the app's vocabulary as
parameters — its ``UI_LOCALE_CODES`` (keksdose: de-CH, en, fr, it) and ``REVIEW_AREAS``
(keksdose: ``legal``) — and the user read through :class:`ReviewerProfile`. Roles stay the
app's: it says whether the caller is an admin (every locale) or a reviewer (the granted
ones); keksdose also passes ``is_reviewer=False`` for a demo session.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

#: The only keys a review token may read or judge: the kit's own wording.
KIT_KEY_PREFIX = "kit."
#: The area the kit's keys form — what a review token's listing is narrowed to.
KIT_AREA = "kit"


class TranslationLocaleError(ValueError):
    """A locale code the app does not ship (→ 422)."""


class TranslationAreaError(ValueError):
    """An area name outside the app's vocabulary (→ 422)."""


class TranslationAccessError(PermissionError):
    """A locale, an area or a key the caller has not been granted (→ 403)."""


class ReviewerProfile(Protocol):
    """The two grant fields of an app's user (keksdose ``User.translation_review_locales`` /
    ``translation_review_areas``)."""

    @property
    def translation_review_locales(self) -> Sequence[str] | None: ...

    @property
    def translation_review_areas(self) -> Sequence[str] | None: ...


@dataclass(frozen=True, slots=True)
class ReviewGrant:
    """What a caller may review: ``locales`` in the UI's order; ``areas`` ``None`` = every area."""

    locales: tuple[str, ...]
    areas: tuple[str, ...] | None


def is_kit_key(key: str) -> bool:
    """Is ``key`` the kit's own wording (keksdose ``translation_review_token_service.py:49``)?"""
    return key.startswith(KIT_KEY_PREFIX)


def require_kit_keys(keys: Iterable[str]) -> None:
    """A review token reaches the kit's keys and nothing else: one other key in a batch
    refuses the whole batch (keksdose ``translations_router.py:65`` ``_kit_keys_only``)."""
    if not all(is_kit_key(key) for key in keys):
        raise TranslationAccessError("A review token reaches the kit's wording only")


def in_areas(key: str, areas: Sequence[str] | None) -> bool:
    """Whether ``key`` lies in one of ``areas`` — the area itself or anything under ``<area>.``
    (``None`` = every area; keksdose ``translation_review_service.py:91``)."""
    return areas is None or any(key == area or key.startswith(f"{area}.") for area in areas)


def normalize_locales(raw: Iterable[str], locale_codes: Sequence[str]) -> list[str]:
    """``raw`` as shipped codes, de-duplicated, in ``locale_codes`` order (keksdose ``:61``).

    Case-insensitive (``"DE-ch"`` is ``"de-CH"``) because a grant is typed by a person, but
    never fuzzy: ``"de"`` is not ``"de-CH"``. An unknown code is a :class:`TranslationLocaleError`.
    """
    by_lower = {code.lower(): code for code in locale_codes}
    picked: set[str] = set()
    for value in raw:
        code = by_lower.get(value.strip().lower())
        if code is None:
            raise TranslationLocaleError(f"Unknown locale {value!r}; expected one of {', '.join(locale_codes)}")
        picked.add(code)
    return [code for code in locale_codes if code in picked]


def normalize_areas(raw: Iterable[str] | None, review_areas: Sequence[str]) -> list[str] | None:
    """``raw`` as known areas in ``review_areas`` order; ``None`` or empty = every area (keksdose ``:78``)."""
    if raw is None:
        return None
    picked: set[str] = set()
    for value in raw:
        area = value.strip().lower()
        if area not in review_areas:
            raise TranslationAreaError(f"Unknown area {value!r}; expected one of {', '.join(review_areas)}")
        picked.add(area)
    return [area for area in review_areas if area in picked] or None


def review_grant(
    profile: ReviewerProfile,
    *,
    is_admin: bool,
    is_reviewer: bool,
    locale_codes: Sequence[str],
) -> ReviewGrant:
    """The caller's grant (keksdose ``allowed_locales`` / ``allowed_areas``, ``:96-112``).

    An admin: every locale, every area. A reviewer: exactly the granted locales (in UI
    order, unknown ones ignored) and the granted areas. Anyone else: nothing — the role is
    the permission and the locales are its scope, so a leftover list grants nothing.
    """
    if is_admin:
        return ReviewGrant(locales=tuple(locale_codes), areas=None)
    if not is_reviewer:
        return ReviewGrant(locales=(), areas=None)
    granted = set(profile.translation_review_locales or ())
    areas = tuple(profile.translation_review_areas or ()) or None
    return ReviewGrant(locales=tuple(code for code in locale_codes if code in granted), areas=areas)


def can_review_kit(grant: ReviewGrant) -> bool:
    """May this grant mint or use a review token? Some locale, and an area limit that does not
    shut the kit out (keksdose ``translation_review_token_service.py:57``, generalised).

    keksdose refused ANY area limit, because its one area (``legal``) holds no ``kit.`` key.
    The general rule: refused when areas are granted and none of them covers ``kit.`` —
    an app with a ``kit`` area may hand a token to a kit-only reviewer.
    """
    if not grant.locales:
        return False
    return grant.areas is None or any(area == KIT_AREA or area.startswith(KIT_KEY_PREFIX) for area in grant.areas)


def assert_allowed(grant: ReviewGrant, pairs: Iterable[tuple[str, str]], locale_codes: Sequence[str]) -> None:
    """Every ``(locale, key)`` inside the grant, or nothing is written (keksdose ``_assert_allowed``, ``:115``).

    An unknown locale is a :class:`TranslationLocaleError` (422); a locale or key outside
    the grant a :class:`TranslationAccessError` (403).
    """
    allowed = set(grant.locales)
    for locale, key in pairs:
        if locale not in locale_codes:
            raise TranslationLocaleError(f"Unknown locale {locale!r}")
        if locale not in allowed:
            raise TranslationAccessError(f"Not granted for locale {locale!r}")
        if not in_areas(key, grant.areas):
            raise TranslationAccessError(f"Not granted for {key!r}; limited to {', '.join(grant.areas or ())}")
