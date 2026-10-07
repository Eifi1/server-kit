"""Settings on the server: one rule for every settings body, and the account's language.

``docs/settings-harmonization.md`` §6 in ``Eifi1/ui-kit``. Two things every app's settings
endpoints share; the endpoints themselves, their models and their columns stay in the app.

**One PATCH rule** (§6.1). Every settings write body — ``PATCH /auth/me``, kastlan's
``PATCH /company/settings``, keksdose's ``PUT /push/settings`` and ``/push/preferences`` —
follows the same five rules, whatever its verb: the rule is about the BODY, so keksdose's
push writes stay PUT and still follow it.

1. an omitted field keeps its value;
2. an explicit ``null`` clears a nullable field — back to "not set", or to the field's
   default;
3. an explicit ``null`` on a field that can't be empty is a 422, never a silent skip;
4. unknown fields are a 422 (``extra="forbid"``), so a typo is not a silent no-op;
5. the answer is the whole resource after the change, so the client replaces its copy
   instead of merging.

:func:`apply_patch` is rules 1–3 over a Pydantic model's ``model_fields_set`` — the one
place that tells "left out" from "sent as null" — and refuses a model that would break
rule 4. Rule 5 is the endpoint's ``response_model``. The three apps each had their own
reading of ``null`` before (keksdose's push preferences dropped it, its push settings
kept the old cadence, kastlan skipped it on a required field, Kurvenschmiede refused it),
which is why it is code now and not a convention.

**The account's language** (§6.2). The account holds ONE ``locale``, canonical, from the
app's offered languages: :func:`canonical_locale` replaces keksdose's
``canonical_locale``, kastlan's ``normalize_lang`` and Kurvenschmiede's ``LanguageIn``.
:func:`parse_accept_language` reads a browser's header for a mail to someone without an
account; for everyone with one, mails and pushes use the account's locale and the server
never guesses from the request. ``ProfileUpdate`` takes the app's offered languages through
its ``offered_locales`` knob, or :func:`profile_update_model` builds that class in one call.
"""

from __future__ import annotations

import re
import types
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from eifi1_server_kit.auth.schemas import ProfileUpdate

__all__ = [
    "PATCH_NULL_CODE",
    "PatchNullError",
    "apply_patch",
    "canonical_locale",
    "parse_accept_language",
    "profile_update_model",
]

#: The ``code`` of :class:`PatchNullError`, named after ``apply_patch``'s ``not_nullable``.
PATCH_NULL_CODE = "not_nullable"

#: A language tag after case and ``_`` are normalised: a 2–3 letter language, then subtags
#: of 1–8 letters or digits (BCP 47's shape — ``de-ch``, ``zh-hans``, ``es-419``, and
#: Kurvenschmiede's retired ``de-ch-informal``). Anything else is no tag at all.
_TAG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{1,8})*$")


class PatchNullError(ValueError):
    """An explicit ``null`` on a field that can't be empty (§6.1 rule 3) → ``422``.

    ``{"detail": "first_name cannot be null; leave it out to keep it", "code":
    "not_nullable", "fields": ["first_name"]}`` through
    :func:`~eifi1_server_kit.errors.install_contract_error_handlers`. Every refused field is
    named, in the model's order. A ``ValueError`` like every kit refusal, so an app-wide
    ``ValueError`` → 400 handler cannot swallow its status.
    """

    status_code: int = 422
    code: str = PATCH_NULL_CODE
    extra: Mapping[str, object]

    def __init__(self, fields: Sequence[str]) -> None:
        self.fields = tuple(fields)
        self.extra = {"fields": list(self.fields)}
        names = ", ".join(self.fields)
        if len(self.fields) == 1:
            detail = f"{names} cannot be null; leave it out to keep it"
        else:
            detail = f"{names} cannot be null; leave them out to keep them"
        super().__init__(detail)


def apply_patch(
    obj: object,
    update: BaseModel,
    *,
    not_nullable: Iterable[str],
    defaults: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Write a settings body onto ``obj`` under §6.1's rules 1–3; the fields written.

    For a PATCH and a PUT body alike — the rule is about the body, not the verb::

        changes = apply_patch(user, payload, not_nullable=PROFILE_NOT_NULLABLE)

    * **Only the fields the client sent** are written (``update.model_fields_set``):
      a field left out keeps its value (rule 1), even where the model's default is
      ``None``. That is the whole difference from ``model_dump()``, which cannot tell "left
      out" from "sent as null".
    * **A null clears** (rule 2): it is written as ``None`` — or as ``defaults[name]``,
      for a field whose "not set" is a value (keksdose's push preferences: ``null`` resets
      a type to its default).
    * **A null on a ``not_nullable`` field** is a :class:`PatchNullError` (rule 3, 422),
      raised before ANYTHING is written, so a refused body never leaves ``obj`` half
      changed. ``not_nullable`` is required on purpose: an empty one says "every field may
      be cleared" out loud, and a forgotten one would otherwise reach the database as a
      ``NOT NULL`` violation — a 500.

    ``obj`` is an ORM row or any object (``setattr``), or a mutable mapping (item
    assignment). Values are written as validated: a nested model stays a model, so dump it
    first for a JSON column. Answers ``{name: value}`` for every field written, in the
    model's order — for the app's audit or its "did the locale change" check.

    Programming errors, raised as :class:`TypeError` / :class:`ValueError` so a test meets
    them on the first call: a model that doesn't forbid unknown fields (rule 4 — without
    ``extra="forbid"`` a typo would be a silent no-op), and a ``not_nullable`` or
    ``defaults`` name that is no field of the model (a typo there would switch the guard off
    without a word), or one name in both.
    """
    model = type(update)
    if model.model_config.get("extra") != "forbid":
        raise TypeError(f"{model.__name__} must refuse unknown fields: model_config = ConfigDict(extra='forbid')")
    fields = model.model_fields
    refused_null = frozenset(not_nullable)
    resets = dict(defaults or {})
    if unknown := sorted((refused_null | resets.keys()) - fields.keys()):
        raise ValueError(f"no such field on {model.__name__}: {', '.join(unknown)}")
    if both := sorted(refused_null & resets.keys()):
        raise ValueError(f"a field is either not nullable or reset to a default, not both: {', '.join(both)}")

    sent = [name for name in fields if name in update.model_fields_set]
    if nulls := [name for name in sent if name in refused_null and getattr(update, name) is None]:
        raise PatchNullError(nulls)

    written: dict[str, object] = {}
    for name in sent:
        value = getattr(update, name)
        if value is None and name in resets:
            value = resets[name]
        if isinstance(obj, MutableMapping):
            obj[name] = value
        else:
            setattr(obj, name, value)
        written[name] = value
    return written


def _offered_index(offered: Sequence[str]) -> dict[str, str]:
    """``{normalised tag: the tag as the app writes it}``, in the app's order."""
    if isinstance(offered, str):
        raise TypeError("offered is a sequence of tags, not one tag: ('de-CH', 'en', …)")
    index: dict[str, str] = {}
    for tag in offered:
        index.setdefault(tag.strip().replace("_", "-").lower(), tag)
    if not index:
        raise ValueError("an app offers at least one locale")
    return index


def canonical_locale(tag: str | None, offered: Sequence[str]) -> str | None:
    """The app's own spelling of ``tag`` from ``offered``, or ``None`` (§6.2).

    * **Case and ``_`` normalised**: ``de_ch`` and ``DE-ch`` are ``de-CH``, ``EN`` is
      ``en``; the answer is the offered tag exactly as the app writes it, because every
      client compares the stored string to its own language code.
    * **An offered tag matches exactly**; otherwise **the language's offered tag**: ``de``,
      ``de-DE`` and Kurvenschmiede's retired ``de-informal`` are all the offered ``de-CH``
      (there is one German), ``fr-CH`` is ``fr``. With two offered tags of one language,
      the first in ``offered`` wins.
    * **Anything else is** ``None``: a language the app does not offer, an empty value, or
      a string that is no language tag at all. ``PATCH /auth/me`` answers it with a 422
      (``ProfileUpdate.offered_locales``); a reader of a stored value falls back to the
      app's default.

    ``offered`` is the app's languages in its menu's order — kastlan's ``UI_LANGUAGES``,
    Kurvenschmiede's seven codes. Lifted from keksdose ``domain/server_text.py``
    (``canonical_locale``), kastlan ``domain/i18n.py`` (``normalize_lang``) and
    Kurvenschmiede ``domain/languages.py`` (``LanguageIn``), which each did half of this.
    """
    index = _offered_index(offered)
    requested = (tag or "").strip().replace("_", "-").lower()
    if not _TAG.match(requested):
        return None
    if requested in index:
        return index[requested]
    language = requested.split("-", 1)[0]
    for normalised, written in index.items():
        if normalised.split("-", 1)[0] == language:
            return written
    return None


def _quality(params: str) -> float | None:
    """The ``q`` of one ``Accept-Language`` entry: ``1.0`` when absent, ``None`` when it is
    not a number from 0 to 1 (a malformed entry is skipped, not trusted)."""
    for param in params.split(";"):
        key, _, value = param.partition("=")
        if key.strip().lower() == "q":
            try:
                quality = float(value.strip())
            except ValueError:
                return None
            # NaN fails both comparisons, so it is refused here too.
            return quality if 0.0 <= quality <= 1.0 else None
    return 1.0


def parse_accept_language(header: str | None, offered: Sequence[str]) -> str | None:
    """The best offered locale in an ``Accept-Language`` header, by q-value; else ``None``.

    For a mail to somebody WITHOUT an account — an invitation, a reset for an unknown
    address answered the same way (§6.2). Somebody with an account gets the account's
    locale: the server never guesses from the request then.

    Every entry is read through :func:`canonical_locale`, so ``de-DE`` finds the offered
    ``de-CH``. The highest ``q`` wins and, at equal ``q``, the earlier entry (kastlan
    ``parse_accept_language``); ``q=0`` means "not this one", and an entry with a malformed
    ``q`` or the wildcard ``*`` is skipped — ``None`` then lets the caller use its default.
    """
    if not header:
        return None
    _offered_index(offered)  # the same programming errors as canonical_locale, even for a header of only "*"
    ranked: list[tuple[float, int, str]] = []
    for position, entry in enumerate(header.split(",")):
        tag, _, params = entry.partition(";")
        quality = _quality(params)
        if quality is None or quality <= 0.0:
            continue
        locale = canonical_locale(tag, offered)
        if locale is not None:
            ranked.append((-quality, position, locale))
    return min(ranked)[2] if ranked else None


def profile_update_model(offered: Sequence[str], *, name: str = "ProfileUpdate") -> type[ProfileUpdate]:
    """``ProfileUpdate`` for the app's languages, in one call (settings §7.2)::

        ProfileUpdate = profile_update_model(("de-CH", "en", "fr", "it"))

    A subclass of :class:`~eifi1_server_kit.auth.ProfileUpdate` with ``offered_locales``
    set: everything it already did stays (unknown fields and nulls refused, names trimmed),
    and the locale goes through :func:`canonical_locale` — ``de_ch`` is stored as ``de-CH``,
    and a language the app doesn't offer is a 422. Kurvenschmiede's ``LanguageIn`` override
    and kastlan's own check go.

    **An app that adds fields subclasses instead** and sets the knob — mypy takes no
    function's result as a base class::

        class ProfileUpdate(kit.ProfileUpdate):
            offered_locales = LANGUAGES
            reporting_currency: CurrencyCode | None = None
    """
    # Imported here: auth.schemas imports canonical_locale from this module.
    from eifi1_server_kit.auth.schemas import ProfileUpdate

    locales = tuple(_offered_index(offered).values())
    namespace = {
        "__module__": __name__,
        "__qualname__": name,
        "__doc__": f"``PATCH /auth/me`` for the locales {', '.join(locales)} (:func:`profile_update_model`).",
        "offered_locales": locales,
    }
    # new_class, not type(): the class goes through Pydantic's metaclass like a written one.
    return cast(
        "type[ProfileUpdate]", types.new_class(name, (ProfileUpdate,), exec_body=lambda ns: ns.update(namespace))
    )
