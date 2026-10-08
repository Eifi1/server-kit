"""The plan catalogue and its limits, and money as minor units.

``docs/billing-harmonization.md`` §3.1, §3.4, §4 and §12.15–§12.17 in ``Eifi1/ui-kit``.

**The catalogue lives in code** (§3.1, keksdose's rule: the dict that sets the limits is
the only list). Each app declares its plans as :class:`PlanSpec` and builds one mapping with
:func:`plan_catalogue`; the provider's price ids live in the settings
(:class:`~eifi1_server_kit.billing.BillingSettings`), display names and feature lines in
the app's i18n — never the provider's.

**A limit gates creation only** (§3.4): :func:`check_limit` before a create, and nowhere
else. A downgrade never deletes, hides or locks what is over the new limit; it only stops
creating more, and :func:`dimensions_over_limit` says so to the operator (§6).

**Money crosses the wire as integer minor units plus an ISO currency, never as a float**
(§4): ``990`` and ``"CHF"`` are CHF 9.90. Prices are GROSS, MWST/VAT included, as Swiss
consumer law (PBV) wants (§12.17). :func:`minor_to_decimal` is the one conversion, for a
mail or a log line; pages format the minor units with the kit's ``formatMoney``.
"""

from __future__ import annotations

import enum
import re
import types
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from eifi1_server_kit.billing.errors import PlanLimitError

__all__ = [
    "CURRENCY_EXPONENTS",
    "BillingCurrency",
    "BillingInterval",
    "Currency",
    "Interval",
    "MinorUnits",
    "PlanCode",
    "PlanSpec",
    "check_limit",
    "dimensions_over_limit",
    "minor_to_decimal",
    "normalize_plan",
    "plan_catalogue",
]


class BillingCurrency(enum.StrEnum):
    """The currencies the apps charge in (§2.6): CHF and EUR. The payer's is picked at
    checkout from the plan's prices, defaulting from the account's stored currency where the
    app keeps one (keksdose ``reporting_currency``), else the locale (§4, §12.18)."""

    CHF = "CHF"
    EUR = "EUR"


class BillingInterval(enum.StrEnum):
    """How often a plan is paid (§3.1). Pages lead with the yearly price: about 5 % + 0.50
    per transaction makes a small monthly price expensive (§12.17)."""

    MONTH = "month"
    YEAR = "year"


#: ISO 4217's minor-unit exponent per currency: how many decimal places one minor unit is.
#: CHF and EUR are 2 (rappen and cents). A currency the apps don't charge in is not here,
#: and :func:`minor_to_decimal` refuses it rather than guessing 2.
CURRENCY_EXPONENTS: Mapping[str, int] = types.MappingProxyType({"CHF": 2, "EUR": 2})

#: A plan code on the wire: lowercase, a letter first, then letters, digits, ``_`` or ``-``
#: (§3.1, §12.16).
_PLAN_CODE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def normalize_plan(code: str) -> str:
    """A plan code as stored and sent: trimmed and lowercase (§3.1, §12.16).

    Codes become lowercase by a data migration, and **reads compare case-insensitively**:
    keksdose's ``admin_actions`` history keeps its uppercase ``FREE`` / ``PRO``, and this
    reads them as ``free`` / ``pro``. Not a code — empty, too long (32), or anything but
    letters, digits, ``_`` and ``-`` after a letter — is a :class:`ValueError`, so inside a
    Pydantic model it is a 422. Whether the code is one of the app's plans is the
    catalogue's question (:func:`plan_catalogue`), not this one's.
    """
    normalised = code.strip().lower()
    if not _PLAN_CODE.match(normalised):
        raise ValueError(f"a plan code is a letter, then letters, digits, '_' or '-' (at most 32): {code!r}")
    return normalised


def _plan_code(value: object) -> object:
    return normalize_plan(value) if isinstance(value, str) else value


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


#: A plan code, normalised on the way in (:func:`normalize_plan`).
PlanCode = Annotated[str, BeforeValidator(_plan_code)]
#: A :class:`BillingCurrency`, any case on the way in (``"chf"`` is CHF).
Currency = Annotated[BillingCurrency, BeforeValidator(_upper)]
#: A :class:`BillingInterval`, any case on the way in.
Interval = Annotated[BillingInterval, BeforeValidator(_lower)]
#: An amount in minor units: a whole, non-negative ``int``. Strict, so a float — or a
#: ``bool`` — is refused rather than rounded (§4).
MinorUnits = Annotated[int, Field(strict=True, ge=0)]
_Limit = Annotated[int, Field(strict=True, ge=0)]


class PlanSpec(BaseModel):
    """One plan in the app's catalogue (§3.1)::

        FREE = PlanSpec(code="free", limits={"budgets": 1}, sort=0)
        PRO = PlanSpec(
            code="pro",
            limits={"budgets": 5},
            prices={("CHF", "year"): 3000, ("EUR", "year"): 3000, ("CHF", "month"): 300},
            sort=1,
        )
        PLANS = plan_catalogue([FREE, PRO])

    * ``code`` — lowercase and stable on the wire (:func:`normalize_plan`);
    * ``limits`` — dimension → the most a payer may have, ``None`` for unlimited. Each app
      names its own (§12.15): keksdose ``budgets``; Kurvenschmiede ``curves``; kastlan
      ``units``, ``seats`` (staff only) and ``storage`` (bytes, all company files at
      plaintext size). A dimension the plan doesn't name is a programming error, not
      "unlimited" (:func:`check_limit`);
    * ``prices`` — ``(currency, interval)`` → the GROSS price in minor units (§12.17); a
      combination left out is not sold. The provider's price id for each lives in the
      settings;
    * ``sort`` — the page's order, cheapest first.

    Frozen: the catalogue is a constant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: PlanCode
    limits: dict[str, _Limit | None] = Field(default_factory=dict)
    prices: dict[tuple[Currency, Interval], MinorUnits] = Field(default_factory=dict)
    sort: int = 0

    def limit(self, dimension: str) -> int | None:
        """The limit on ``dimension``, ``None`` for unlimited. A dimension this plan doesn't
        name is a :class:`ValueError`: a typo must not switch a limit off without a word."""
        if dimension not in self.limits:
            raise ValueError(f"the {self.code} plan names no limit for {dimension!r}: {', '.join(self.limits)}")
        return self.limits[dimension]

    def price(self, currency: BillingCurrency | str, interval: BillingInterval | str) -> int | None:
        """The gross price in minor units for ``currency`` and ``interval``; ``None`` where
        the plan is not sold that way (a free plan, a monthly price left out)."""
        return self.prices.get((BillingCurrency(currency.strip().upper()), BillingInterval(interval.strip().lower())))


def plan_catalogue(plans: Iterable[PlanSpec]) -> Mapping[str, PlanSpec]:
    """The app's plans as one read-only mapping, code → plan, in ``sort`` order (§3.1).

    Look a plan up with the code normalised: ``PLANS.get(normalize_plan(code))``. Refused,
    as :class:`ValueError`, on the first import: no plans, two plans with one code, and
    plans that name different dimensions — a ``budget`` beside a ``budgets`` would leave
    one plan without a limit there.
    """
    ordered = sorted(plans, key=lambda plan: (plan.sort, plan.code))
    if not ordered:
        raise ValueError("a catalogue holds at least one plan")
    catalogue: dict[str, PlanSpec] = {}
    for plan in ordered:
        if plan.code in catalogue:
            raise ValueError(f"two plans have the code {plan.code!r}")
        catalogue[plan.code] = plan
    dimensions = set(ordered[0].limits)
    for plan in ordered[1:]:
        if set(plan.limits) != dimensions:
            raise ValueError(
                f"every plan names the same dimensions: {ordered[0].code} has {sorted(dimensions)}, "
                f"{plan.code} has {sorted(plan.limits)}"
            )
    return types.MappingProxyType(catalogue)


def check_limit(plan: PlanSpec, dimension: str, used: int, *, adding: int = 1) -> None:
    """Refuse a create that would take the payer past ``plan``'s limit on ``dimension``
    (§3.4): :class:`~eifi1_server_kit.billing.PlanLimitError`, ``402 {code: "plan_limit",
    dimension, plan, limit, used}``.

    ``used`` is the payer's count BEFORE the create (keksdose: owned, non-deleted budgets;
    shared-in budgets and the preview never count, §12.15); ``adding`` is how much the
    create adds — 1 for a budget or a unit, the file's size for kastlan's ``storage``. The
    create is refused when ``used + adding`` exceeds the limit; an unlimited dimension
    (``None``) always passes. A copy counts; an admin transfer or an erasure hand-over never
    checks a limit (§12.15).

    **Call it after the read-only gate** (§12.3): a lapsed payer creating gets
    ``billing_read_only``, never ``plan_limit``.
    """
    if used < 0 or adding < 0:
        raise ValueError("used and adding are counts: zero or more")
    limit = plan.limit(dimension)
    if limit is not None and used + adding > limit:
        raise PlanLimitError(dimension=dimension, plan=plan.code, limit=limit, used=used)


def dimensions_over_limit(plan: PlanSpec, usage: Mapping[str, int]) -> list[str]:
    """The dimensions where ``usage`` already exceeds ``plan``'s limits, in the plan's order.

    For a plan change (§6): a downgrade below what the payer has is not an error and
    nothing is cleaned up — only the next create is refused — so the answer says so
    (:class:`~eifi1_server_kit.billing.PlanChangeResponse` ``over_limit``) rather than let an
    operator discover it. A dimension missing from ``usage`` counts as 0.
    """
    return [
        dimension for dimension, limit in plan.limits.items() if limit is not None and usage.get(dimension, 0) > limit
    ]


def minor_to_decimal(amount: int, currency: BillingCurrency | str) -> Decimal:
    """``amount`` minor units of ``currency`` as an exact :class:`~decimal.Decimal`:
    ``minor_to_decimal(990, "CHF") == Decimal("9.90")``.

    Exact by construction — the exponent of :data:`CURRENCY_EXPONENTS` shifts the integer,
    no float is ever involved (§4). A ``float`` or a ``bool`` amount is a
    :class:`TypeError`; a currency not in the table is a :class:`ValueError`, never a
    guessed exponent.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError(f"an amount in minor units is an int, not {type(amount).__name__}")
    code = str(currency).strip().upper()
    if code not in CURRENCY_EXPONENTS:
        raise ValueError(f"no minor-unit exponent for {currency!r}: {', '.join(CURRENCY_EXPONENTS)}")
    return Decimal(amount).scaleb(-CURRENCY_EXPONENTS[code])
