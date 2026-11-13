"""The admin's flows over the scope and the account layer: the roster, the four actions, the
reviewer scope, the log, the overview, the test mail and the operator's erasure.

Layer 2 (``docs/auth-harmonization.md`` §12.3–§12.5, ``docs/user-admin-harmonization.md``
§3–§6, ``docs/admin-harmonization.md`` §2.4, §9.2 in ``Eifi1/ui-kit``), on layer 1's rules:
:func:`~eifi1_server_kit.user_admin.parse_roster_query`, :func:`~eifi1_server_kit.user_admin.refuse_self`,
:func:`~eifi1_server_kit.user_admin.confirmation_level`,
:func:`~eifi1_server_kit.user_admin.require_confirmation`,
:func:`~eifi1_server_kit.user_admin.admin_action_record`, :class:`~eifi1_server_kit.user_admin.AdminAttention`.
No FastAPI: :mod:`eifi1_server_kit.user_admin.router` builds one per request.

**What the kit settles where the three apps differ** (each app's adopt note names its own):

* **No change, no row**: an action that changes nothing (deactivating an inactive account,
  the role it has, a password change already required, the same review scope) answers the
  row as it is, asks no confirmation and writes no log row (keksdose and Kurvenschmiede;
  kastlan wrote one every time).
* **The order of the refusals**: the account (``user_not_found``, ``demo_refused``), then
  ``self_action``, the scope's account-wide refusal (``other_companies``), ``last_admin``,
  then the confirmation the server decided (keksdose's and Kurvenschmiede's; kastlan
  confirmed before ``last_admin``). Every refusal is coded.
* **Deactivating** ends every session (:meth:`~eifi1_server_kit.auth.service.AccountService.end_sessions`:
  the rows, the ``iat`` cut-off where the row has one, the pending address change, the
  ``sessions_ended`` hook) and clears nothing else; **reactivating** withdraws a pending
  deletion first (``deletion_cancel``, then ``reactivate``: Kurvenschmiede's and kastlan's
  two rows).
* **A forced password change ends every session** (keksdose, Kurvenschmiede; §12.5 item 7),
  is allowed on a deactivated account (it waits for the return) and on oneself.
* **The admin's mail** refuses a deactivated account (``account_inactive``) and a verified
  address's verification (``email_already_verified``), is charged to the recipient's public
  window, commits its link before it mails it, and logs ``{sent}``; its link is answered only
  while mail goes to the console.
* **Demo accounts** are never on the roster and every action on one is ``demo_refused``
  (Kurvenschmiede, kastlan); a demo admin sees no count and sends no test mail.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from eifi1_server_kit.auth.limits import AuthLimiters
from eifi1_server_kit.auth.ports import AccountRow, AdminActionLog, InvitationStore, UserStore
from eifi1_server_kit.auth.service import AccountPolicy, AccountService, SessionsEndedReason
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.limiter import charge
from eifi1_server_kit.mail import MAIL_TEST_KIND, Mailer, MailTestResult, test_mail_message
from eifi1_server_kit.translation_review.scope import normalize_areas, normalize_locales
from eifi1_server_kit.user_admin.actions import AdminAction, ConfirmationLevel, confirmation_level
from eifi1_server_kit.user_admin.attention import AdminAttention
from eifi1_server_kit.user_admin.audit import admin_action_record
from eifi1_server_kit.user_admin.deletion import DeletionMode
from eifi1_server_kit.user_admin.errors import AccountError, AccountErrorCode
from eifi1_server_kit.user_admin.roster import SORT_KEYS, STATE_TOKENS, parse_roster_query
from eifi1_server_kit.user_admin.rules import refuse_self, require_confirmation
from eifi1_server_kit.user_admin.schemas import (
    MAIL_BACKEND_CONSOLE,
    SUMMARY_ACTIVE_ADMINS,
    ActionConfirmation,
    AdminActionRow,
    AdminUserRow,
    GroupRef,
    InvitationStatus,
    MailKind,
    PersonRef,
    ReviewerResult,
    ReviewerUpdate,
    invitation_status,
)
from eifi1_server_kit.user_admin.scope import AdminScope

if TYPE_CHECKING:
    from eifi1_server_kit.feedback.service import FeedbackService

__all__ = [
    "ADMIN_ACTIONS",
    "AdminPolicy",
    "ErasureRun",
    "UserAdminHooks",
    "UserAdminService",
    "erase_due_accounts",
]

logger = logging.getLogger("eifi1_server_kit.user_admin")

#: The actions the admin router takes on a person, whose levels the roster answers (§3.1, §4.2)
#: — the operator's ``erase`` only where an operator erases.
ADMIN_ACTIONS: tuple[AdminAction, ...] = (
    AdminAction.DEACTIVATE,
    AdminAction.REACTIVATE,
    AdminAction.ROLE,
    AdminAction.PASSWORD_CHANGE_REQUIRE,
    AdminAction.PASSWORD_CHANGE_WITHDRAW,
    AdminAction.MAIL_VERIFICATION,
    AdminAction.MAIL_RESET,
    AdminAction.REVIEWER,
)

#: The overview's "new" (admin §2.4): the last seven days.
_NEW_WINDOW = timedelta(days=7)
#: The test mail's 429 sentence (Kurvenschmiede's words).
_TEST_MAIL_THROTTLED = "Three test mails in ten minutes — please try again later."


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminPolicy:
    """The app's roles and vocabulary the admin router reads, one per app. The admin role and
    the default role are the account layer's (``AccountPolicy.admin_role``,
    ``default_role``) and must be among ``roles``; the router checks when it is built."""

    #: Every role the app has, in the order its page lists them.
    roles: tuple[str, ...] = ("ADMIN", "MEMBER", "REVIEWER")
    #: The roles the role change gives; ``None``: every role but the reviewer role, which
    #: comes with its scope through ``/reviewer`` (Kurvenschmiede; kastlan passes its own,
    #: without ``tenant``).
    grantable: tuple[str, ...] | None = None
    #: The role a review scope gives; ``None``: the scope is the account's columns alone and
    #: the role stays (kastlan's platform).
    reviewer_role: str | None = "REVIEWER"
    #: The locales a reviewer may be given (the app's ``UI_LOCALE_CODES``), in the order the
    #: grant is stored; empty: no reviewer route.
    review_locales: tuple[str, ...] = ()
    #: The review areas (the app's ``REVIEW_AREAS``).
    review_areas: tuple[str, ...] = ()
    #: The roster's state tokens: the contract's but ``invited`` (an invitation is its own
    #: row); an app adds its own with a roster that answers them.
    states: tuple[str, ...] = tuple(token for token in STATE_TOKENS if token != "invited")
    #: The roster's sort keys; an app adds its own with a roster that sorts by them.
    sort_keys: tuple[str, ...] = SORT_KEYS
    #: The app's own actions on a person, whose levels the roster answers beside the kit's
    #: (Kurvenschmiede's ``transfer`` and ``plan``, keksdose's ``plan``): its page asks for
    #: them from the same ``levels`` (§3.1).
    app_actions: tuple[AdminAction, ...] = ()

    def grantable_roles(self, accounts: AccountPolicy) -> frozenset[str]:
        """The roles the role change gives — one source with the invitations' (0.30):
        ``grantable``, else ``AccountPolicy.invitable_roles``, else every role but the reviewer
        role."""
        if self.grantable is not None:
            return frozenset(self.grantable)
        if accounts.invitable_roles is not None:
            return frozenset(accounts.invitable_roles)
        return frozenset(role for role in self.roles if role != self.reviewer_role)

    def check(self, accounts: AccountPolicy) -> None:
        """Refuse a policy that names a role the app does not have, and a ``grantable`` that
        disagrees with ``AccountPolicy.invitable_roles`` (:class:`ValueError`): an admin gives
        the roles an invitation may name, and no other."""
        named = {
            "the admin role": accounts.admin_role,
            "the default role": accounts.default_role,
            "the reviewer role": self.reviewer_role,
            **{f"a grantable role ({role})": role for role in self.grantable or ()},
            **{f"an invitable role ({role})": role for role in accounts.invitable_roles or ()},
        }
        for what, role in named.items():
            if role is not None and role not in self.roles:
                raise ValueError(f"AdminPolicy.roles lacks {what}, {role!r}: {', '.join(self.roles)}")
        invitable = accounts.invitable_roles
        if self.grantable is not None and invitable is not None and set(self.grantable) != set(invitable):
            raise ValueError(
                f"AdminPolicy.grantable {self.grantable} and AccountPolicy.invitable_roles {invitable} disagree: "
                "an admin gives the roles an invitation may name; set one of them"
            )


type _Extras = Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class UserAdminHooks[UserT: AccountRow = Any]:
    """What differs between the apps' admins (§12.2 seams 5 and 6, §12.4), as optional
    coroutines the app's hook dependency answers per request, on the request's own session.
    Each may raise a kit refusal (``AccountError``) to refuse the step. The erasure's hooks
    are the account layer's (``AuthHooks.erase``, ``purge_files``): the job runs them too."""

    #: Seam 6: one roster row as the app's ``row_model`` (keksdose's plan and key custody,
    #: Kurvenschmiede's owned counts, kastlan's roles); without it the row from the account.
    to_row: Callable[[UserT], Awaitable[BaseModel]] | None = None
    #: ``GET /admin/attention`` (admin §2.4): the app's counts — ``feedback_waiting`` and its
    #: ``extra`` tiles — over the kit's, which it may replace (kastlan's ``None``).
    attention: Callable[[UserT], Awaitable[_Extras]] | None = None
    #: The level an action asks on this account, when the app raises it (keksdose: a password
    #: that opens a key makes the forced change and the reset ``type_email``); never lower
    #: than the contract's.
    confirmation: Callable[[UserT, AdminAction], Awaitable[ConfirmationLevel | str | None]] | None = None
    #: A role change, before it is written: ``(admin, target, before, after)``; refuse to
    #: refuse it (kastlan's seats: a staff role takes one).
    role_changing: Callable[[UserT, UserT, frozenset[str], frozenset[str]], Awaitable[None]] | None = None
    #: A reactivation, before it is written: ``(admin, target)``; refuse to refuse it
    #: (kastlan's seats again).
    activating: Callable[[UserT, UserT], Awaitable[None]] | None = None
    #: The log's groups: their names by id (Kurvenschmiede's teams, kastlan's companies), a
    #: deleted one's from its ``group_delete`` row.
    group_names: Callable[[Sequence[int]], Awaitable[Mapping[int, str | None]]] | None = None


@dataclass(frozen=True, slots=True)
class ErasureRun:
    """What one run of the ``after_days`` job did (:func:`erase_due_accounts`)."""

    erased: tuple[int, ...] = ()
    #: Not due, gone or reactivated meanwhile: nothing done, nothing wrong.
    skipped: tuple[int, ...] = ()
    #: Failed and rolled back; logged, and asked again on the next run.
    failed: tuple[int, ...] = ()


async def erase_due_accounts(ids: Iterable[int], erase_one: Callable[[int], Awaitable[bool]]) -> ErasureRun:
    """The ``after_days`` job (user-admin §6.4): **one account per transaction**, so a failure
    holds back no other. ``ids`` are the candidates
    (:meth:`~eifi1_server_kit.user_admin.ports.RosterStore.deletion_requested`, read on a
    session of their own); ``erase_one`` is the app's: it opens a transaction, builds the
    request-less ``AccountService`` on it, awaits
    :meth:`~eifi1_server_kit.auth.service.AccountService.erase_if_due` and commits — rolling
    back if that raises. An account that raises is logged and counted as failed; the rest go
    on. The kit never commits: the transaction is the app's (keksdose's ``erase_due``)::

        async def erase_one(user_id: int) -> bool:
            async with sessions() as session, session.begin():
                return await accounts_on(session).erase_if_due(user_id, feedback=feedback_on(session))

        async with sessions() as session:
            ids = await SqlRosterStore(session, User).deletion_requested()
        run = await erase_due_accounts(ids, erase_one)
    """
    erased: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    for user_id in ids:
        try:
            (erased if await erase_one(user_id) else skipped).append(user_id)
        except Exception:
            logger.exception("erasing account %s failed; it is asked again on the next run", user_id)
            failed.append(user_id)
    return ErasureRun(tuple(erased), tuple(skipped), tuple(failed))


def _only(roles: frozenset[str]) -> str | None:
    return next(iter(roles)) if len(roles) == 1 else None


class UserAdminService[UserT: AccountRow]:
    """The admin's flows for one request, over the scope and the account layer's service and
    stores (the same ones its routes use, so a write here is in the request's transaction)."""

    def __init__(
        self,
        *,
        accounts: AccountService[UserT, Any, Any],
        scope: AdminScope[UserT],
        users: UserStore[UserT],
        log: AdminActionLog[Any],
        invitations: InvitationStore[Any],
        mailer: Mailer,
        mail_backend: str,
        account_policy: AccountPolicy,
        policy: AdminPolicy,
        limiters: AuthLimiters,
        hooks: UserAdminHooks[UserT] | None = None,
        feedback: FeedbackService[Any] | None = None,
        role_sets: bool = False,
        row_model: type[AdminUserRow] = AdminUserRow,
        action_row_model: type[AdminActionRow] = AdminActionRow,
        now: Clock = utc_now,
    ) -> None:
        self.accounts = accounts
        self.scope = scope
        self._users = users
        self._log = log
        self._invitations = invitations
        self._mailer = mailer
        self._backend = mail_backend
        self._accounts_policy = account_policy
        self._policy = policy
        self._limiters = limiters
        self._hooks: UserAdminHooks[UserT] = hooks or UserAdminHooks()
        self._feedback = feedback
        self._role_sets = role_sets
        self._row_model = row_model
        self._action_row_model = action_row_model
        self._now = now

    # --- the pieces ------------------------------------------------------------------------

    async def target(self, user_id: int) -> UserT:
        """The account an action is on: in the scope (``404 user_not_found``), and a person
        (``403 demo_refused``: a demo account is the demo's, never an admin's)."""
        user = await self.scope.find(user_id)
        if user is None:
            raise AccountError(AccountErrorCode.USER_NOT_FOUND)
        refuse_demo(getattr(user, "is_demo", False), "admin actions on a demo account")
        return user

    async def row(self, user: UserT) -> BaseModel:
        """The account as one roster row: the ``to_row`` hook, else ``row_model`` from the
        account with ``email_verified`` from ``email_verified_at``."""
        if self._hooks.to_row is not None:
            return await self._hooks.to_row(user)
        row = self._row_model.model_validate(user, from_attributes=True)
        return row.model_copy(update={"email_verified": user.email_verified_at is not None})

    def levels(self) -> dict[str, ConfirmationLevel]:
        """The level of each action the router takes (§3.1, §4.2), the contract's floor."""
        actions = (*ADMIN_ACTIONS, *self._erase_actions(), *self._policy.app_actions)
        return {str(action): confirmation_level(action) for action in actions}

    def _erase_actions(self) -> tuple[AdminAction, ...]:
        return (AdminAction.ERASE,) if self._accounts_policy.deletion_mode is DeletionMode.OPERATOR else ()

    async def _confirm(self, action: AdminAction, target: UserT, body: ActionConfirmation) -> None:
        floor = None
        if self._hooks.confirmation is not None:
            floor = await self._hooks.confirmation(target, action)
        level = confirmation_level(action, at_least=floor or ConfirmationLevel.NONE)
        require_confirmation(
            level, target_email=target.email, acknowledged=body.acknowledged, confirm_email=body.confirm_email
        )

    async def _record(self, action: AdminAction, admin: UserT, target: UserT, detail: _Extras | None = None) -> None:
        record = admin_action_record(
            action,
            actor_id=admin.id,
            target_user_id=target.id,
            target_email=target.email,
            detail=detail,
            now=self._now(),
        )
        await self._log.add({**record, **self.scope.record_fields()})

    # --- the roster ------------------------------------------------------------------------

    async def roster(self, **raw: Any) -> dict[str, Any]:
        """``GET /admin/users`` (§3.1): the query parsed (``422`` for a bad one), one page of
        the scope's people as rows, the total, the transport, the active admins and the
        levels."""
        query = parse_roster_query(
            **raw,
            sort_keys=self._policy.sort_keys,
            accepted_states=self._policy.states,
            roles=self._policy.roles,
        )
        rows, total = await self.scope.roster(query)
        return {
            "users": [await self.row(user) for user in rows],
            "total": total,
            "mail_backend": self._backend,
            "summary": {SUMMARY_ACTIVE_ADMINS: await self.scope.active_admins()},
            "levels": self.levels(),
        }

    # --- deactivate and reactivate -----------------------------------------------------------

    async def set_active(self, admin: UserT, target: UserT, *, active: bool, body: ActionConfirmation) -> UserT:
        """``POST /admin/users/{id}/active`` (§4.1). Deactivating: never yourself, the scope's
        account-wide refusal, never the last active admin, the typed address; then every
        session ends and nothing else is cleared. Reactivating: the scope's refusal, the
        ``activating`` hook, a pending deletion withdrawn (§6.4), the account on."""
        if active == target.is_active:
            return target
        if active:
            return await self._reactivate(admin, target, body)
        refuse_self(admin.id, target.id)
        await self.scope.refuse_account_wide(target, AdminAction.DEACTIVATE)
        await self.scope.refuse_last_admin(target)
        await self._confirm(AdminAction.DEACTIVATE, target, body)
        target = await self._users.apply(target, {"is_active": False})
        await self.accounts.end_sessions(target, SessionsEndedReason.DEACTIVATED)
        await self._record(AdminAction.DEACTIVATE, admin, target, {"sessions_ended": True})
        return target

    async def _reactivate(self, admin: UserT, target: UserT, body: ActionConfirmation) -> UserT:
        await self.scope.refuse_account_wide(target, AdminAction.REACTIVATE)
        await self._confirm(AdminAction.REACTIVATE, target, body)
        if self._hooks.activating is not None:
            await self._hooks.activating(admin, target)
        if target.deletion_requested_at is not None:
            target = await self.accounts.withdraw_deletion(target, actor_id=admin.id)
        else:
            target = await self._users.apply(target, {"is_active": True})
        await self._record(AdminAction.REACTIVATE, admin, target)
        return target

    # --- the role --------------------------------------------------------------------------

    async def change_roles(self, admin: UserT, target: UserT, roles: frozenset[str], body: ActionConfirmation) -> UserT:
        """``POST /admin/users/{id}/role`` or kastlan's ``PUT …/roles`` (§4.1): every role the
        change adds grantable (``422 role_not_grantable``); taking an admin role away never
        your own and never the last active admin's; acknowledged; the ``role_changing`` hook;
        then the scope writes it. Logged ``{from, to}``, or ``{added, removed}`` for sets.
        Sessions stay: the guard reads the row on every request."""
        before = await self.scope.roles_of(target)
        if roles == before:
            return target
        if roles - before - await self.scope.grantable_roles():
            raise AccountError(AccountErrorCode.ROLE_NOT_GRANTABLE)
        admin_role = self._accounts_policy.admin_role
        if admin_role in before and admin_role not in roles:
            refuse_self(admin.id, target.id)
            await self.scope.refuse_last_admin(target)
        await self._confirm(AdminAction.ROLE, target, body)
        await self._roles(admin, target, before, roles)
        if self._role_sets:
            detail: dict[str, Any] = {"added": sorted(roles - before), "removed": sorted(before - roles)}
        else:
            detail = {"from": _only(before), "to": _only(roles)}
        await self._record(AdminAction.ROLE, admin, target, detail)
        return target

    async def _roles(self, admin: UserT, target: UserT, before: frozenset[str], after: frozenset[str]) -> None:
        if self._hooks.role_changing is not None:
            await self._hooks.role_changing(admin, target, before, after)
        await self.scope.set_roles(target, after)

    # --- the forced password change ------------------------------------------------------------

    async def require_password_change(self, admin: UserT, target: UserT, body: ActionConfirmation) -> UserT:
        """``POST /admin/users/{id}/password-change`` (§4.1): the scope's account-wide refusal,
        acknowledged; the next sign-in asks for a new password, and every session ends now
        (§12.5 item 7)."""
        if target.password_change_required_at is not None:
            return target
        await self.scope.refuse_account_wide(target, AdminAction.PASSWORD_CHANGE_REQUIRE)
        await self._confirm(AdminAction.PASSWORD_CHANGE_REQUIRE, target, body)
        target = await self._users.apply(target, {"password_change_required_at": self._now()})
        await self.accounts.end_sessions(target, SessionsEndedReason.PASSWORD_CHANGE_REQUIRED)
        await self._record(AdminAction.PASSWORD_CHANGE_REQUIRE, admin, target, {"sessions_ended": True})
        return target

    async def withdraw_password_change(self, admin: UserT, target: UserT, body: ActionConfirmation) -> UserT:
        """``DELETE /admin/users/{id}/password-change``: the requirement goes; the sessions it
        ended stay ended."""
        if target.password_change_required_at is None:
            return target
        await self.scope.refuse_account_wide(target, AdminAction.PASSWORD_CHANGE_WITHDRAW)
        await self._confirm(AdminAction.PASSWORD_CHANGE_WITHDRAW, target, body)
        target = await self._users.apply(target, {"password_change_required_at": None})
        await self._record(AdminAction.PASSWORD_CHANGE_WITHDRAW, admin, target)
        return target

    # --- the mails -------------------------------------------------------------------------

    async def send_mail(self, admin: UserT, target: UserT, kind: MailKind, body: ActionConfirmation) -> dict[str, Any]:
        """``POST /admin/users/{id}/mail {kind}`` (§4.1) → ``{sent, mail_backend, link?}``:
        ``account_inactive`` for a deactivated account, ``email_already_verified`` for a
        verification that would change nothing; the level the app may raise; the mail
        (:meth:`~eifi1_server_kit.auth.service.AccountService.send_account_mail`), then its
        row ``{sent}`` — after the link's commit, as all three apps log it."""
        if not target.is_active:
            raise AccountError(AccountErrorCode.ACCOUNT_INACTIVE)
        if kind is MailKind.VERIFICATION and target.email_verified_at is not None:
            raise AccountError(AccountErrorCode.EMAIL_ALREADY_VERIFIED)
        await self._confirm(kind.action, target, body)
        sent, link = await self.accounts.send_account_mail(target, kind)
        await self._record(kind.action, admin, target, {"sent": sent})
        return {"sent": sent, "mail_backend": self._backend, "link": link if self._console else None}

    @property
    def _console(self) -> bool:
        return self._backend == MAIL_BACKEND_CONSOLE

    async def test_mail(self, admin: UserT) -> MailTestResult:
        """``POST /admin/mail/test`` (admin §9.2): one mail to the admin's own address, in
        their language, awaited inline through the app's transport, so the answer says whether
        it went; 3 in 10 minutes per admin (``mail_test``); never a demo's; no log row
        (nobody is acted on)."""
        refuse_demo(getattr(admin, "is_demo", False), "the test mail")
        charge(self._limiters.mail_test, str(admin.id), _TEST_MAIL_THROTTLED)
        message = test_mail_message(
            admin.locale, app_name=self._accounts_policy.app_name, backend=self._backend, sent_at=self._now()
        )
        sent = await self._mailer.send(admin.email, message, kind=MAIL_TEST_KIND)
        logger.info("test mail by admin %s: sent=%s through %s", admin.id, sent, self._backend)
        return MailTestResult(sent=sent, mail_backend=self._backend)

    # --- the reviewer scope ----------------------------------------------------------------

    async def set_reviewer(self, admin: UserT, target: UserT, body: ReviewerUpdate) -> ReviewerResult:
        """``PUT /admin/users/{id}/reviewer {locales, areas}`` (§4.1): never an admin's
        (``reviewer_is_admin``: they review every locale, and taking the admin role away is the
        role change's); the locales and areas checked against the app's (``422``). Locales give
        the reviewer role (with ``AdminPolicy.reviewer_role``), none take it away again; the
        areas count only with locales. No confirmation (``none``)."""
        roles = await self.scope.roles_of(target)
        if self._accounts_policy.admin_role in roles:
            raise AccountError(AccountErrorCode.REVIEWER_IS_ADMIN)
        locales = normalize_locales(body.locales, self._policy.review_locales)
        areas = normalize_areas(body.areas, self._policy.review_areas) if locales else None
        wanted = self._reviewer_roles(roles, given=bool(locales))
        scope = {"translation_review_locales": locales or None, "translation_review_areas": areas}
        unchanged = (
            wanted == roles
            and list(target.translation_review_locales or []) == locales
            and (None if target.translation_review_areas is None else list(target.translation_review_areas)) == areas
        )
        if not unchanged:
            if wanted != roles:
                await self._roles(admin, target, roles, wanted)
            target = await self._users.apply(target, scope)
            await self._record(AdminAction.REVIEWER, admin, target, {"locales": locales, "areas": areas})
        return ReviewerResult(user_id=target.id, role=target.role, locales=locales, areas=areas)

    def _reviewer_roles(self, roles: frozenset[str], *, given: bool) -> frozenset[str]:
        """The roles after a review scope is ``given`` or taken away: with one role, the
        reviewer role in place of the account's, and the default role in place of the
        reviewer's; with a set, the reviewer role added or removed (the default role when none
        would be left); without a reviewer role, the roles as they are."""
        reviewer = self._policy.reviewer_role
        default = frozenset({self._accounts_policy.default_role})
        if reviewer is None:
            return roles
        if self._role_sets:
            return (roles | {reviewer}) if given else (roles - {reviewer}) or default
        if given:
            return frozenset({reviewer})
        return default if roles == {reviewer} else roles

    # --- the log ---------------------------------------------------------------------------

    async def actions(self, *, limit: int, offset: int, target_user_id: int | None) -> dict[str, Any]:
        """``GET /admin/actions?limit=&offset=&target=`` (§4.3): newest first, with the actor
        and the target joined as people (an erased one is ``None``) and the groups named by
        the ``group_names`` hook."""
        rows, total = await self._log.page(target_user_id=target_user_id, limit=limit, offset=offset)
        people: dict[int, PersonRef | None] = {}
        for person_id in {row.actor_id for row in rows} | {row.target_user_id for row in rows}:
            if person_id is not None:
                person = await self._users.get(person_id)
                people[person_id] = None if person is None else PersonRef.model_validate(person)
        group_ids = sorted({group for row in rows if (group := getattr(row, "target_group_id", None)) is not None})
        names: Mapping[int, str | None] = {}
        if group_ids and self._hooks.group_names is not None:
            names = await self._hooks.group_names(group_ids)
        return {"actions": [self._action_row(row, people, names) for row in rows], "total": total}

    def _action_row(self, row: Any, people: Mapping[int, PersonRef | None], names: Mapping[int, str | None]) -> Any:
        group = getattr(row, "target_group_id", None)
        return self._action_row_model.model_validate(
            {
                "id": row.id,
                "at": row.at,
                "action": row.action,
                "actor_id": row.actor_id,
                "target_user_id": row.target_user_id,
                "target_email": row.target_email,
                "detail": row.detail,
                "company_id": getattr(row, "company_id", None),
                "target_group_id": group,
                "actor": people.get(row.actor_id) if row.actor_id is not None else None,
                "target": people.get(row.target_user_id) if row.target_user_id is not None else None,
                "target_group": None if group is None else GroupRef(id=group, name=names.get(group)),
            }
        )

    # --- the overview ----------------------------------------------------------------------

    async def attention(self, admin: UserT, model: type[AdminAttention] = AdminAttention) -> AdminAttention:
        """``GET /admin/attention`` (admin §2.4): the people, the new ones of the last seven
        days and the open invitations, counted by the kit over the scope; the deletion
        requests only where an operator erases (where a job does, nobody waits on the admin);
        the app's ``attention`` hook over them (``feedback_waiting``, its ``extra``). A demo
        admin sees no count, the app's tiles kept as ``None``."""
        app = dict(await self._hooks.attention(admin)) if self._hooks.attention is not None else {}
        if getattr(admin, "is_demo", False):
            return model.for_demo(*cast(Mapping[str, Any], app.get("extra") or {}))
        counts = await self.scope.counts(since=self._now() - _NEW_WINDOW)
        operator = self._accounts_policy.deletion_mode is DeletionMode.OPERATOR
        values: dict[str, Any] = {
            "users_total": counts.people,
            "users_new_7d": counts.new,
            "deletion_requests": counts.deletion_requests if operator else None,
            "invitations_pending": await self._open_invitations(),
        }
        return model.model_validate({**values, **app})

    async def _open_invitations(self) -> int:
        now = self._now()
        return sum(
            invitation.expires_at is not None
            and invitation_status(
                expires_at=invitation.expires_at,
                now=now,
                accepted_at=invitation.accepted_at,
                revoked_at=invitation.revoked_at,
            )
            is InvitationStatus.OPEN
            for invitation in await self._invitations.listing()
        )

    # --- the operator's erasure ------------------------------------------------------------

    async def erase(self, admin: UserT, target: UserT, body: ActionConfirmation) -> dict[str, Any]:
        """``POST /admin/users/{id}/erase`` (§6.4, ``operator`` mode; Kurvenschmiede's, kastlan's
        platform): never yourself, the scope's account-wide refusal, only an account that is
        deactivated and asked for its deletion (``deletion_not_requested``), its address typed;
        then :meth:`~eifi1_server_kit.auth.service.AccountService.erase` with the admin as the
        actor. Answers the ``erase`` row's detail."""
        refuse_self(admin.id, target.id)
        await self.scope.refuse_account_wide(target, AdminAction.ERASE)
        if target.is_active or target.deletion_requested_at is None:
            raise AccountError(AccountErrorCode.DELETION_NOT_REQUESTED)
        await self._confirm(AdminAction.ERASE, target, body)
        return await self.accounts.erase(target, actor_id=admin.id, feedback=self._feedback)
