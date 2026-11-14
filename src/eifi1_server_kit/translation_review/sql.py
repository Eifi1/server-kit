"""The translation review's table as a SQLAlchemy declarative mixin, and the store over it:
the ``sql`` extra (``docs/server-services-harmonization.md`` §6.2.3 in ``Eifi1/ui-kit``).

keksdose, kastlan and Kurvenschmiede each declare ``translation_reviews`` column for column
and write the same store around it (kastlan's ``translation_review_repository.py``); here it
is once. The app subclasses :class:`TranslationReviewColumns` on its own declarative base,
names the table and writes its migration, and builds :class:`SqlTranslationReviewStore` on the
request's ``AsyncSession``; ``put`` and ``clear`` flush and never commit.

**The strings are not here.** They are the app's catalogues; a row is a verdict on one
``(locale, key)`` — unique, so a second review replaces the first — with the wording the
reviewer was shown (``text``) and the reference beside it, so the page can tell a string that
changed since. ``reviewer_id`` is ``SET NULL``: a verdict outlives its reviewer's account.

**The area filter** is :func:`~eifi1_server_kit.translation_review.area_like_patterns`, as the
port asks, never a pattern by hand; and because SQLite's ``LIKE`` ignores ASCII case where
PostgreSQL's and :func:`~eifi1_server_kit.translation_review.in_areas` do not, each row is
held to ``in_areas`` again, so the answer is the same on every database.

**What this does not do.** It seals nothing: Kurvenschmiede sealed the four texts until its
feedback went plain, and an app that seals overrides those attributes with its own type. It
names no table and creates no schema; the account table's name is ``__accounts_table__``
(default ``"users"``), as in :mod:`~eifi1_server_kit.auth.sql`, and the unique pair is an
unnamed constraint the app's naming convention names.

Needs the ``sql`` extra (``eifi1-server-kit[sql]``).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import fields
from datetime import datetime
from typing import Any

try:
    from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, delete, false, or_, select
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Mapped, declared_attr, mapped_column
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.translation_review.sql needs SQLAlchemy: install the kit with its sql extra,"
        " eifi1-server-kit[sql]."
    ) from exc

from eifi1_server_kit.auth.sql import UtcDateTime, _assign, _new, _PointsAtAccounts, _rowcount
from eifi1_server_kit.translation_review.ports import NewVerdict, ReviewerName
from eifi1_server_kit.translation_review.scope import LIKE_ESCAPE, area_like_patterns, in_areas

__all__ = ["SqlTranslationReviewStore", "TranslationReviewColumns"]

#: A BCP 47 tag the apps ship (``"de-CH"``), with room for a script subtag.
_LOCALE_WIDTH = 16
#: An i18next key, a kit key (``kit.legal.…``) or kastlan's ``ns:key``.
_KEY_WIDTH = 255
#: A :class:`~eifi1_server_kit.translation_review.TranslationVerdict` value (``"NEEDS_CHANGE"``).
_VERDICT_WIDTH = 16


class TranslationReviewColumns(_PointsAtAccounts):
    """One verdict on one string in one locale (§6.2.3), read by the kit through
    :class:`~eifi1_server_kit.translation_review.TranslationReviewRow` as it is."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    locale: Mapped[str] = mapped_column(String(_LOCALE_WIDTH), nullable=False)
    key: Mapped[str] = mapped_column(String(_KEY_WIDTH), nullable=False)
    #: The wording the reviewer was shown, verbatim; ``""`` for a string the locale lacks.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: The reference language's wording beside it.
    reference_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: A :class:`~eifi1_server_kit.translation_review.TranslationVerdict` value.
    verdict: Mapped[str] = mapped_column(String(_VERDICT_WIDTH), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The wording the reviewer would use instead; applied by a developer, never by the app.
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)

    @declared_attr
    def reviewer_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="SET NULL"), index=True, nullable=True)

    reviewed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    @classmethod
    def table_args(cls) -> tuple[Any, ...]:
        """One verdict per locale and key, a fresh constraint per table."""
        return (UniqueConstraint("locale", "key"),)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return cls.table_args()


def _by_locale(pairs: Collection[tuple[str, str]]) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for locale, key in pairs:
        keys.setdefault(locale, []).append(key)
    return keys


class SqlTranslationReviewStore[RowT: TranslationReviewColumns]:
    """:class:`~eifi1_server_kit.translation_review.TranslationReviewStore` over a model built
    from :class:`TranslationReviewColumns`. ``accounts`` is the account model whose
    ``first_name`` and ``last_name`` the list joins: :class:`~eifi1_server_kit.auth.sql.AccountColumns`'
    model or the app's own."""

    def __init__(self, session: AsyncSession, model: type[RowT], *, accounts: type[Any]) -> None:
        self._session = session
        self._model = model
        self._accounts = accounts

    async def verdicts(
        self, locales: Sequence[str], areas: Sequence[str] | None
    ) -> Sequence[tuple[RowT, ReviewerName | None]]:
        model, accounts = self._model, self._accounts
        query = (
            select(model, accounts.id, accounts.first_name, accounts.last_name)
            .outerjoin(accounts, accounts.id == model.reviewer_id)
            .where(model.locale.in_(list(locales)))
            .order_by(model.locale, model.key)
        )
        patterns = area_like_patterns(areas)
        if patterns is not None:
            # `false()` first: an OR of nothing (no area) matches nothing, as `in_areas` says.
            query = query.where(or_(false(), *(model.key.like(pattern, escape=LIKE_ESCAPE) for pattern in patterns)))
        rows = (await self._session.execute(query)).all()
        return [
            (row, None if account is None else ReviewerName(first or "", last or ""))
            for row, account, first, last in rows
            if in_areas(row.key, areas)
        ]

    async def put(self, verdicts: Sequence[NewVerdict]) -> Sequence[RowT]:
        model = self._model
        found: dict[tuple[str, str], RowT] = {}
        for locale, keys in _by_locale([(new.locale, new.key) for new in verdicts]).items():
            for held in await self._session.scalars(select(model).where(model.locale == locale, model.key.in_(keys))):
                found[(held.locale, held.key)] = held
        written: list[RowT] = []
        for new in verdicts:
            values = {field.name: getattr(new, field.name) for field in fields(new)}
            values["verdict"] = new.verdict.value
            pair = (new.locale, new.key)
            if pair in found:
                _assign(found[pair], values)
            else:
                found[pair] = _new(model, values)
                self._session.add(found[pair])
            written.append(found[pair])
        await self._session.flush()
        return written

    async def clear(self, pairs: Collection[tuple[str, str]]) -> int:
        model = self._model
        await self._session.flush()
        cleared = 0
        for locale, keys in _by_locale(pairs).items():
            statement = delete(model).where(model.locale == locale, model.key.in_(keys))
            # "fetch": the rows this session holds leave it too, at once.
            cleared += _rowcount(await self._session.execute(statement.execution_options(synchronize_session="fetch")))
        return cleared

    async def by_reviewer(self, reviewer_id: int) -> Sequence[RowT]:
        model = self._model
        query = select(model).where(model.reviewer_id == reviewer_id).order_by(model.reviewed_at, model.id)
        return list(await self._session.scalars(query))
