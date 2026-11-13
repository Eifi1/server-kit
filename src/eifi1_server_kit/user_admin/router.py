"""The admin's routes as a router factory: ``user_admin_router(UserAdminDeps(...))``.

Layer 2 (``docs/auth-harmonization.md`` §12.3–§12.4, ``docs/user-admin-harmonization.md``
§3–§6, ``docs/admin-harmonization.md`` §2.4, §9.2 in ``Eifi1/ui-kit``), behind the ``fastapi``
extra, beside :func:`~eifi1_server_kit.auth.router.auth_router` and on its ``AuthDeps``: the
same stores, the same :func:`~eifi1_server_kit.auth.router.current_user` and, unless the app
passes its own, the same :func:`~eifi1_server_kit.auth.router.admin_guard` — the guard is on
the router, so a route added here cannot forget it. An app with the global scope mounts it as::

    admin = UserAdminDeps(
        auth=deps,                       # the app's AuthDeps
        roster=roster_store,             # a dependency: SqlRosterStore(session, User)
        hooks=admin_hooks,               # a dependency answering UserAdminHooks
        feedback=feedback_service,       # optional: the erasure anonymises the person's feedback
        policy=AdminPolicy(review_locales=UI_LOCALE_CODES, review_areas=REVIEW_AREAS),
    )
    app.include_router(user_admin_router(admin), prefix="/api/v1")
    app.include_router(invitations_admin_router(deps), prefix="/api/v1")  # 0.29's, the same guard

kastlan (later) mounts it twice: its company admin with ``scope=`` its company scope,
``guard=`` its company-admin check and ``role_sets=True``, and its platform with the global
scope under ``prefix="/platform"``.

**Every refusal is coded** (§12.5 item 3), answered by
:func:`~eifi1_server_kit.errors.install_contract_error_handlers`; the limits are the app's
``AuthLimiters`` on ``app.state``.

No ``from __future__ import annotations`` here, on purpose: the endpoints' parameter types
are the app's models, closed over in the factory.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import BaseModel

from eifi1_server_kit.auth.router import AuthDeps, account_service, admin_guard, current_user
from eifi1_server_kit.auth.service import AccountService
from eifi1_server_kit.mail import MailTestResult
from eifi1_server_kit.user_admin.attention import AdminAttention
from eifi1_server_kit.user_admin.deletion import DeletionMode
from eifi1_server_kit.user_admin.roster import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from eifi1_server_kit.user_admin.schemas import (
    ActionConfirmation,
    ActiveChange,
    AdminActionPage,
    AdminActionRow,
    AdminUserRow,
    ErasureResult,
    MailBackendInfo,
    MailRequest,
    MailResult,
    ReviewerResult,
    ReviewerUpdate,
    RoleChange,
    RolesChange,
    UserListResponse,
)
from eifi1_server_kit.user_admin.scope import GlobalScope
from eifi1_server_kit.user_admin.service import AdminPolicy, UserAdminHooks, UserAdminService

try:
    from fastapi import APIRouter, Depends, Query, Request
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.user_admin.router needs FastAPI: install the kit with its fastapi extra, "
        "eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["UserAdminDeps", "no_admin_hooks", "user_admin_router"]


async def no_admin_hooks() -> UserAdminHooks[Any]:
    """``UserAdminDeps.hooks``' default: no hook at all."""
    return UserAdminHooks()


async def _absent() -> None:
    """A dependency the app did not give."""
    return None


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class UserAdminDeps:
    """What an app hands :func:`user_admin_router` (§12.3–§12.4): its ``AuthDeps``, the
    roster or a scope, its hooks, and the models it extends."""

    #: The account layer's: the stores, the signer, the mailer and its ``mail_backend``, the
    #: policy (``admin_role``, ``default_role``, ``deletion_mode``), the limiters and the clock.
    auth: AuthDeps
    #: A dependency answering the :class:`~eifi1_server_kit.user_admin.ports.RosterStore` on
    #: the request's session: the global scope reads it (``SqlRosterStore`` on ``[sql]``).
    roster: Callable[..., Any] | None = None
    #: A dependency answering the request's
    #: :class:`~eifi1_server_kit.user_admin.scope.AdminScope`; ``None``: the global scope over
    #: ``auth.users`` and ``roster`` (§12.4).
    scope: Callable[..., Any] | None = None
    #: A dependency answering the request's
    #: :class:`~eifi1_server_kit.user_admin.service.UserAdminHooks`.
    hooks: Callable[..., Any] = no_admin_hooks
    #: A dependency answering the app's ``FeedbackService`` on the request's session: the
    #: operator's erasure anonymises the person's feedback through it. ``None``: no feedback.
    feedback: Callable[..., Any] | None = None
    #: The router's guard; ``None``: ``admin_guard(auth)``. Pass the same to
    #: ``invitations_admin_router(auth, guard=…)``.
    guard: Callable[..., Any] | None = None
    policy: AdminPolicy = field(default_factory=AdminPolicy)
    #: Roles as a set (kastlan's company admin): ``PUT /users/{id}/roles {roles}`` in place of
    #: ``POST /users/{id}/role {role}``, logged ``{added, removed}``.
    role_sets: bool = False
    # The app's answers and bodies (subclasses of the kit's).
    row_model: type[AdminUserRow] = AdminUserRow
    action_row_model: type[AdminActionRow] = AdminActionRow
    attention_model: type[AdminAttention] = AdminAttention
    erasure_result_model: type[ErasureResult] = ErasureResult
    active_change_model: type[ActiveChange] = ActiveChange
    role_change_model: type[RoleChange] = RoleChange
    roles_change_model: type[RolesChange] = RolesChange
    mail_request_model: type[MailRequest] = MailRequest


def _global_scope(deps: UserAdminDeps, roster: Callable[..., Any]) -> Callable[..., Any]:
    auth = deps.auth
    grantable = deps.policy.grantable_roles()

    async def scope(
        users: Annotated[Any, Depends(auth.users)], store: Annotated[Any, Depends(roster)]
    ) -> GlobalScope[Any]:
        return GlobalScope(
            users=users,
            roster=store,
            admin_role=auth.policy.admin_role,
            reviewer_role=deps.policy.reviewer_role,
            grantable=grantable,
        )

    return scope


def _service(deps: UserAdminDeps) -> Callable[..., Any]:
    auth = deps.auth
    if deps.scope is not None:
        scope_of = deps.scope
    elif deps.roster is not None:
        scope_of = _global_scope(deps, deps.roster)
    else:
        raise ValueError("user_admin_router: the global scope reads a roster (UserAdminDeps.roster), or pass a scope")

    async def service(
        request: Request,
        accounts: Annotated[AccountService[Any, Any, Any], Depends(account_service(auth))],
        users: Annotated[Any, Depends(auth.users)],
        log: Annotated[Any, Depends(auth.log)],
        invitations: Annotated[Any, Depends(auth.invitations)],
        scope: Annotated[Any, Depends(scope_of)],
        hooks: Annotated[Any, Depends(deps.hooks)],
        feedback: Annotated[Any, Depends(deps.feedback or _absent)],
    ) -> UserAdminService[Any]:
        return UserAdminService(
            accounts=accounts,
            scope=scope,
            users=users,
            log=log,
            invitations=invitations,
            mailer=auth.mailer,
            mail_backend=auth.mail_backend,
            account_policy=auth.policy,
            policy=deps.policy,
            limiters=auth.limiters(request),
            hooks=hooks,
            feedback=feedback,
            role_sets=deps.role_sets,
            row_model=deps.row_model,
            action_row_model=deps.action_row_model,
            now=auth.now,
        )

    return service


def user_admin_router(deps: UserAdminDeps, *, prefix: str = "/admin", tags: Sequence[str] = ("admin",)) -> APIRouter:
    """The admin's routes (§12.1), every one behind the guard, on
    ``APIRouter(prefix=prefix, tags=tags)``:

    * ``GET /users`` — the roster (``UserListResponse[row_model]``);
    * ``POST /users/{user_id}/active``, ``POST /users/{user_id}/role`` (or ``PUT …/roles``
      with ``role_sets``), ``POST`` and ``DELETE /users/{user_id}/password-change`` — each
      answers the account's row;
    * ``POST /users/{user_id}/mail`` → ``MailResult``;
    * ``PUT /users/{user_id}/reviewer`` → ``ReviewerResult``, with ``policy.review_locales``;
    * ``POST /users/{user_id}/erase`` → ``erasure_result_model``, where an operator erases
      (``deletion_mode`` ``operator``; where a job does, the route is not there);
    * ``GET /actions`` → ``AdminActionPage[action_row_model]``, ``GET /attention`` →
      ``attention_model``, ``GET /mail`` → ``MailBackendInfo``, ``POST /mail/test`` →
      ``MailTestResult``.

    Raises :class:`ValueError` for a policy naming a role the app does not have, and for the
    global scope without a roster."""
    auth = deps.auth
    deps.policy.check(auth.policy)
    Service = Annotated[UserAdminService[Any], Depends(_service(deps))]  # noqa: N806
    Admin = Annotated[Any, Depends(current_user(auth))]  # noqa: N806
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(deps.guard or admin_guard(auth))])
    row_model = deps.row_model
    list_model = UserListResponse[row_model]  # type: ignore[valid-type]

    async def target(user_id: int, service: Service) -> Any:
        return await service.target(user_id)

    Target = Annotated[Any, Depends(target)]  # noqa: N806

    @router.get("/users", response_model=list_model)
    async def list_users(
        service: Service,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        sort: str | None = None,
        q: str | None = None,
        role: Annotated[list[str] | None, Query()] = None,
        state: Annotated[list[str] | None, Query()] = None,
    ) -> BaseModel:
        """One page of the people the admin administers (§3.1); ``422`` for a bad query."""
        found = await service.roster(limit=limit, offset=offset, sort=sort, q=q, role=role, state=state)
        return list_model.model_validate(found)

    active_model = deps.active_change_model

    @router.post("/users/{user_id}/active", response_model=row_model)
    async def change_active(payload: active_model, admin: Admin, user: Target, service: Service) -> BaseModel:  # type: ignore[valid-type]
        """Deactivate (the address typed, every session ended) or reactivate (§4.1)."""
        body: ActiveChange = payload
        return await service.row(await service.set_active(admin, user, active=body.active, body=body))

    if deps.role_sets:
        roles_model = deps.roles_change_model

        @router.put("/users/{user_id}/roles", response_model=row_model)
        async def change_roles(payload: roles_model, admin: Admin, user: Target, service: Service) -> BaseModel:  # type: ignore[valid-type]
            """The account's roles in the scope (kastlan's company), acknowledged (§4.1)."""
            body: RolesChange = payload
            return await service.row(await service.change_roles(admin, user, frozenset(body.roles), body))
    else:
        role_model = deps.role_change_model

        @router.post("/users/{user_id}/role", response_model=row_model)
        async def change_role(payload: role_model, admin: Admin, user: Target, service: Service) -> BaseModel:  # type: ignore[valid-type]
            """The account's role, acknowledged (§4.1)."""
            body: RoleChange = payload
            return await service.row(await service.change_roles(admin, user, frozenset({body.role}), body))

    @router.post("/users/{user_id}/password-change", response_model=row_model)
    async def require_password_change(
        admin: Admin, user: Target, service: Service, payload: ActionConfirmation | None = None
    ) -> BaseModel:
        """A new password at the next sign-in, acknowledged; every session ends (§4.1). A body
        a proxy dropped is ``confirmation_required``: the action fails closed (§4.2)."""
        changed = await service.require_password_change(admin, user, payload or ActionConfirmation())
        return await service.row(changed)

    @router.delete("/users/{user_id}/password-change", response_model=row_model)
    async def withdraw_password_change(
        admin: Admin, user: Target, service: Service, payload: ActionConfirmation | None = None
    ) -> BaseModel:
        """Take the requirement back."""
        changed = await service.withdraw_password_change(admin, user, payload or ActionConfirmation())
        return await service.row(changed)

    mail_model = deps.mail_request_model

    @router.post("/users/{user_id}/mail", response_model=MailResult)
    async def send_mail(payload: mail_model, admin: Admin, user: Target, service: Service) -> MailResult:  # type: ignore[valid-type]
        """The verification mail or a password reset again; the link only on the console."""
        body: MailRequest = payload
        return MailResult.model_validate(await service.send_mail(admin, user, body.kind, body))

    if deps.policy.review_locales:

        @router.put("/users/{user_id}/reviewer", response_model=ReviewerResult)
        async def set_reviewer(payload: ReviewerUpdate, admin: Admin, user: Target, service: Service) -> ReviewerResult:
            """The reviewer role and its scope in one write (§4.1)."""
            return await service.set_reviewer(admin, user, payload)

    if auth.policy.deletion_mode is DeletionMode.OPERATOR:
        erasure_model = deps.erasure_result_model

        @router.post("/users/{user_id}/erase", response_model=erasure_model)
        async def erase_account(
            admin: Admin, user: Target, service: Service, payload: ActionConfirmation | None = None
        ) -> BaseModel:
            """Erase an account that asked to be deleted, its address typed (§6.4)."""
            return erasure_model.model_validate(await service.erase(admin, user, payload or ActionConfirmation()))

    action_page = AdminActionPage[deps.action_row_model]  # type: ignore[name-defined]

    @router.get("/actions", response_model=action_page)
    async def list_actions(
        service: Service,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        offset: Annotated[int, Query(ge=0)] = 0,
        target: Annotated[int | None, Query(description="Only the actions on this account")] = None,
    ) -> BaseModel:
        """The admin log, newest first (§4.3)."""
        return action_page.model_validate(await service.actions(limit=limit, offset=offset, target_user_id=target))

    attention_model = deps.attention_model

    @router.get("/attention", response_model=attention_model)
    async def attention(admin: Admin, service: Service) -> AdminAttention:
        """What waits on the admin: the overview's tiles and the menu's badge (admin §2.4)."""
        return await service.attention(admin, attention_model)

    @router.get("/mail", response_model=MailBackendInfo)
    async def mail_backend() -> MailBackendInfo:
        """The transport the mail actions go through, before any of them is sent."""
        return MailBackendInfo(mail_backend=auth.mail_backend)

    @router.post("/mail/test", response_model=MailTestResult)
    async def send_test_mail(admin: Admin, service: Service) -> MailTestResult:
        """One mail to the admin's own address, awaited, to see the transport work (admin §9.2)."""
        return await service.test_mail(admin)

    return router
