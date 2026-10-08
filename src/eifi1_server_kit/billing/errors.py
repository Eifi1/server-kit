"""Billing's coded refusals: the switch, the read-only gate, configuration, the webhook's
signature, and a plan's limit.

``docs/billing-harmonization.md`` §2.9, §3.3, §3.4, §5 and §12.3 in ``Eifi1/ui-kit``. The
same shape as the demo's and the account's refusals: a ``code`` the page switches on, never
the English ``detail``, answered ``{"detail": …, "code": …}`` — plus a plan limit's named
fields — by :func:`~eifi1_server_kit.errors.install_contract_error_handlers`. Both classes
are in :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`.

Two classes, because the two answers differ in kind: :class:`BillingError` refuses what
billing's STATE forbids (off, lapsed, not configured), :class:`PlanLimitError` what a plan's
SIZE forbids, and names the dimension, the plan, the limit and the count (§3.4). **The order
between them is the app's** (§12.3): the demo's 403 first, then ``billing_read_only``, and
only then ``plan_limit`` — a lapsed payer creating gets ``billing_read_only``, never
``plan_limit``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

__all__ = [
    "BILLING_ERROR_DETAIL",
    "BILLING_ERROR_STATUS",
    "PLAN_LIMIT_CODE",
    "BillingError",
    "BillingErrorCode",
    "PlanLimitError",
]

#: The ``code`` of :class:`PlanLimitError` (§3.4). keksdose's ``plan_budget_limit`` becomes
#: this over two releases (§12.16): release N's frontend accepts both, release N+1's server
#: switches.
PLAN_LIMIT_CODE = "plan_limit"


class BillingErrorCode(enum.StrEnum):
    """The ``code`` of a billing refusal. A ``StrEnum``, so ``str(code)`` is the wire value;
    the ui-kit's ``KitErrorCode`` reads the same words."""

    #: The switch is off (§2.9, §4): every billing route but ``GET /billing/status``, and the
    #: webhook, answer 404 — "not there", as a switched-off demo does (``demo_disabled``).
    #: kastlan's 503 moves here.
    BILLING_DISABLED = "billing_disabled"
    #: The payer is not in good standing and this is a write (§3.3, §12.1): 402. Reading,
    #: exporting, signing in, billing itself and the account's own settings always pass. The
    #: ``detail`` never names the payer's status — a guest in a lapsed owner's budget reads
    #: it too (§12.6).
    BILLING_READ_ONLY = "billing_read_only"
    #: Billing is on, but what this request needs is missing from the settings — a price id
    #: for the plan, currency and interval asked for, or a plan for the price a webhook
    #: names: 503, a fault of the deployment the client cannot fix. A webhook answered 503
    #: is retried by the provider, so the event applies once the setting is there.
    BILLING_NOT_CONFIGURED = "billing_not_configured"
    #: The webhook's signature is missing, malformed, stale or wrong (§5): 400. Only the
    #: provider ever sees it.
    INVALID_SIGNATURE = "invalid_signature"


#: Each code's status.
BILLING_ERROR_STATUS: Mapping[BillingErrorCode, int] = {
    BillingErrorCode.BILLING_DISABLED: 404,
    BillingErrorCode.BILLING_READ_ONLY: 402,
    BillingErrorCode.BILLING_NOT_CONFIGURED: 503,
    BillingErrorCode.INVALID_SIGNATURE: 400,
}

#: The English ``detail`` of each code, for logs and API clients; the kit's pages show their
#: own words for the code. ``billing_read_only``'s says nothing about whose plan or why
#: (§12.6).
BILLING_ERROR_DETAIL: Mapping[BillingErrorCode, str] = {
    BillingErrorCode.BILLING_DISABLED: "Billing is not available",
    BillingErrorCode.BILLING_READ_ONLY: "Read-only for now: changes need an active plan",
    BillingErrorCode.BILLING_NOT_CONFIGURED: "Billing is not configured for this",
    BillingErrorCode.INVALID_SIGNATURE: "Invalid webhook signature",
}


class BillingError(ValueError):
    """A billing refusal: ``status_code`` and ``code`` from :class:`BillingErrorCode`.

    ``BillingError(BillingErrorCode.BILLING_DISABLED)`` answers 404 ``{"detail": "Billing is
    not available", "code": "billing_disabled"}`` through
    :func:`~eifi1_server_kit.errors.install_contract_error_handlers`; pass ``detail`` to say
    more. A ``ValueError`` like every kit refusal, so an app-wide ``ValueError`` → 400
    handler does not swallow its status.
    """

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 402
    code: BillingErrorCode

    def __init__(self, code: BillingErrorCode, detail: str | None = None) -> None:
        self.code = BillingErrorCode(code)
        self.status_code = BILLING_ERROR_STATUS[self.code]
        super().__init__(detail or BILLING_ERROR_DETAIL[self.code])


class PlanLimitError(ValueError):
    """A create beyond the plan's limit (§3.4): ``402 {detail, code: "plan_limit",
    dimension, plan, limit, used}``.

    Raised by :func:`~eifi1_server_kit.billing.check_limit`. A limit gates creation only:
    a downgrade never deletes or hides what is over the new limit, it only stops creating
    more — so this is the one place a limit ever refuses. The page offers "Choose a plan"
    or, where the operator grants plans by hand, "Ask for more" (§3.4); it reads the
    fields, never the English ``detail``. ``used`` is the count BEFORE the create.
    """

    status_code: int = 402
    code: str = PLAN_LIMIT_CODE
    extra: Mapping[str, object]

    def __init__(self, *, dimension: str, plan: str, limit: int, used: int, detail: str | None = None) -> None:
        self.dimension = dimension
        self.plan = plan
        self.limit = limit
        self.used = used
        self.extra = {"dimension": dimension, "plan": plan, "limit": limit, "used": used}
        super().__init__(detail or f"The {plan} plan's limit on {dimension} is {limit}")
