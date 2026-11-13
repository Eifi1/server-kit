"""The account routes as router factories: ``auth_router(AuthDeps(...))`` and
``invitations_admin_router(...)``.

Layer 2 (``docs/auth-harmonization.md`` §12.3 in ``Eifi1/ui-kit``; the pattern of
:func:`eifi1_server_kit.feedback.router.feedback_router`), behind the ``fastapi`` extra, and
the passkey routes behind the ``passkeys`` extra too. The app passes its stores as
dependencies on the request's session, its signer, hasher and mailer, its policy, its hooks
dependency, and the schema subclasses it extends; and includes the routers as its own::

    deps = AuthDeps(
        users=user_store, sessions=session_store, tokens=token_store, invitations=invitation_store,
        log=admin_log, after_commit=after_commit, hooks=account_hooks,
        signer=jwt_signer, hasher=bcrypt_hasher, mailer=mailer, mail_backend=settings.mail_backend,
        policy=AccountPolicy(app="staunlabor", app_name="Staunlabor", contact="support@staunlabor.app",
                             links=AccountLinks(base_url=settings.app_base_url)),
        passkeys=passkey_store, relying_party=RelyingParty(rp_id=..., rp_name="Staunlabor", origins=(...,)),
        client_address=caller_address,
    )
    app.include_router(auth_router(deps), prefix="/api/v1")
    app.include_router(invitations_admin_router(deps), prefix="/api/v1")
    app.state.auth_limiters = AuthLimiters()
    install_contract_error_handlers(app)

**One** :func:`current_user` **for every router** (§12.3): the dependency the account routes
check the bearer with — the token, its session row, the cut-off, the account active, the
app's ``bind`` — so ``FeedbackDeps.actor`` and ``TranslationReviewDeps.reviewer`` map that
user instead of checking a bearer a second time. :func:`admin_guard` is built on it. Both are
the same callables for the same ``deps`` however often they are asked for, so FastAPI runs
each once per request.

**The router raises no** ``HTTPException``: every refusal is a coded kit refusal (§12.5 item
3), answered by :func:`~eifi1_server_kit.errors.install_contract_error_handlers`; a signed-in
route answers ``401`` only about the session, never for a wrong input. The limits are the
app's :class:`~eifi1_server_kit.auth.AuthLimiters` on ``app.state.auth_limiters``, each window
keyed by the app's ``client_address`` (its ``client_ip`` with ``vouched_hops``).

No ``from __future__ import annotations`` here, on purpose: the endpoints' parameter types
are the app's models, closed over in the factory, and FastAPI cannot resolve a string
annotation that names a local.
"""

import asyncio
import json
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

from pydantic import BaseModel

from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.limits import AuthLimiters
from eifi1_server_kit.auth.mails import AccountMailText, account_mail_texts
from eifi1_server_kit.auth.passkeys import RelyingParty
from eifi1_server_kit.auth.passwords import absent_account_check
from eifi1_server_kit.auth.ports import PasswordHasher, TokenSigner
from eifi1_server_kit.auth.schemas import (
    DeletionResult,
    DeletionTerms,
    EmailChangeConfirmed,
    EmailChangeState,
    EmailVerificationStatus,
    InvitationAccepted,
    InvitationCheck,
    LoginRequest,
    PasskeyLoginFinish,
    PasskeyOptions,
    PasskeyRegistrationBegin,
    PasskeyRegistrationFinish,
    PasskeyRename,
    PasskeyResponse,
    PasswordChangeChallenge,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetState,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    SessionInfo,
    SetPasswordRequest,
    TokenRequest,
    TokenResponse,
    UserResponse,
)
from eifi1_server_kit.auth.service import (
    AccountPolicy,
    AccountService,
    AuthHooks,
    ChallengeIssued,
    IssuedSession,
    SignedIn,
)
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.limiter import UNKNOWN_CLIENT
from eifi1_server_kit.mail import Mailer, MailText
from eifi1_server_kit.user_admin.export import export_filename
from eifi1_server_kit.user_admin.schemas import (
    MAIL_BACKEND_CONSOLE,
    DeletionRequest,
    EmailChangeConfirm,
    EmailChangeRequest,
    InvitationCreate,
    InvitationIssued,
    InvitationRow,
)

try:
    from fastapi import APIRouter, Depends, Request, Response, status
    from fastapi.encoders import jsonable_encoder
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.router needs FastAPI: install the kit with its fastapi extra, eifi1-server-kit[fastapi]."
    ) from exc

__all__ = [
    "AuthDeps",
    "account_service",
    "admin_guard",
    "app_state_limiters",
    "auth_router",
    "current_user",
    "invitations_admin_router",
    "no_hooks",
    "peer_address",
]

#: The longest ``User-Agent`` a session row keeps as its device.
_DEVICE_LENGTH = 255
_bearer = HTTPBearer(auto_error=False, description="The access token of a session (§6.2).")


def app_state_limiters(request: Request) -> AuthLimiters:
    """The app's :class:`~eifi1_server_kit.auth.AuthLimiters`, read per request from
    ``app.state.auth_limiters`` — built once in ``create_app``, so a test that builds an app
    per test starts with empty windows."""
    limiters: AuthLimiters = request.app.state.auth_limiters
    return limiters


def peer_address(request: Request) -> str:
    """The TCP peer: ``AuthDeps.client_address``'s default, right only for a process no proxy
    fronts. Behind one, pass the app's own ``client_ip(…, trusted_hops=vouched_hops(…))``."""
    return request.client.host if request.client else UNKNOWN_CLIENT


async def no_hooks() -> AuthHooks[Any, Any]:
    """``AuthDeps.hooks``' default: no hook at all."""
    return AuthHooks()


async def _absent() -> None:
    """A store the app did not give."""
    return None


@dataclass(frozen=True, slots=True, kw_only=True, eq=False, weakref_slot=True)
class AuthDeps:
    """What an app hands :func:`auth_router` (§12.3): its stores as dependencies on the
    request's session, its objects, its policy, the schema subclasses it extends, and its
    choices. Compared by identity: :func:`current_user` is one callable per ``AuthDeps``."""

    # The app's dependencies, each answering its store on the request's session (ports).
    #: ``UserStore``.
    users: Callable[..., Any]
    #: ``SessionStore``.
    sessions: Callable[..., Any]
    #: ``OneTimeTokenStore``.
    tokens: Callable[..., Any]
    #: ``InvitationStore``.
    invitations: Callable[..., Any]
    #: ``AdminActionLog``.
    log: Callable[..., Any]
    #: ``AfterCommit``: the request's queue for the work after its commit.
    after_commit: Callable[..., Any]
    #: The app's :class:`~eifi1_server_kit.auth.service.AuthHooks` for this request (§12.3:
    #: a dependency, so a hook works in the request's transaction).
    hooks: Callable[..., Any] = no_hooks
    #: ``PasskeyStore``: mounts the passkey routes, with ``relying_party``.
    passkeys: Callable[..., Any] | None = None
    # The app's objects.
    signer: TokenSigner
    hasher: PasswordHasher
    mailer: Mailer
    #: The transport's name (``console``, ``resend``, ``smtp``): an admin's answer carries a
    #: link only while it is ``console`` (§12.5 item 11).
    mail_backend: str
    policy: AccountPolicy
    relying_party: RelyingParty | None = None
    #: The app's factory for the never-list routes: ``demo_guard("passkeys")``. ``None``: the
    #: service's own ``refuse_demo`` is the only check.
    demo_guard: Callable[[str], Callable[..., Any]] | None = None
    # The app's request bodies (subclasses of the kit's).
    register_model: type[RegisterRequest] = RegisterRequest
    profile_model: type[ProfileUpdate] = ProfileUpdate
    password_change_model: type[PasswordChangeRequest] = PasswordChangeRequest
    set_password_model: type[SetPasswordRequest] = SetPasswordRequest
    email_change_model: type[EmailChangeRequest] = EmailChangeRequest
    deletion_model: type[DeletionRequest] = DeletionRequest
    invitation_create_model: type[InvitationCreate] = InvitationCreate
    passkey_login_model: type[PasskeyLoginFinish] = PasskeyLoginFinish
    # The app's answers (subclasses of the kit's), where a hook's extras ride along.
    me_model: type[UserResponse] = UserResponse
    #: ``None``: ``TokenResponse[me_model]``; an app whose ``password_changing`` answers
    #: extras declares them in its subclass.
    token_model: type[TokenResponse[Any]] | None = None
    challenge_model: type[PasswordChangeChallenge] = PasswordChangeChallenge
    reset_state_model: type[PasswordResetState] = PasswordResetState
    invitation_check_model: type[InvitationCheck] = InvitationCheck
    invitation_accepted_model: type[InvitationAccepted] = InvitationAccepted
    invitation_row_model: type[InvitationRow] = InvitationRow
    invitation_issued_model: type[InvitationIssued] = InvitationIssued
    deletion_terms_model: type[DeletionTerms] = DeletionTerms
    deletion_result_model: type[DeletionResult] = DeletionResult
    # The app's choices.
    #: The app's words for a kind of mail, in place of the kit's (:mod:`~eifi1_server_kit.auth.mails`).
    mail_texts: Mapping[AccountMailText | str, Mapping[str, MailText]] = field(default_factory=dict)
    #: The caller's address for the per-IP windows.
    client_address: Callable[[Request], str] = peer_address
    limiters: Callable[[Request], AuthLimiters] = app_state_limiters
    now: Clock = utc_now
    #: The per-address delay's wait (a test passes one that records).
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True, slots=True)
class _Built:
    """What one ``AuthDeps`` builds once: its dependencies and its checked texts."""

    service: Callable[..., Awaitable[AccountService[Any, Any, Any]]]
    signed_in: Callable[..., Awaitable[SignedIn[Any]]]
    current_user: Callable[..., Awaitable[Any]]
    admin_guard: Callable[..., Awaitable[Any]]
    token_model: type[TokenResponse[Any]]


_BUILT: weakref.WeakKeyDictionary[AuthDeps, _Built] = weakref.WeakKeyDictionary()


def _build(deps: AuthDeps) -> _Built:
    if (deps.passkeys is None) != (deps.relying_party is None):
        raise ValueError("auth_router: passkeys and relying_party come together, or neither")
    texts = account_mail_texts(deps.mail_texts)
    check_absent = absent_account_check(deps.hasher.hash, deps.hasher.verify)

    async def service(
        request: Request,
        users: Annotated[Any, Depends(deps.users)],
        sessions: Annotated[Any, Depends(deps.sessions)],
        tokens: Annotated[Any, Depends(deps.tokens)],
        invitations: Annotated[Any, Depends(deps.invitations)],
        log: Annotated[Any, Depends(deps.log)],
        after_commit: Annotated[Any, Depends(deps.after_commit)],
        hooks: Annotated[Any, Depends(deps.hooks)],
        passkeys: Annotated[Any, Depends(deps.passkeys or _absent)],
    ) -> AccountService[Any, Any, Any]:
        return AccountService(
            users=users,
            sessions=sessions,
            tokens=tokens,
            invitations=invitations,
            log=log,
            after_commit=after_commit,
            signer=deps.signer,
            hasher=deps.hasher,
            mailer=deps.mailer,
            policy=deps.policy,
            limiters=deps.limiters(request),
            hooks=hooks,
            passkeys=passkeys,
            relying_party=deps.relying_party,
            mail_texts=texts,
            absent_check=check_absent,
            now=deps.now,
            sleep=deps.sleep,
        )

    async def signed_in(
        accounts: Annotated[AccountService[Any, Any, Any], Depends(service)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> SignedIn[Any]:
        return await accounts.authenticate(credentials.credentials if credentials is not None else None)

    async def current(signed: Annotated[SignedIn[Any], Depends(signed_in)]) -> Any:
        return signed.user

    async def guard(user: Annotated[Any, Depends(current)]) -> Any:
        if user.role != deps.policy.admin_role:
            raise AuthError(AuthErrorCode.ADMIN_REQUIRED)
        return user

    token_model = deps.token_model or TokenResponse[deps.me_model]  # type: ignore[name-defined]
    return _Built(service, signed_in, current, guard, token_model)


def _built(deps: AuthDeps) -> _Built:
    found = _BUILT.get(deps)
    if found is None:
        found = _BUILT[deps] = _build(deps)
    return found


def account_service(deps: AuthDeps) -> Callable[..., Awaitable[AccountService[Any, Any, Any]]]:
    """The dependency answering this request's :class:`~eifi1_server_kit.auth.service.AccountService`,
    for an app's own routes beside the kit's (keksdose's private mode)."""
    return _built(deps).service


def current_user(deps: AuthDeps) -> Callable[..., Awaitable[Any]]:
    """The dependency answering the signed-in account (the app's row): ``401 not_signed_in``
    without a usable bearer token, ``401 session_ended`` once its session is over, or the
    ``bind`` hook's refusal. The same callable for the same ``deps``."""
    return _built(deps).current_user


def admin_guard(deps: AuthDeps) -> Callable[..., Awaitable[Any]]:
    """:func:`current_user`, and ``403 admin_required`` unless the account holds
    ``policy.admin_role``. The same callable for the same ``deps``."""
    return _built(deps).admin_guard


def _guards(deps: AuthDeps, what: str) -> list[Any]:
    return [Depends(deps.demo_guard(what))] if deps.demo_guard is not None else []


def _device(request: Request) -> str | None:
    agent = request.headers.get("user-agent")
    return agent[:_DEVICE_LENGTH] if agent else None


def auth_router(deps: AuthDeps, *, prefix: str = "/auth", tags: Sequence[str] = ("auth",)) -> APIRouter:
    """The account routes (§12.1), on ``APIRouter(prefix=prefix, tags=tags)``: sign-up and
    sign-in with its forced change, the refresh, the sessions, ``/me`` and its settings
    (password, address, deletion, export), the mailed links (verification, reset,
    invitations), and with ``deps.passkeys`` the passkey routes. ``/sessions/current`` comes
    before ``/sessions/{session_id}``. Raises :class:`ValueError` for a ``passkeys`` without a
    ``relying_party`` (or the reverse) and for an app mail table the kit cannot fill."""
    built = _built(deps)
    token_model = built.token_model
    challenge_model = deps.challenge_model
    me_model = deps.me_model
    Accounts = Annotated[AccountService[Any, Any, Any], Depends(built.service)]  # noqa: N806
    Signed = Annotated[SignedIn[Any], Depends(built.signed_in)]  # noqa: N806
    User = Annotated[Any, Depends(built.current_user)]  # noqa: N806
    router = APIRouter(prefix=prefix, tags=list(tags))

    async def session_answer(accounts: AccountService[Any, Any, Any], issued: IssuedSession[Any]) -> BaseModel:
        me = await accounts.me(issued.user, me_model)
        return token_model.model_validate(
            {"access_token": issued.access_token, "refresh_token": issued.refresh_token, "user": me, **issued.extras}
        )

    async def sign_in_answer(
        accounts: AccountService[Any, Any, Any], outcome: IssuedSession[Any] | ChallengeIssued[Any]
    ) -> BaseModel:
        if isinstance(outcome, ChallengeIssued):
            return challenge_model.model_validate({"challenge_token": outcome.challenge_token, **outcome.extras})
        return await session_answer(accounts, outcome)

    register_model = deps.register_model

    @router.post("/register", response_model=token_model, status_code=status.HTTP_201_CREATED)
    async def register(payload: register_model, request: Request, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """Sign up (§4.2–§4.4), signed in on the answer."""
        issued = await accounts.register(payload, ip=deps.client_address(request), device=_device(request))
        return await session_answer(accounts, issued)

    @router.post("/login", response_model=token_model | challenge_model)
    async def login(payload: LoginRequest, request: Request, accounts: Accounts) -> BaseModel:
        """Sign in (§5.1): a session, or the forced change's challenge."""
        outcome = await accounts.login(payload, ip=deps.client_address(request), device=_device(request))
        return await sign_in_answer(accounts, outcome)

    set_password_model = deps.set_password_model

    @router.post("/login/set-password", response_model=token_model)
    async def set_password(payload: set_password_model, request: Request, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """The forced change, from the sign-in's challenge; then a session."""
        issued = await accounts.set_password(payload, ip=deps.client_address(request), device=_device(request))
        return await session_answer(accounts, issued)

    @router.post("/refresh", response_model=token_model)
    async def refresh(payload: RefreshRequest, request: Request, accounts: Accounts) -> BaseModel:
        """Renew the session (§11): every refusal ``401 session_ended``."""
        return await session_answer(accounts, await accounts.refresh(payload, ip=deps.client_address(request)))

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(user: User, accounts: Accounts) -> Response:
        """Sign out everywhere: every session ends, this one included (§6.3)."""
        await accounts.logout_everywhere(user)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions(signed: Signed, accounts: Accounts) -> list[SessionInfo]:
        """The account's live sessions, the caller's marked ``current``."""
        return await accounts.sessions_of(signed)

    @router.delete("/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
    async def end_current_session(
        accounts: Accounts, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
    ) -> Response:
        """End this device's session (§6.3) — also ``204`` when it had already ended."""
        await accounts.end_session(credentials.credentials if credentials is not None else None)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def end_session(session_id: str, signed: Signed, accounts: Accounts) -> Response:
        """End one of the account's own sessions (kastlan's device list)."""
        await accounts.end_device(signed, session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/me", response_model=me_model)
    async def me(user: User, accounts: Accounts) -> BaseModel:
        """The account (§6.1): the shell reads it here, never from the token."""
        return await accounts.me(user, me_model)

    profile_model = deps.profile_model

    @router.patch("/me", response_model=me_model)
    async def update_me(payload: profile_model, user: User, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """The name, the language, the text size, the contrast, the device's zone."""
        return await accounts.me(await accounts.update_profile(user, payload), me_model)

    password_change_model = deps.password_change_model

    @router.post("/me/password", response_model=token_model, dependencies=_guards(deps, "changing the password"))
    async def change_password(payload: password_change_model, signed: Signed, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """A new password: every other session ends, the caller's goes on with a fresh pair."""
        return await session_answer(accounts, await accounts.change_password(signed, payload))

    @router.get("/me/email", response_model=EmailChangeState)
    async def email_state(user: User, accounts: Accounts) -> EmailChangeState:
        """The address, and the change it waits on."""
        return await accounts.email_state(user)

    email_change_model = deps.email_change_model
    address_guards = _guards(deps, "changing the sign-in address")

    @router.post(
        "/me/email",
        response_model=EmailChangeState,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=address_guards,
    )
    async def request_email_change(payload: email_change_model, user: User, accounts: Accounts) -> EmailChangeState:  # type: ignore[valid-type]
        """A new address: a link to it, a notice to the old one; nothing changes yet."""
        body = cast(EmailChangeRequest, payload)
        return await accounts.request_email_change(user, body.new_email, body.password)

    @router.post("/me/email/resend", response_model=EmailChangeState, dependencies=address_guards)
    async def resend_email_change(user: User, accounts: Accounts) -> EmailChangeState:
        """A new link for the pending change."""
        return await accounts.resend_email_change(user)

    @router.delete("/me/email", status_code=status.HTTP_204_NO_CONTENT, dependencies=address_guards)
    async def cancel_email_change(user: User, accounts: Accounts) -> Response:
        """Drop the pending change."""
        await accounts.cancel_email_change(user)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/me/email/confirm", response_model=EmailChangeConfirmed)
    async def confirm_email_change(
        payload: EmailChangeConfirm, request: Request, accounts: Accounts
    ) -> EmailChangeConfirmed:
        """Open the change's link, anonymously: the address switches, the sessions stay."""
        return await accounts.confirm_email_change(payload.token, ip=deps.client_address(request))

    deletion_terms_model = deps.deletion_terms_model

    @router.get("/me/deletion", response_model=deletion_terms_model)
    async def deletion_terms(user: User, accounts: Accounts) -> BaseModel:
        """What deleting the account would do, said before it is asked for."""
        return deletion_terms_model.model_validate(await accounts.deletion_terms(user))

    deletion_model = deps.deletion_model
    deletion_result_model = deps.deletion_result_model

    @router.post(
        "/me/deletion",
        response_model=deletion_result_model,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=_guards(deps, "deleting the account"),
    )
    async def request_deletion(payload: deletion_model, user: User, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """Delete the account: deactivated now, erased later (user-admin §6.4)."""
        body = cast(DeletionRequest, payload)
        done = await accounts.request_deletion(user, body.password, body.confirm_email)
        return deletion_result_model.model_validate(done)

    @router.get(
        "/me/export",
        response_class=Response,
        responses={200: {"content": {"application/json": {}}, "description": "The account export, as a download"}},
        dependencies=_guards(deps, "the account export"),
    )
    async def export_account(user: User, accounts: Accounts) -> Response:
        """The account's data as one JSON download in the kit's envelope (user-admin §6.5)."""
        envelope = await accounts.export(user)
        filename = export_filename(deps.policy.app, deps.now())
        return Response(
            content=json.dumps(jsonable_encoder(envelope), ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
        )

    @router.post("/verify-email", response_model=EmailVerificationStatus)
    async def verify_email(payload: TokenRequest, request: Request, accounts: Accounts) -> EmailVerificationStatus:
        """Redeem a verification link, anonymously, by POST (§7)."""
        return await accounts.verify_email(payload.token, ip=deps.client_address(request))

    @router.post("/resend-verification", response_model=EmailVerificationStatus)
    async def resend_verification(user: User, accounts: Accounts) -> EmailVerificationStatus:
        """Send the verification mail again; ``503 mail_not_sent`` when it could not go."""
        return await accounts.resend_verification(user)

    @router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
    async def request_password_reset(payload: PasswordResetRequest, request: Request, accounts: Accounts) -> Response:
        """Mail a reset link: always ``204``, account or not."""
        await accounts.request_password_reset(payload, ip=deps.client_address(request))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    reset_state_model = deps.reset_state_model

    @router.post("/password-reset/check", response_model=reset_state_model)
    async def check_password_reset(payload: TokenRequest, request: Request, accounts: Accounts) -> BaseModel:
        """Is this reset link still good? Without spending it."""
        user, extras = await accounts.check_password_reset(payload.token, ip=deps.client_address(request))
        return reset_state_model.model_validate({"email": user.email, **extras})

    @router.post("/password-reset/confirm", response_model=reset_state_model)
    async def confirm_password_reset(payload: PasswordResetConfirm, request: Request, accounts: Accounts) -> BaseModel:
        """Set the new password and end every session; no tokens (§6.3)."""
        user, extras = await accounts.confirm_password_reset(payload, ip=deps.client_address(request))
        return reset_state_model.model_validate({"email": user.email, **extras})

    invitation_check_model = deps.invitation_check_model

    @router.post("/invitations/check", response_model=invitation_check_model)
    async def check_invitation(payload: TokenRequest, request: Request, accounts: Accounts) -> BaseModel:
        """What an invitation is for, without spending it (§4.4)."""
        invitation, exists, extras = await accounts.check_invitation(payload.token, ip=deps.client_address(request))
        return invitation_check_model.model_validate(
            {
                "email": invitation.email,
                "expires_at": invitation.expires_at,
                "account_exists": exists,
                "role": invitation.role,
                **extras,
            }
        )

    invitation_accepted_model = deps.invitation_accepted_model

    @router.post(
        "/invitations/accept", response_model=invitation_accepted_model, dependencies=_guards(deps, "invitations")
    )
    async def accept_invitation(payload: TokenRequest, request: Request, user: User, accounts: Accounts) -> BaseModel:
        """An existing account takes up an invitation (§4.4)."""
        invitation, extras = await accounts.accept_invitation(user, payload.token, ip=deps.client_address(request))
        return invitation_accepted_model.model_validate({"email": invitation.email, "role": invitation.role, **extras})

    if deps.passkeys is not None:
        _passkey_routes(router, deps, Accounts, User, sign_in_answer)
    return router


def _passkey_routes(
    router: APIRouter,
    deps: AuthDeps,
    Accounts: Any,  # noqa: N803
    User: Any,  # noqa: N803
    sign_in_answer: Callable[..., Awaitable[BaseModel]],
) -> None:
    """The passkey routes (§12.5 item 8), on ``router``."""
    token_model = _built(deps).token_model
    passkey_guards = _guards(deps, "passkeys")

    @router.post("/passkey/register/begin", response_model=PasskeyOptions, dependencies=passkey_guards)
    async def begin_passkey_registration(
        payload: PasskeyRegistrationBegin, user: User, accounts: Accounts
    ) -> PasskeyOptions:
        """The current password, then the options for ``navigator.credentials.create``."""
        result: PasskeyOptions = await accounts.begin_passkey_registration(user, payload)
        return result

    @router.post(
        "/passkey/register/finish",
        response_model=PasskeyResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=passkey_guards,
    )
    async def finish_passkey_registration(
        payload: PasskeyRegistrationFinish, user: User, accounts: Accounts
    ) -> PasskeyResponse:
        """The authenticator's answer: the passkey added, the address told."""
        return PasskeyResponse.model_validate(await accounts.finish_passkey_registration(user, payload))

    @router.post("/passkey/login/begin", response_model=PasskeyOptions)
    async def begin_passkey_login(accounts: Accounts) -> PasskeyOptions:
        """The options for ``navigator.credentials.get``: usernameless."""
        result: PasskeyOptions = await accounts.begin_passkey_login()
        return result

    passkey_login_model = deps.passkey_login_model

    @router.post("/passkey/login/finish", response_model=token_model | deps.challenge_model)
    async def finish_passkey_login(payload: passkey_login_model, request: Request, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """Sign in with a passkey: a session, or the forced change's challenge."""
        outcome = await accounts.finish_passkey_login(payload, ip=deps.client_address(request), device=_device(request))
        return await sign_in_answer(accounts, outcome)

    @router.get("/passkeys", response_model=list[PasskeyResponse])
    async def list_passkeys(user: User, accounts: Accounts) -> list[PasskeyResponse]:
        """The account's passkeys, oldest first."""
        return [PasskeyResponse.model_validate(row) for row in await accounts.passkeys_of(user)]

    @router.patch("/passkeys/{passkey_id}", response_model=PasskeyResponse, dependencies=passkey_guards)
    async def rename_passkey(
        passkey_id: int, payload: PasskeyRename, user: User, accounts: Accounts
    ) -> PasskeyResponse:
        """Rename one of the account's passkeys."""
        return PasskeyResponse.model_validate(await accounts.rename_passkey(user, passkey_id, payload.name))

    @router.delete("/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=passkey_guards)
    async def delete_passkey(passkey_id: int, user: User, accounts: Accounts) -> Response:
        """Delete one of the account's passkeys, the last one included."""
        await accounts.remove_passkey(user, passkey_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def invitations_admin_router(
    deps: AuthDeps,
    *,
    prefix: str = "/admin/invitations",
    tags: Sequence[str] = ("admin",),
    guard: Callable[..., Any] | None = None,
) -> APIRouter:
    """The admin's invitation routes (user-admin §5; §12.6: an invitation-only app lets
    nobody in without them): ``GET ""`` the list, ``POST ""`` a new one, ``POST
    /{invitation_id}/resend``, ``DELETE /{invitation_id}``. Every route is behind ``guard``,
    the kit's :func:`admin_guard` unless the app passes its own (its route walks find their
    own name). Each mail goes after the commit; the answer carries the link once while mail
    goes to the console (§12.5 item 11)."""
    built = _built(deps)
    Accounts = Annotated[AccountService[Any, Any, Any], Depends(built.service)]  # noqa: N806
    Admin = Annotated[Any, Depends(built.current_user)]  # noqa: N806
    guards = [Depends(guard or built.admin_guard)]
    row_model = deps.invitation_row_model
    issued_model = deps.invitation_issued_model
    create_model = deps.invitation_create_model
    show_link = deps.mail_backend == MAIL_BACKEND_CONSOLE
    router = APIRouter(prefix=prefix, tags=list(tags))

    def issued(fields: Mapping[str, Any]) -> BaseModel:
        return issued_model.model_validate({**fields, "mail_backend": deps.mail_backend})

    @router.get("", response_model=list[row_model], dependencies=guards)  # type: ignore[valid-type]
    async def list_invitations(accounts: Accounts) -> list[BaseModel]:
        """Every invitation not accepted, newest first; never a link."""
        return [row_model.model_validate(fields) for fields in await accounts.invitation_rows()]

    @router.post("", response_model=issued_model, status_code=status.HTTP_201_CREATED, dependencies=guards)
    async def create_invitation(payload: create_model, admin: Admin, accounts: Accounts) -> BaseModel:  # type: ignore[valid-type]
        """Invite an address, in the invitee's language."""
        return issued(await accounts.create_invitation(admin, payload, show_link=show_link))

    @router.post("/{invitation_id}/resend", response_model=issued_model, dependencies=guards)
    async def resend_invitation(invitation_id: int, admin: Admin, accounts: Accounts) -> BaseModel:
        """A new link and 14 more days; the old link dead."""
        return issued(await accounts.resend_invitation(admin, invitation_id, show_link=show_link))

    @router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=guards)
    async def revoke_invitation(invitation_id: int, admin: Admin, accounts: Accounts) -> Response:
        """Withdraw an open or expired invitation."""
        await accounts.revoke_invitation(admin, invitation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
