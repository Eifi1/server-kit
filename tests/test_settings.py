"""Settings on the server (settings contract §6): one PATCH rule, and the account's language."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route

from eifi1_server_kit.auth import PROFILE_NOT_NULLABLE, ProfileUpdate
from eifi1_server_kit.errors import CONTRACT_ERRORS, install_contract_error_handlers
from eifi1_server_kit.settings import (
    PATCH_NULL_CODE,
    PatchNullError,
    apply_patch,
    canonical_locale,
    parse_accept_language,
    profile_update_model,
)

#: kastlan's UI languages, in its menu's order.
KASTLAN = ("de-CH", "en", "fr", "it")
#: Kurvenschmiede's seven.
KURVENSCHMIEDE = ("de-CH", "en", "es", "fr", "it", "hu", "zh")


@dataclass
class _User:
    first_name: str = "Ada"
    last_name: str = "Example"
    locale: str = "en"


class _CompanySettings(BaseModel):
    """kastlan's company settings: two fields that can't be empty, one that clears."""

    model_config = ConfigDict(extra="forbid")

    default_language: str | None = None
    default_account_country: str | None = None
    qr_iban: str | None = None


class _Preferences(BaseModel):
    """keksdose's push preferences: ``null`` resets a type to its default."""

    model_config = ConfigDict(extra="forbid")

    overspend: bool | None = None
    digest: bool | None = None


# ── apply_patch (§6.1) ──────────────────────────────────────────────────────


def test_an_omitted_field_keeps_its_value() -> None:
    user = _User()
    written = apply_patch(user, ProfileUpdate(locale="fr"), not_nullable=PROFILE_NOT_NULLABLE)
    assert written == {"locale": "fr"}
    assert (user.first_name, user.last_name, user.locale) == ("Ada", "Example", "fr")


def test_an_explicit_null_clears_a_nullable_field() -> None:
    row: dict[str, Any] = {"default_language": "de-CH", "default_account_country": "CH", "qr_iban": "CH93…"}
    body = _CompanySettings.model_validate({"qr_iban": None, "default_language": "fr"})
    written = apply_patch(row, body, not_nullable=("default_language", "default_account_country"))
    assert written == {"default_language": "fr", "qr_iban": None}, "in the model's order"
    assert row == {"default_language": "fr", "default_account_country": "CH", "qr_iban": None}


def test_a_null_on_a_field_that_cant_be_empty_is_a_422_and_writes_nothing() -> None:
    row: dict[str, Any] = {"default_language": "de-CH", "default_account_country": "CH", "qr_iban": "x"}
    body = _CompanySettings.model_validate({"qr_iban": None, "default_account_country": None, "default_language": None})
    with pytest.raises(PatchNullError) as caught:
        apply_patch(row, body, not_nullable=("default_language", "default_account_country"))
    error = caught.value
    assert isinstance(error, ValueError) and error.status_code == 422 and error.code == PATCH_NULL_CODE
    assert error.fields == ("default_language", "default_account_country")
    assert error.extra == {"fields": ["default_language", "default_account_country"]}
    assert str(error) == "default_language, default_account_country cannot be null; leave them out to keep them"
    assert row == {"default_language": "de-CH", "default_account_country": "CH", "qr_iban": "x"}, "nothing written"
    assert str(PatchNullError(["locale"])) == "locale cannot be null; leave it out to keep it"


def test_a_null_resets_to_the_fields_default_where_one_is_given() -> None:
    """keksdose's ``/push/preferences``: ``null`` resets a type to its default, not to NULL."""
    row: dict[str, Any] = {"overspend": False, "digest": False}
    written = apply_patch(
        row,
        _Preferences.model_validate({"overspend": None, "digest": True}),
        not_nullable=(),
        defaults={"overspend": True},
    )
    assert written == {"overspend": True, "digest": True} and row == written
    # A field without a default in `defaults` still clears to None.
    assert apply_patch({}, _Preferences.model_validate({"digest": None}), not_nullable=()) == {"digest": None}


def test_put_bodies_follow_the_same_rule() -> None:
    """The rule is about the body, not the verb: a PUT body leaves out what it doesn't send."""
    row: dict[str, Any] = {"overspend": True, "digest": True}
    apply_patch(row, _Preferences.model_validate({"digest": False}), not_nullable=())
    assert row == {"overspend": True, "digest": False}


def test_a_model_that_lets_unknown_fields_through_is_refused() -> None:
    class Loose(BaseModel):
        digest: bool | None = None

    with pytest.raises(TypeError, match="extra='forbid'"):
        apply_patch({}, Loose(digest=True), not_nullable=())


def test_a_typo_in_the_guard_is_a_programming_error() -> None:
    body = _CompanySettings(default_language="fr")
    with pytest.raises(ValueError, match="no such field on _CompanySettings: default_langauge"):
        apply_patch({}, body, not_nullable=("default_langauge",))
    with pytest.raises(ValueError, match="no such field"):
        apply_patch({}, body, not_nullable=(), defaults={"colour": "teal"})
    with pytest.raises(ValueError, match="not both: qr_iban"):
        apply_patch({}, body, not_nullable=("qr_iban",), defaults={"qr_iban": ""})


async def test_the_refusal_is_answered_with_its_code_and_fields() -> None:
    assert PatchNullError in CONTRACT_ERRORS

    async def patch(_request: Request) -> None:
        # model_construct: a body whose model has no null check of its own.
        body = ProfileUpdate.model_construct(_fields_set={"first_name"}, first_name=None)
        apply_patch(_User(), body, not_nullable=PROFILE_NOT_NULLABLE)

    app = Starlette(routes=[Route("/auth/me", patch, methods=["PATCH"])])
    install_contract_error_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/auth/me")
    assert (response.status_code, response.json()) == (
        422,
        {
            "detail": "first_name cannot be null; leave it out to keep it",
            "code": "not_nullable",
            "fields": ["first_name"],
        },
    )


# ── canonical_locale (§6.2) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("de-CH", "de-CH"),
        ("de_ch", "de-CH"),
        ("DE-ch", "de-CH"),
        ("  en  ", "en"),
        ("EN", "en"),
        # The language's offered tag: one German.
        ("de", "de-CH"),
        ("de-DE", "de-CH"),
        ("de-AT", "de-CH"),
        ("fr-CH", "fr"),
        ("it_IT", "it"),
        ("en-GB", "en"),
        # Not offered, empty, or no tag at all.
        ("pt", None),
        ("hu", None),
        ("", None),
        (None, None),
        ("e", None),
        ("de-CH; DROP", None),
        ("*", None),
        ("de--CH", None),
    ],
)
def test_canonical_locale(tag: str | None, expected: str | None) -> None:
    assert canonical_locale(tag, KASTLAN) == expected


def test_canonical_locale_answers_the_apps_own_spelling() -> None:
    assert canonical_locale("zh-hans", ("de-CH", "zh-Hans")) == "zh-Hans"
    assert canonical_locale("zh", ("de-CH", "zh-Hans")) == "zh-Hans"
    # Kurvenschmiede's retired Germans are read as its one German.
    assert canonical_locale("de-informal", KURVENSCHMIEDE) == "de-CH"
    assert canonical_locale("de-CH-informal", KURVENSCHMIEDE) == "de-CH"
    # With two offered tags of one language, the first offered wins.
    assert canonical_locale("en", ("en-GB", "en-US")) == "en-GB"
    assert canonical_locale("en-us", ("en-GB", "en-US")) == "en-US"
    # A bare offered language answers its regional variants.
    assert canonical_locale("es-419", KURVENSCHMIEDE) == "es"


def test_offered_is_a_sequence_of_tags() -> None:
    with pytest.raises(TypeError, match="not one tag"):
        canonical_locale("de", "de-CH")
    with pytest.raises(ValueError, match="at least one"):
        canonical_locale("de", ())


# ── parse_accept_language (§6.2) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7, *;q=0.5", "fr"),
        ("en-US,en;q=0.9", "en"),
        ("pt-BR, de-DE;q=0.4", "de-CH"),
        ("it;q=0.2, en;q=0.9", "en"),
        # Equal q: the earlier entry.
        ("it, fr", "it"),
        ("it;q=0.5, fr;q=0.5", "it"),
        # q=0 is "not this one"; a malformed q is skipped, not trusted.
        ("en;q=0, fr;q=0.1", "fr"),
        ("en;q=abc, fr;q=0.1", "fr"),
        ("en;q=2, fr;q=0.1", "fr"),
        ("en;q=nan, fr;q=0.1", "fr"),
        ("en; Q=0.3 , fr;q=0.2", "en"),
        ("en;level=1, fr;q=0.9", "en"),
        # Nothing offered, the wildcard alone, or nothing at all.
        ("pt, es", None),
        ("*", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_accept_language(header: str | None, expected: str | None) -> None:
    assert parse_accept_language(header, KASTLAN) == expected


def test_parse_accept_language_checks_offered_even_for_a_wildcard() -> None:
    with pytest.raises(TypeError):
        parse_accept_language("*", "de-CH")


# ── ProfileUpdate with the app's languages (§7.2) ───────────────────────────


def test_profile_update_model_keeps_everything_and_canonicalises_the_locale() -> None:
    model = profile_update_model(KASTLAN)
    assert issubclass(model, ProfileUpdate) and model.__name__ == "ProfileUpdate"
    assert model.offered_locales == KASTLAN
    assert model(locale="de_ch").locale == "de-CH"
    assert model.model_validate({"locale": "DE"}).locale == "de-CH"
    assert model.model_validate({"locale": "en-GB", "first_name": " Ada "}).model_dump(exclude_unset=True) == {
        "locale": "en",
        "first_name": "Ada",
    }
    with pytest.raises(ValidationError, match="locale must be one of de-CH, en, fr, it"):
        model.model_validate({"locale": "pt"})
    with pytest.raises(ValidationError, match="locale must be one of"):
        model.model_validate({"locale": "not a tag"})
    # Today's rules stay: unknown fields and nulls refused, a non-string still a type error.
    with pytest.raises(ValidationError, match="Extra inputs"):
        model.model_validate({"role": "ADMIN"})
    with pytest.raises(ValidationError, match="cannot be null"):
        model.model_validate({"locale": None})
    with pytest.raises(ValidationError, match="valid string"):
        model.model_validate({"locale": 7})
    assert profile_update_model(KURVENSCHMIEDE, name="KsProfileUpdate").__name__ == "KsProfileUpdate"


def test_an_app_that_adds_fields_subclasses_and_sets_the_knob() -> None:
    class KeksdoseProfileUpdate(ProfileUpdate):
        offered_locales = ("de-CH", "en")
        reporting_currency: str | None = None

    update = KeksdoseProfileUpdate.model_validate({"locale": "de", "reporting_currency": None})
    assert update.locale == "de-CH" and update.model_fields_set == {"locale", "reporting_currency"}
    user: dict[str, Any] = {"locale": "en", "reporting_currency": "CHF"}
    apply_patch(user, update, not_nullable=PROFILE_NOT_NULLABLE)
    assert user == {"locale": "de-CH", "reporting_currency": None}


def test_the_plain_profile_update_checks_only_the_shape_as_before() -> None:
    assert ProfileUpdate.offered_locales is None
    assert ProfileUpdate(locale="pt").locale == "pt"
    with pytest.raises(ValidationError):
        ProfileUpdate.model_validate({"locale": "de_ch"})
