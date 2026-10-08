"""The admin actions on one account, and how much confirmation each needs.

``docs/user-admin-harmonization.md`` §4.2–§4.3 in ``Eifi1/ui-kit``. One vocabulary,
:class:`AdminAction`, for both: the ``action`` column of the ``admin_actions`` table
(:func:`~eifi1_server_kit.user_admin.admin_action_record`) and the key the confirmation
level is decided by (:func:`confirmation_level`).

**The level is decided on the server** (keksdose ``admin_user_actions_service``). The page
is not a control: a hand-written POST skips its dialog, so the server states each
action's level, the page asks for what it says (the kit's ``AdminActionConfirm``), and the
server refuses a request that lacks it
(:func:`~eifi1_server_kit.user_admin.require_confirmation`). ``type_email`` asks for the
TARGET's address, not the admin's password: what is unproven is not who the admin is —
they passed the admin gate — but which row they are on.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

__all__ = ["CONFIRMATION_LEVELS", "AdminAction", "ConfirmationLevel", "confirmation_level"]


class AdminAction(enum.StrEnum):
    """What an ``admin_actions`` row records (§4.3). A ``StrEnum``: ``str(action)`` is the
    stored value."""

    DEACTIVATE = "deactivate"
    REACTIVATE = "reactivate"
    #: A role change — keksdose's and Kurvenschmiede's one role, kastlan's roles in a
    #: company.
    ROLE = "role"
    #: kastlan: "Remove from company", where deactivating would reach other companies
    #: (§4.1).
    MEMBERSHIP_REMOVE = "membership_remove"
    PASSWORD_CHANGE_REQUIRE = "password_change_require"
    PASSWORD_CHANGE_WITHDRAW = "password_change_withdraw"
    MAIL_VERIFICATION = "mail_verification"
    MAIL_RESET = "mail_reset"
    #: The reviewer scope (``PUT /admin/users/{id}/reviewer``).
    REVIEWER = "reviewer"
    INVITE = "invite"
    INVITE_RESEND = "invite_resend"
    INVITE_REVOKE = "invite_revoke"
    #: Kurvenschmiede's transfer of owned work — by an admin, or by the erasure job at
    #: day 30 with no actor (§2.7).
    TRANSFER = "transfer"
    #: The user asked to delete their own account; the actor IS the target (§6.4).
    DELETION_REQUEST = "deletion_request"
    #: An admin reactivated the account before its erasure date (§6.4).
    DELETION_CANCEL = "deletion_cancel"
    #: The account was erased — by the scheduled job (no actor) or by kastlan's operator.
    ERASE = "erase"
    #: A manual plan change or free grant (``docs/billing-harmonization.md`` §6): the row's
    #: ``detail`` is ``{from, to, comped_until, counts}``. keksdose's own ``"plan"`` since
    #: 0.30, now the kit's word; for kastlan the target is a company (``company_id`` set,
    #: ``target_user_id`` empty).
    PLAN = "plan"


class ConfirmationLevel(enum.StrEnum):
    """How much the admin says before an action runs (§4.2; keksdose ``Confirmation``)."""

    #: One click.
    NONE = "none"
    #: A checkbox naming the consequence.
    ACKNOWLEDGE = "acknowledge"
    #: The target's address, typed.
    TYPE_EMAIL = "type_email"

    @property
    def rank(self) -> int:
        """``0`` / ``1`` / ``2``: the order a stronger level outranks a weaker one in."""
        return _RANK[self]


_RANK: Mapping[ConfirmationLevel, int] = {
    ConfirmationLevel.NONE: 0,
    ConfirmationLevel.ACKNOWLEDGE: 1,
    ConfirmationLevel.TYPE_EMAIL: 2,
}

#: The contract's level per action (§4.2). ``type_email``: deactivate, erase (the
#: operator's "erase now"), transfer — and the user's own deletion request, whose body
#: carries the typed address (§6.4). ``acknowledge``: force a password change, change a
#: role, change a plan (billing §6). Everything else: ``none``.
CONFIRMATION_LEVELS: Mapping[AdminAction, ConfirmationLevel] = {
    AdminAction.DEACTIVATE: ConfirmationLevel.TYPE_EMAIL,
    AdminAction.REACTIVATE: ConfirmationLevel.NONE,
    AdminAction.ROLE: ConfirmationLevel.ACKNOWLEDGE,
    AdminAction.MEMBERSHIP_REMOVE: ConfirmationLevel.NONE,
    AdminAction.PASSWORD_CHANGE_REQUIRE: ConfirmationLevel.ACKNOWLEDGE,
    AdminAction.PASSWORD_CHANGE_WITHDRAW: ConfirmationLevel.NONE,
    AdminAction.MAIL_VERIFICATION: ConfirmationLevel.NONE,
    AdminAction.MAIL_RESET: ConfirmationLevel.NONE,
    AdminAction.REVIEWER: ConfirmationLevel.NONE,
    AdminAction.INVITE: ConfirmationLevel.NONE,
    AdminAction.INVITE_RESEND: ConfirmationLevel.NONE,
    AdminAction.INVITE_REVOKE: ConfirmationLevel.NONE,
    AdminAction.TRANSFER: ConfirmationLevel.TYPE_EMAIL,
    AdminAction.DELETION_REQUEST: ConfirmationLevel.TYPE_EMAIL,
    AdminAction.DELETION_CANCEL: ConfirmationLevel.NONE,
    AdminAction.ERASE: ConfirmationLevel.TYPE_EMAIL,
    AdminAction.PLAN: ConfirmationLevel.ACKNOWLEDGE,
}


def confirmation_level(
    action: AdminAction | str,
    *,
    at_least: ConfirmationLevel | str = ConfirmationLevel.NONE,
) -> ConfirmationLevel:
    """The confirmation ``action`` needs: the contract's level (:data:`CONFIRMATION_LEVELS`),
    or ``at_least`` when that is stronger.

    The contract's level is a FLOOR. An app may ask for more, never less — keksdose
    raises the reset mail and the forced password change to ``type_email`` for an account
    whose encryption key a password opens (``admin_user_actions_service
    .required_confirmation``)::

        level = kit.confirmation_level(
            kit.AdminAction.PASSWORD_CHANGE_REQUIRE,
            at_least=kit.ConfirmationLevel.TYPE_EMAIL if ctx.has_password_wrap else kit.ConfirmationLevel.NONE,
        )

    An unknown action or level is a :class:`ValueError`.
    """
    contract = CONFIRMATION_LEVELS[AdminAction(action)]
    floor = ConfirmationLevel(at_least)
    return floor if floor.rank > contract.rank else contract
