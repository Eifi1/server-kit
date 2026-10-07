"""The coded refusals of user administration and the account's own settings.

``docs/user-admin-harmonization.md`` §4.1, §6.4 and §9.6 in ``Eifi1/ui-kit``. The same
shape as the sign-in refusals (:class:`~eifi1_server_kit.auth.AuthError`): a ``code`` the
page switches on, never the English ``detail``, and
:func:`eifi1_server_kit.errors.install_contract_error_handlers` answers it as ``{"detail":
…, "code": …}`` — plus the refusal's :attr:`AccountError.extra` fields, for an answer that
names something (kastlan's companies).

A sibling of ``AuthError``, not a subclass: these refuse an action on an account that is
signed in and allowed to act, which is a different family from "who are you". Every one
is a ``409``: the request is well formed, and it is the state of the instance (or of the
confirmation) that refuses it — Kurvenschmiede's reasoning for its last-admin and
self refusals, and keksdose's status for a missing confirmation
(``admin_user_actions_service.assert_confirmed``).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

__all__ = ["ACCOUNT_ERROR_DETAIL", "ACCOUNT_ERROR_STATUS", "AccountError", "AccountErrorCode"]


class AccountErrorCode(enum.StrEnum):
    """The ``code`` of a refusal. A ``StrEnum``, so ``str(code)`` is the wire value."""

    #: The action would leave nobody to run the instance — or, in kastlan, a company:
    #: deactivating, demoting or deleting the last active admin (§4.1, §6.4).
    LAST_ADMIN = "last_admin"
    #: An admin acting on their own account where that is never allowed: deactivating
    #: themselves, or taking their own admin role away (§4.1) — the same lock-out as the
    #: last admin's, one click sooner.
    SELF_ACTION = "self_action"
    #: kastlan: the account belongs to other companies too, so a company admin may not
    #: deactivate it (an account-wide switch); the page offers "Remove from company"
    #: instead (§4.1, §9.6).
    OTHER_COMPANIES = "other_companies"
    #: keksdose: a deletion request from the owner of a household that still has another
    #: member row, whose budgets erasing the owner would wipe (§6.4, §9.2).
    HOUSEHOLD_HAS_MEMBERS = "household_has_members"
    #: The action's confirmation level asks for an acknowledgement or a typed address,
    #: and the request carries neither (§4.2).
    CONFIRMATION_REQUIRED = "confirmation_required"
    #: The typed address is not the target account's (§4.2).
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    #: The CURRENT password is wrong on a signed-in route: an email change or a deletion
    #: request (§6.2, §6.4). 400, not 401 — an app's client treats a 401 as an ended
    #: session and signs the user out, which would be the wrong answer to a typo.
    PASSWORD_INCORRECT = "password_incorrect"


#: Each code's status: ``409``, see the module docstring — except ``password_incorrect``,
#: a ``400`` so no client reads it as an ended session.
ACCOUNT_ERROR_STATUS: Mapping[AccountErrorCode, int] = {
    **dict.fromkeys(AccountErrorCode, 409),
    AccountErrorCode.PASSWORD_INCORRECT: 400,
}

#: The English ``detail`` of each code, for logs and API clients; the pages show the
#: kit's own text for the code. None of them echoes an address.
ACCOUNT_ERROR_DETAIL: Mapping[AccountErrorCode, str] = {
    AccountErrorCode.LAST_ADMIN: "This would leave no active admin",
    AccountErrorCode.SELF_ACTION: "You cannot do this to your own account",
    AccountErrorCode.OTHER_COMPANIES: "This account belongs to other companies too",
    AccountErrorCode.HOUSEHOLD_HAS_MEMBERS: "The household still has other members",
    AccountErrorCode.CONFIRMATION_REQUIRED: "This action needs an explicit confirmation",
    AccountErrorCode.CONFIRMATION_MISMATCH: "The typed address does not match the account",
    AccountErrorCode.PASSWORD_INCORRECT: "The current password is not correct",
}

#: Keys the error body sets itself; :attr:`AccountError.extra` may not override them.
_RESERVED_KEYS = frozenset({"detail", "code"})


class AccountError(ValueError):
    """A coded refusal: ``status_code`` and ``code`` from :class:`AccountErrorCode`.

    ``AccountError(AccountErrorCode.LAST_ADMIN)`` is a 409 with the code's English detail;
    pass ``detail`` to say more. ``extra`` adds JSON fields to the answer for a refusal
    that names something the page shows — kastlan's per-company last-admin check answers
    ``AccountError(AccountErrorCode.LAST_ADMIN, extra={"companies": ["Example AG"]})`` →
    ``{"detail": …, "code": "last_admin", "companies": ["Example AG"]}`` (§6.4). It may
    not set ``detail`` or ``code``.

    A ``ValueError`` like every kit refusal, and registered in
    :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`, so an app-wide ``ValueError`` → 400
    handler does not swallow its status.
    """

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 409
    code: AccountErrorCode
    extra: Mapping[str, object]

    def __init__(
        self,
        code: AccountErrorCode,
        detail: str | None = None,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        self.code = AccountErrorCode(code)
        self.status_code = ACCOUNT_ERROR_STATUS[self.code]
        fields = dict(extra or {})
        if clash := sorted(_RESERVED_KEYS & fields.keys()):
            raise ValueError(f"the error body sets {', '.join(clash)} itself")
        self.extra = fields
        super().__init__(detail or ACCOUNT_ERROR_DETAIL[self.code])
