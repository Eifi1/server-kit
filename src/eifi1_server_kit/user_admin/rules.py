"""The guards an admin action passes before it runs.

``docs/user-admin-harmonization.md`` §4.1–§4.2 and §6.4 in ``Eifi1/ui-kit``; the rules are
Kurvenschmiede's ``admin_service`` (last admin, yourself, the typed address) and keksdose's
``admin_user_actions_service`` (the levels). Plain values in, an
:class:`~eifi1_server_kit.user_admin.AccountError` out: the counts, the lookups and the
company scoping stay in the app.

A route runs them in this order, before it writes anything::

    kit.refuse_self(actor.id, target.id)                       # deactivate; demoting yourself
    kit.refuse_last_admin(
        target_is_admin=target.role is Role.ADMIN and target.is_active,
        active_admins=await count_active_admins(session),      # kastlan: in this company
        change_removes_admin=not body.active,
    )
    kit.require_confirmation(
        kit.confirmation_level(kit.AdminAction.DEACTIVATE),
        target_email=target.email,
        acknowledged=body.acknowledged,
        confirm_email=body.confirm_email,
    )
"""

from __future__ import annotations

from eifi1_server_kit.auth.accounts import normalise_email
from eifi1_server_kit.user_admin.actions import ConfirmationLevel
from eifi1_server_kit.user_admin.errors import AccountError, AccountErrorCode

__all__ = ["confirm_email_matches", "refuse_last_admin", "refuse_self", "require_confirmation"]


def refuse_self(actor_id: int | str, target_id: int | str) -> None:
    """Refuse an admin acting on their own account: ``409 {code: "self_action"}`` (§4.1).

    Call it where the contract says "never yourself": deactivating, and a role change that
    takes your own admin role away. Kurvenschmiede's reason: deactivating yourself is the
    last admin's lock-out one click sooner, and nobody is left on the page to undo it.
    Not for the actions that are fine on yourself (resending your own mail).
    """
    if str(actor_id) == str(target_id):
        raise AccountError(AccountErrorCode.SELF_ACTION)


def refuse_last_admin(*, target_is_admin: bool, active_admins: int, change_removes_admin: bool) -> None:
    """Refuse an action that would leave no active admin: ``409 {code: "last_admin"}``
    (§4.1, §6.4; Kurvenschmiede ``_refuse_if_last_admin``).

    * ``target_is_admin`` — the target IS an active admin now, so it is one of
      ``active_admins``. A deactivated admin is not: switching it off again removes
      nobody.
    * ``active_admins`` — the active admins in the scope the rule guards, the target
      included: the instance in keksdose and Kurvenschmiede, the company in kastlan (and
      per company, for a request that touches several).
    * ``change_removes_admin`` — the action leaves the target no longer an active admin:
      deactivating, a role change away from admin, a deletion request (keksdose: "covers
      deactivation AND the self-deletion request"), kastlan's removal from a company.

    kastlan's **one-person company** is always its own last admin; there the deletion
    request is accepted and flags the company for the operator (§6.4), so the app does
    not call this for it. Naming the companies in kastlan's answer is
    ``AccountError(AccountErrorCode.LAST_ADMIN, extra={"companies": [...]})``.
    """
    if target_is_admin and change_removes_admin and active_admins <= 1:
        raise AccountError(AccountErrorCode.LAST_ADMIN)


def confirm_email_matches(target_email: str, typed: str | None) -> bool:
    """Is ``typed`` the target's address? Both normalised
    (:func:`~eifi1_server_kit.auth.normalise_email`: trimmed, lower-cased, a ``+tag``
    kept), so case and stray spaces never fail a confirmation and nothing else passes.
    Nothing typed is never a match."""
    if typed is None or not typed.strip():
        return False
    return normalise_email(typed) == normalise_email(target_email)


def require_confirmation(
    level: ConfirmationLevel | str,
    *,
    target_email: str,
    acknowledged: bool = False,
    confirm_email: str | None = None,
) -> None:
    """Refuse a request that lacks the confirmation ``level`` asks for (§4.2; keksdose
    ``assert_confirmed``).

    * ``none`` — passes.
    * ``acknowledge`` — the request's ``acknowledged`` is true, or it carries the
      target's typed address (the stronger confirmation covers the weaker one).
    * ``type_email`` — the request carries the target's address
      (:func:`confirm_email_matches`); ``acknowledged`` alone is not enough.

    Nothing given: ``409 {code: "confirmation_required"}``. A typed address that is not
    the target's: ``409 {code: "confirmation_mismatch"}`` — the page keeps the dialog open
    and says the address is wrong, rather than asking again from scratch.
    """
    level = ConfirmationLevel(level)
    if level is ConfirmationLevel.NONE:
        return
    typed = confirm_email is not None and bool(confirm_email.strip())
    if level is ConfirmationLevel.ACKNOWLEDGE and acknowledged:
        return
    if not typed:
        raise AccountError(AccountErrorCode.CONFIRMATION_REQUIRED)
    if not confirm_email_matches(target_email, confirm_email):
        raise AccountError(AccountErrorCode.CONFIRMATION_MISMATCH)
