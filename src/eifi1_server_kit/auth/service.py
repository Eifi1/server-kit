"""The account service: sign-up, sign-in, the session, the account's own settings, written
once against the ports.

Layer 2 (``docs/auth-harmonization.md`` §12 in ``Eifi1/ui-kit``; the pattern of
:mod:`eifi1_server_kit.feedback.service`). What the three apps each wrote around the layer-1
rules — the lookups, the gate, the claims, the limits, the links and their mails, the
session rows — as one :class:`AccountService` over the app's
:mod:`~eifi1_server_kit.auth.ports`. No FastAPI: :mod:`eifi1_server_kit.auth.router` builds one
per request on the request's stores, and a caller without a router (a command-line tool)
builds its own.

**The kit settles where the copies differ** (§12.5), each point one app's today:

1. session rows and ``sid`` in every token; ``logout_everywhere`` ends every row (and stamps
   the ``iat`` cut-off where the row has one), ``end_session`` this one;
2. **every account mail after the commit** (:class:`~eifi1_server_kit.auth.ports.AfterCommit`):
   a route whose answer says whether its mail went out commits first
   (``commit_now``) and then sends; every other mail waits in ``later``;
3. a code on every refusal (:class:`~eifi1_server_kit.auth.AuthError`, ``AccountError``);
4. a limit on every anonymous redemption, on the refresh, and on every signed-in password
   check (:class:`~eifi1_server_kit.auth.AuthLimiters`), charged before the work;
5. sign-up checks in the order invitation, gate, ``email_taken``, so an anonymous caller
   learns nothing about addresses it holds no invitation for;
6. a reset ends a forced password change, ends every session and marks the address
   verified (it proved it), and answers no tokens;
7. a forced password change ends the person's sessions;
8. passkeys: user verification required, one use per challenge (:mod:`~eifi1_server_kit.auth.passkeys`);
9. only open, unexpired invitations attach at an address confirmation;
10. the kit's mail texts (:mod:`~eifi1_server_kit.auth.mails`).

**The kit never commits** but where ``commit_now`` says (§2.7 of
``docs/server-services-harmonization.md``): every store write flushes. A refusal raised
after a write leaves it to the app's rollback.

**The hooks** (:class:`AuthHooks`, every one optional and async) are what differs between
the apps (§12.2's six seams): what a sign-up or an invitation attaches, what a token carries
and what each request binds, the private mode around passwords and passkeys, what else ends
with the sessions, the deletion's outcomes, and the extras on the answers.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from pydantic import BaseModel

from eifi1_server_kit.auth.accounts import (
    addresses_for_reset,
    env_list_match,
    erasure_identifiers,
    full_name,
    invitation_accepts,
    normalise_email,
    registration_decision,
    verified_by_invitation,
)
from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.limits import AuthLimiters
from eifi1_server_kit.auth.mails import ACCOUNT_MAIL_KINDS, ACCOUNT_MAIL_TEXTS, AccountMailText, mail_date
from eifi1_server_kit.auth.passkeys import (
    DEFAULT_PASSKEY_NAME,
    PasskeyCeremony,
    PasskeyCheckError,
    RelyingParty,
    authentication_options,
    base64url_decode,
    base64url_encode,
    credential_id_of,
    registration_options,
    user_handle_of,
    verify_authentication,
    verify_registration,
)
from eifi1_server_kit.auth.passwords import absent_account_check
from eifi1_server_kit.auth.ports import (
    AccountRow,
    AdminActionLog,
    AfterCommit,
    InvitationRow,
    InvitationStore,
    NewAccount,
    NewInvitation,
    NewPasskey,
    NewSession,
    OneTimeTokenStore,
    PasskeyRow,
    PasskeyStore,
    PasswordHasher,
    SessionRow,
    SessionStore,
    TokenSigner,
    UserStore,
)
from eifi1_server_kit.auth.schemas import (
    EmailChangeConfirmed,
    EmailChangeState,
    EmailVerificationStatus,
    LoginRequest,
    PasskeyLoginFinish,
    PasskeyOptions,
    PasskeyRegistrationBegin,
    PasskeyRegistrationFinish,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    SessionInfo,
    SetPasswordRequest,
)
from eifi1_server_kit.auth.sessions import (
    SessionPolicy,
    access_lifetime,
    refresh_lifetime,
    session_expired,
    session_expires_at,
)
from eifi1_server_kit.auth.tokens import (
    ACCESS_TOKEN_TYPE,
    CHALLENGE_LIFETIMES,
    EMAIL_CHANGE_TTL,
    INVITE_TTL,
    REFRESH_TOKEN_TYPE,
    RESET_TTL,
    VERIFY_TTL,
    ChallengeKind,
    OneTimeTokenKind,
    _aware,
    access_claims,
    challenge_claims,
    hash_token,
    mint,
    refresh_claims,
    shared_device_of,
    token_is_revoked,
)
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.demo import refuse_demo
from eifi1_server_kit.limiter import charge
from eifi1_server_kit.mail import Mailer, MailMessage, render_mail
from eifi1_server_kit.user_admin.actions import AdminAction, confirmation_level
from eifi1_server_kit.user_admin.audit import admin_action_record
from eifi1_server_kit.user_admin.deletion import (
    DEFAULT_DELETION_DAYS,
    DeletionMode,
    RetentionNote,
    deletion_due,
    deletion_mail_retention_note,
    deletion_schedule,
)
from eifi1_server_kit.user_admin.errors import AccountError, AccountErrorCode
from eifi1_server_kit.user_admin.export import export_envelope
from eifi1_server_kit.user_admin.ports import AcceptedInvitations
from eifi1_server_kit.user_admin.rules import refuse_last_admin, require_confirmation
from eifi1_server_kit.user_admin.schemas import (
    InvitationCreate,
    InvitationStatus,
    MailKind,
    PersonRef,
    invitation_status,
)

if TYPE_CHECKING:
    from eifi1_server_kit.feedback.service import FeedbackService

__all__ = [
    "AUTH_THROTTLED",
    "AccountLinks",
    "AccountPolicy",
    "AccountService",
    "AuthHooks",
    "ChallengeIssued",
    "IssuedInvitation",
    "IssuedSession",
    "SessionsEndedReason",
    "SignedIn",
    "StartedSession",
]

logger = logging.getLogger("eifi1_server_kit.auth")

#: The 429 sentence of each :class:`~eifi1_server_kit.auth.AuthLimiters` window, keksdose's
#: words; ``Retry-After`` says how long (:func:`~eifi1_server_kit.limiter.charge`).
AUTH_THROTTLED: Mapping[str, str] = {
    "login": "Too many sign-in attempts — please try again in a moment.",
    "challenge": "Too many attempts — please try again in a moment.",
    "register": "Too many new accounts from this address — please try again later.",
    "reset": "Too many password reset requests — please try again in a moment.",
    "redeem": "Too many attempts — please try again in a moment.",
    "refresh": "Too many session renewals — please try again in a moment.",
    "password_check": "Too many password attempts — please try again in a few minutes.",
    "email_change": "Too many address changes requested — please try again later.",
    "mail": "Too many mails to this address — please try again later.",
    "export": "You exported your data a moment ago — please try again in a minute.",
}

#: How much later than a cut-off stamped in the same flow its fresh tokens are minted, so
#: :func:`~eifi1_server_kit.auth.token_is_revoked` (revoked AT the cut-off) lets them live
#: on a clock that has not moved (a test's, a coarse one).
_AFTER_CUTOFF = timedelta(milliseconds=1)
#: The ``sub`` of a passkey sign-in's challenge: nobody yet (``ChallengeKind.WEBAUTHN_AUTH``).
_NOBODY = "webauthn"
#: What a passkey sign-in's refusal logs as the reason, never answers.
_REFUSED = "Passkey sign-in refused: %s"

_APP_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: The refresh token's claim of a session with a fixed end (:meth:`AccountService.start_session`):
#: the end as a POSIX timestamp, carried from refresh to refresh.
_END_CLAIM = "end"


class SessionsEndedReason(enum.StrEnum):
    """Why every session of an account ended — ``AuthHooks.sessions_ended``'s second argument
    (§12.3 seam 4: keksdose revokes its API tokens and push devices at some of these)."""

    #: ``POST /auth/logout``.
    LOGOUT_EVERYWHERE = "logout_everywhere"
    #: ``POST /auth/me/password``: every OTHER session; the caller's goes on.
    PASSWORD_CHANGED = "password_changed"
    #: ``POST /auth/password-reset/confirm``.
    PASSWORD_RESET = "password_reset"
    #: ``POST /auth/login/set-password``: the admin's forced change, done.
    FORCED_CHANGE = "forced_change"
    #: An admin deactivated the account (``user_admin_router``, 0.30).
    DEACTIVATED = "deactivated"
    #: ``POST /auth/me/deletion``.
    DELETION_REQUESTED = "deletion_requested"
    #: An admin required a new password (``user_admin_router``, 0.30): every session ends, and
    #: the next sign-in asks for the new password first.
    PASSWORD_CHANGE_REQUIRED = "password_change_required"


type _Extras = Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthHooks[UserT: AccountRow = Any, InvT: InvitationRow = Any]:
    """What differs between the apps (§12.3), as optional coroutines the app's hook
    dependency answers per request, on the request's own session — so ``on_register`` can
    write the app's rows in the sign-up's transaction (kastlan's company). Each may raise a
    kit refusal (``AuthError``, ``AccountError``) to refuse the step. A hook that answers a
    mapping adds its keys to the route's answer, which the app's answer model (an
    ``AuthDeps`` field) declares."""

    #: Seam 1: a sign-up, with the invitation that let it in (``None`` for the first account),
    #: and the app's body (its ``register_model``): the household, the company, the team.
    on_register: Callable[[UserT, InvT | None, RegisterRequest], Awaitable[None]] | None = None
    #: Seam 1: an existing account took up an invitation (``/invitations/accept``), or an
    #: address confirmation found an open one for the new address (§12.5 item 9).
    on_invitation_redeemed: Callable[[UserT, InvT], Awaitable[None]] | None = None
    #: The answer's words for an invitation (its team's name), on ``/invitations/check``,
    #: ``/accept`` and the admin's rows.
    describe_invitation: Callable[[InvT], Awaitable[_Extras]] | None = None
    #: The admin named a ``scope`` (``InvitationCreate.scope``): what the store keeps for it,
    #: or a refusal (a team the admin may not invite into). Without it a scope is refused.
    invitation_scope: Callable[[UserT, int | str], Awaitable[Mapping[str, Any]]] | None = None
    #: Seam 2: the access token's extra claims (kastlan's ``company_id`` and ``roles``); may
    #: refuse with ``session_ended`` at a refresh (a lost membership). ``sid`` is the kit's.
    claims: Callable[[UserT, SessionRow], Awaitable[_Extras]] | None = None
    #: Seam 2: every signed-in request, after the kit's checks: bind the request (kastlan's
    #: RLS variables), or refuse (its 401 for a lost membership, 403 for a closed company).
    bind: Callable[[UserT, Mapping[str, Any]], Awaitable[None]] | None = None
    #: Seam 4: every session of the account ended, and why: what else ends with them.
    sessions_ended: Callable[[UserT, SessionsEndedReason], Awaitable[None]] | None = None
    #: Seam 3: a password is about to change (the voluntary change and the forced one),
    #: with the app's body: refuse, or answer extras (keksdose's key wrap,
    #: ``key_wrap_unchanged``).
    password_changing: Callable[[UserT, BaseModel], Awaitable[_Extras | None]] | None = None
    #: Seam 3: the forced-change challenge's extras (keksdose's ``encrypted``).
    challenge_extras: Callable[[UserT], Awaitable[_Extras]] | None = None
    #: Seam 3: a reset's extras, at its check (``False``) and after its confirm (``True``):
    #: keksdose's ``encrypted``, its revoked API tokens.
    reset_extras: Callable[[UserT, bool], Awaitable[_Extras]] | None = None
    #: Seam 3: WebAuthn extensions for a ceremony (keksdose's ``prf``); the account is
    #: ``None`` at the usernameless sign-in.
    passkey_extensions: Callable[[UserT | None, PasskeyCeremony], Awaitable[_Extras]] | None = None
    #: Seam 3: a passkey is about to go: refuse (keksdose's last unlock path).
    passkey_removing: Callable[[UserT, PasskeyRow], Awaitable[None]] | None = None
    #: Seam 5: what a deletion would do, on ``GET /auth/me/deletion`` (Kurvenschmiede's
    #: ``hand_over_count``).
    deletion_terms: Callable[[UserT], Awaitable[_Extras]] | None = None
    #: Seam 5: the account asked to be deleted and is deactivated: cancel or tell what is
    #: the app's (billing, budget guests); the extras ride on the answer.
    on_deletion_requested: Callable[[UserT], Awaitable[_Extras | None]] | None = None
    #: Seam 5: an admin reactivated the account before its erasure (0.30's admin routes).
    on_deletion_withdrawn: Callable[[UserT], Awaitable[None]] | None = None
    #: Seam 5: the export's ``data`` (user-admin §6.5): the app's own rows.
    export_data: Callable[[UserT], Awaitable[_Extras]] | None = None
    #: Seam 5 (0.30): the account is being erased (:meth:`AccountService.erase`): delete or hand
    #: over the app's own rows (keksdose's budgets, Kurvenschmiede's shared work). The mapping
    #: it answers — counts and ids, never content — rides on the admin's answer and in the
    #: ``erase`` row's detail.
    erase: Callable[[UserT], Awaitable[_Extras | None]] | None = None
    #: Seam 5 (0.30): the stored files an erasure left nobody naming (the feedback's), deleted
    #: after the commit (keksdose's object purge). Without it they are left to the app.
    purge_files: Callable[[Sequence[str]], Awaitable[None]] | None = None
    #: Seam 6: ``/auth/me`` and the user in every token answer, as the app's ``me_model``;
    #: without it, ``me_model.model_validate(user)`` with ``email_verified`` from the row.
    to_me: Callable[[UserT], Awaitable[BaseModel]] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountLinks:
    """Where the mails' links lead: the app's own pages, on its public base URL (the ui-kit's
    signed-out pages, auth §7)."""

    #: ``https://staunlabor.app`` — the frontend's origin, as the mails link it.
    base_url: str
    reset_password: str = "/reset-password"
    verify_email: str = "/verify-email"
    #: The address change's link (Kurvenschmiede's page; the kit's ``VerifyEmailStatus``).
    confirm_email: str = "/confirm-email"
    #: An invitation's link: ``/register?invite=<token>`` (§4.4).
    register: str = "/register"
    #: The notices' link: the passkey list, the address change's "not you?".
    settings: str = "/settings"
    #: The deletion mail's link.
    home: str = "/"

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError(f"base_url is the app's http(s) origin: {self.base_url[:40]!r}")

    def url(self, path: str, **query: str) -> str:
        """``base_url`` + ``path`` + the query, encoded."""
        link = self.base_url.rstrip("/") + path
        return f"{link}?{urlencode(query)}" if query else link


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountPolicy:
    """The app's choices the account layer reads, one per app."""

    #: The app's name in lower case: the address tag (``you+staunlabor@…``, §4.5) and the
    #: export's ``app``.
    app: str
    #: The app's name as the mails write it (``Staunlabor``).
    app_name: str
    links: AccountLinks
    #: A person to write to, as the mails name one (the app's support address).
    contact: str
    #: The role the first account gets and the admin routes require (§4.3).
    admin_role: str = "ADMIN"
    #: The role of an account an invitation names none for.
    default_role: str = "MEMBER"
    #: The roles an invitation may name (0.30); another is ``422 role_not_grantable``, and
    #: ``default_role`` is one of them. ``None``: unchecked, as in 0.29. The admin router's
    #: role change gives the same roles (``AdminPolicy.grantable_roles``).
    invitable_roles: tuple[str, ...] | None = None
    #: The environment list that bootstraps the first admin (``parse_env_list``); ``None``:
    #: the first registration is open (``registration_decision``).
    bootstrap: frozenset[str] | None = None
    sessions: SessionPolicy = field(default_factory=SessionPolicy)
    deletion_mode: DeletionMode = DeletionMode.AFTER_DAYS
    deletion_days: int = DEFAULT_DELETION_DAYS
    #: What the deletion mail says may outlive the erasure.
    retention: RetentionNote = field(default_factory=deletion_mail_retention_note)

    def __post_init__(self) -> None:
        if not _APP_NAME.match(self.app):
            raise ValueError(f"app is the app's name in lower case — letters, digits, '.', '_' or '-': {self.app!r}")
        if self.deletion_days < 0:
            raise ValueError("deletion_days is zero or more")
        if self.invitable_roles is not None and self.default_role not in self.invitable_roles:
            raise ValueError(f"default_role {self.default_role!r} is one of invitable_roles: {self.invitable_roles}")


@dataclass(frozen=True, slots=True)
class IssuedSession[UserT: AccountRow = Any]:
    """A session started or renewed: the pair, and the hooks' extras for the answer."""

    user: UserT
    session: SessionRow
    access_token: str
    refresh_token: str
    extras: _Extras = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChallengeIssued[UserT: AccountRow = Any]:
    """The sign-in passed and the account must choose a new password first (§5.1)."""

    user: UserT
    challenge_token: str
    extras: _Extras = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SignedIn[UserT: AccountRow = Any]:
    """A signed-in request: the account, its session row and the token's claims."""

    user: UserT
    session: SessionRow
    claims: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StartedSession[UserT: AccountRow = Any]:
    """A session an app started itself (:meth:`AccountService.start_session`, 0.30): its row,
    the access token, the refresh token only when one was asked for, and when the session
    ends at the latest — the access token's end too when there is no refresh token."""

    user: UserT
    session: SessionRow
    access_token: str
    refresh_token: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedInvitation[InvT: InvitationRow = Any]:
    """An invitation minted or re-minted: its link (the only time the raw token is in the
    server's hands) and whether its mail went out."""

    invitation: InvT
    link: str
    sent: bool


def _expired(moment: datetime | None, now: datetime) -> bool:
    return moment is None or _aware(moment) <= now


def _end_of(claims: Mapping[str, Any]) -> datetime | None:
    """A refresh token's fixed end (``_END_CLAIM``), or None for a session that slides."""
    end = claims.get(_END_CLAIM)
    return datetime.fromtimestamp(end, UTC) if isinstance(end, int | float) else None


def _unspent(invitation: InvitationRow) -> bool:
    return invitation.accepted_at is None and invitation.revoked_at is None


class AccountService[UserT: AccountRow, SessionT: SessionRow, InvT: InvitationRow]:
    """The account's flows over the app's stores, for one request. The limiters are the
    app's (``app.state``); ``absent_check`` is :func:`~eifi1_server_kit.auth.absent_account_check`
    built ONCE per app (the router does), since its dummy hash costs a hash."""

    def __init__(
        self,
        *,
        users: UserStore[UserT],
        sessions: SessionStore[SessionT],
        tokens: OneTimeTokenStore[Any],
        invitations: InvitationStore[InvT],
        log: AdminActionLog[Any],
        after_commit: AfterCommit,
        signer: TokenSigner,
        hasher: PasswordHasher,
        mailer: Mailer,
        policy: AccountPolicy,
        limiters: AuthLimiters,
        hooks: AuthHooks[UserT, InvT] | None = None,
        passkeys: PasskeyStore[Any] | None = None,
        relying_party: RelyingParty | None = None,
        mail_texts: Mapping[AccountMailText, Any] = ACCOUNT_MAIL_TEXTS,
        absent_check: Callable[[str], bool] | None = None,
        now: Clock = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._tokens = tokens
        self._invitations = invitations
        self._log = log
        self._after = after_commit
        self._signer = signer
        self._hasher = hasher
        self._mailer = mailer
        self._policy = policy
        self._limiters = limiters
        self._hooks: AuthHooks[UserT, InvT] = hooks or AuthHooks()
        self._passkeys = passkeys
        self._rp = relying_party
        self._texts = mail_texts
        self._absent = absent_check or absent_account_check(hasher.hash, hasher.verify)
        self._now = now
        self._sleep = sleep

    # --- the answers' pieces -------------------------------------------------------------

    async def me(self, user: UserT, model: type[BaseModel]) -> BaseModel:
        """The account as ``/auth/me`` answers it: ``to_me``, else ``model`` from the row
        with ``email_verified`` read off ``email_verified_at``."""
        if self._hooks.to_me is not None:
            return await self._hooks.to_me(user)
        answer = model.model_validate(user, from_attributes=True)
        return answer.model_copy(update={"email_verified": user.email_verified_at is not None})

    async def _extras[T](self, hook: Callable[..., Awaitable[T]] | None, *args: Any) -> dict[str, Any]:
        if hook is None:
            return {}
        return dict(cast(Mapping[str, Any], await hook(*args) or {}))

    # --- sessions and tokens -------------------------------------------------------------

    async def _start(self, user: UserT, *, shared: bool, ip: str | None, device: str | None, now: datetime) -> SessionT:
        expires_at = session_expires_at(self._policy.sessions, shared=shared, started_at=now, last_active=now)
        return await self._sessions.start(
            NewSession(
                sid=mint().raw,
                user_id=user.id,
                started_at=now,
                expires_at=expires_at,
                shared_device=shared,
                device=device,
                ip_address=ip,
            )
        )

    async def _access_claims(self, user: UserT, session: SessionRow) -> dict[str, Any]:
        return {**(await self._extras(self._hooks.claims, user, session)), "sid": session.sid}

    async def _pair(
        self,
        user: UserT,
        session: SessionRow,
        *,
        now: datetime,
        extras: _Extras | None = None,
        end: datetime | None = None,
    ) -> IssuedSession[UserT]:
        """An access and a refresh token for ``session``, minted at ``now``; both carry its
        ``sid``, the access token the ``claims`` hook's too. ``end``: a session with a fixed
        end (:meth:`start_session`): neither token outlives it, and the refresh token carries
        it on (``end``), so no refresh ever moves the session past it."""
        policy = self._policy.sessions
        shared, started = session.shared_device, session.started_at
        access_life = access_lifetime(policy, shared=shared, started_at=started, now=now)
        refresh_life = refresh_lifetime(policy, shared=shared, started_at=started, now=now)
        refresh_extra: dict[str, Any] = {"sid": session.sid}
        if end is not None:
            access_life, refresh_life = min(access_life, end - now), min(refresh_life, end - now)
            refresh_extra[_END_CLAIM] = end.timestamp()
        access = access_claims(user.id, now=now, lifetime=access_life, extra=await self._access_claims(user, session))
        refresh = refresh_claims(user.id, now=now, lifetime=refresh_life, extra=refresh_extra)
        return IssuedSession(user, session, self._signer.sign(access), self._signer.sign(refresh), dict(extras or {}))

    async def start_session(
        self,
        user: UserT,
        *,
        expires_at: datetime,
        refresh: bool = False,
        shared_device: bool = False,
        ip: str | None = None,
        device: str | None = None,
    ) -> StartedSession[UserT]:
        """A session for an account the app has just made, with a fixed end (auth §12.6, 0.30):
        Kurvenschmiede's ``demo_router`` and keksdose's 60-minute demo. The demo routes stay
        the app's; this is the one entry they need.

        A session row (``sid``) that ends at ``expires_at`` (naive is UTC), and its access
        token with the ``sid`` and the ``claims`` hook's claims, living to that end. Without
        ``refresh`` (the demo's) there is no refresh token, and since only ``/auth/refresh``
        moves a row's ``expires_at``, the session keeps its end. With ``refresh``, a refresh
        token too; the policy's lifetimes still hold, cut to the end, and no refresh moves the
        session past it. ``shared_device`` keeps the shared limits (auth §6.2): nothing moves
        a shared session's idle limit but a refresh, so one started without a refresh token
        ends there, before a later ``expires_at``. ``last_login_at`` is the app's to stamp.
        An end that has passed is a :class:`ValueError`."""
        now = self._now()
        end = _aware(expires_at)
        if end <= now:
            raise ValueError("expires_at lies ahead: a session cannot start already ended")
        limit = min(
            end, session_expires_at(self._policy.sessions, shared=shared_device, started_at=now, last_active=now)
        )
        session = await self._sessions.start(
            NewSession(
                sid=mint().raw,
                user_id=user.id,
                started_at=now,
                expires_at=limit,
                shared_device=shared_device,
                device=device,
                ip_address=ip,
            )
        )
        if refresh:
            issued = await self._pair(user, session, now=now, end=end)
            return StartedSession(user, session, issued.access_token, issued.refresh_token, end)
        access = access_claims(user.id, now=now, lifetime=limit - now, extra=await self._access_claims(user, session))
        return StartedSession(user, session, self._signer.sign(access), None, limit)

    def _cutoff(self, user: UserT, now: datetime) -> dict[str, Any]:
        """The ``iat`` cut-off, where the app's row has one (§12.3 seam 4): a new app ends
        sessions through the rows only, and its store refuses a column it lacks."""
        return {"sessions_invalid_before": now} if hasattr(user, "sessions_invalid_before") else {}

    async def _sessions_ended(self, user: UserT, reason: SessionsEndedReason) -> None:
        """What every "every session ends" does beside the rows: the pending address change
        goes too (keksdose: its notice names a new password as the remedy), then the hook."""
        await self._tokens.drop(OneTimeTokenKind.EMAIL_CHANGE, user.id)
        if self._hooks.sessions_ended is not None:
            await self._hooks.sessions_ended(user, reason)

    async def end_sessions(self, user: UserT, reason: SessionsEndedReason) -> int:
        """Every session of the account ends, for ``reason`` (0.30, the admin's deactivation
        and forced password change): every row, the ``iat`` cut-off where the row has one, the
        pending address change, and the ``sessions_ended`` hook (keksdose's API tokens and push
        devices). Nothing else is cleared. Answers how many rows ended."""
        ended = await self._sessions.end_all(user.id)
        if cutoff := self._cutoff(user, self._now()):
            await self._users.apply(user, cutoff)
        await self._sessions_ended(user, reason)
        return ended

    async def _sign_in(
        self, user: UserT, *, shared: bool, ip: str | None, device: str | None
    ) -> IssuedSession[UserT] | ChallengeIssued[UserT]:
        """What a sign-in that passed answers, by password or by passkey: a session, or,
        while an admin requires a new password, the challenge to set one first
        (user-admin §4.1). ``last_login_at`` waits for the session."""
        now = self._now()
        if user.password_change_required_at is not None:
            claims = challenge_claims(user.id, ChallengeKind.PASSWORD_CHANGE, now=now, shared_device=shared)
            extras = await self._extras(self._hooks.challenge_extras, user)
            return ChallengeIssued(user, self._signer.sign(claims), extras)
        user = await self._users.apply(user, {"last_login_at": now})
        session = await self._start(user, shared=shared, ip=ip, device=device, now=now)
        return await self._pair(user, session, now=now)

    async def _live(self, claims: Mapping[str, Any]) -> tuple[UserT, SessionT]:
        """The account and session a signed token names, both alive, or ``session_ended``:
        the row ended, expired or someone else's; the account gone or deactivated; the
        token minted at or before the cut-off."""
        sid, sub = claims.get("sid"), claims.get("sub")
        session = await self._sessions.get(sid) if isinstance(sid, str) and sid else None
        if (
            session is None
            or not session.is_active
            or str(session.user_id) != str(sub)
            or _expired(session.expires_at, self._now())
            or session_expired(
                self._policy.sessions,
                shared=session.shared_device,
                started_at=session.started_at,
                last_active=session.last_active,
                now=self._now(),
            )
        ):
            raise AuthError(AuthErrorCode.SESSION_ENDED, "The session has ended")
        user = await self._users.get(session.user_id)
        if user is None or not user.is_active:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "The account is not active")
        if token_is_revoked(claims.get("iat"), getattr(user, "sessions_invalid_before", None)):
            raise AuthError(AuthErrorCode.SESSION_ENDED, "The session was ended by a sign-out everywhere")
        return user, session

    async def authenticate(self, token: str | None) -> SignedIn[UserT]:
        """``current_user``'s checks (§12.3): a bearer token this app signed, an access
        token, its session row alive, the cut-off, the account active, then the ``bind``
        hook. ``401 not_signed_in`` without a usable token, ``401 session_ended`` once the
        session is over: the client renews, and signs out on the second (§11.3)."""
        claims = self._signer.verify(token) if token else None
        if claims is None or claims.get("type") != ACCESS_TOKEN_TYPE:
            raise AuthError(AuthErrorCode.NOT_SIGNED_IN, headers={"WWW-Authenticate": "Bearer"})
        user, session = await self._live(claims)
        if self._hooks.bind is not None:
            await self._hooks.bind(user, claims)
        return SignedIn(user, session, claims)

    # --- sign-up and sign-in ---------------------------------------------------------------

    async def _open_invitation(self, token: str) -> InvT:
        """The invitation a token names, open and unexpired, or the coded refusal."""
        invitation = await self._invitations.by_token_hash(hash_token(token))
        if invitation is None or not _unspent(invitation):
            raise AuthError(AuthErrorCode.INVITATION_INVALID)
        if _expired(invitation.expires_at, self._now()):
            raise AuthError(AuthErrorCode.INVITATION_EXPIRED)
        return invitation

    async def register(self, body: RegisterRequest, *, ip: str, device: str | None = None) -> IssuedSession[UserT]:
        """Sign up, signed in on the answer (§4.2–§4.4). In the order of §12.5 item 5: the
        invitation (its own codes), the gate (``registration_closed``), then
        ``email_taken`` — so only an invited address, or the first, can learn it has an
        account. The first account is the admin; the invitation names the role, else the
        app's default. The invitation's exact address counts as verified; anything else
        is sent a verification link after the commit."""
        charge(self._limiters.register, ip, AUTH_THROTTLED["register"])
        invitation: InvT | None = None
        if body.invite_token is not None:
            invitation = await self._open_invitation(body.invite_token)
            if not invitation_accepts(invitation.email, body.email, self._policy.app):
                raise AuthError(AuthErrorCode.INVITATION_INVALID)
        decision = registration_decision(
            is_first_user=await self._users.count_people() == 0,
            on_env_list=env_list_match(body.email, self._policy.bootstrap or ()),
            has_valid_invitation=invitation is not None,
        )
        if not decision.allowed:
            raise AuthError(AuthErrorCode.REGISTRATION_CLOSED)
        if await self._users.by_email(body.email) is not None:
            raise AuthError(AuthErrorCode.EMAIL_TAKEN)
        now = self._now()
        if decision.first_admin:
            role = self._policy.admin_role
        else:
            role = (invitation.role if invitation is not None else None) or self._policy.default_role
        verified = invitation is not None and verified_by_invitation(invitation.email, body.email)
        user = await self._users.add(
            NewAccount(
                email=body.email,
                password_hash=self._hasher.hash(body.password),
                first_name=body.first_name,
                last_name=body.last_name,
                locale=body.locale,
                role=role,
                email_verified_at=now if verified else None,
                last_login_at=now,
                device_timezone=body.device_timezone,
            )
        )
        if invitation is not None:
            await self._invitations.accept(invitation, user_id=user.id, at=now)
        if self._hooks.on_register is not None:
            await self._hooks.on_register(user, invitation, body)
        if not verified:
            link = await self._verification_link(user, now)
            self._send_later(user.email, AccountMailText.VERIFICATION, user.locale, link=link, name=self._name(user))
        session = await self._start(user, shared=body.shared_device, ip=ip, device=device, now=now)
        return await self._pair(user, session, now=now)

    async def login(
        self, body: LoginRequest, *, ip: str, device: str | None = None
    ) -> IssuedSession[UserT] | ChallengeIssued[UserT]:
        """``POST /auth/login`` (§5.1–§5.2): the per-IP limit before any hash, then the
        address's delay (failures only, never a lockout), then one refusal for an unknown
        address, a wrong password and a deactivated account, in the same time (a dummy
        hash for the absent account)."""
        charge(self._limiters.login, ip, AUTH_THROTTLED["login"])
        failures = self._limiters.login_failures
        await self._sleep(failures.delay(body.email))
        user = await self._users.by_email(body.email)
        if user is None:
            accepted = self._absent(body.password)
        else:
            accepted = self._hasher.verify(body.password, user.password_hash) and user.is_active
        if user is None or not accepted:
            failures.record_failure(body.email)
            raise AuthError(AuthErrorCode.INVALID_CREDENTIALS)
        failures.clear(body.email)
        return await self._sign_in(user, shared=body.shared_device, ip=ip, device=device)

    async def set_password(
        self, body: SetPasswordRequest, *, ip: str, device: str | None = None
    ) -> IssuedSession[UserT]:
        """``POST /auth/login/set-password``: the forced change, from the sign-in's
        challenge, then a session. Every session ends with it (§12.5 item 7). A dead
        challenge, or one whose account is gone or deactivated, is ``token_invalid``
        (keksdose's dead challenge: signing in again is the one way on)."""
        charge(self._limiters.challenge, ip, AUTH_THROTTLED["challenge"])
        claims = self._signer.verify(body.challenge_token)
        if claims is None or claims.get("type") != ChallengeKind.PASSWORD_CHANGE:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        charge(self._limiters.challenge_subject, str(claims.get("sub")), AUTH_THROTTLED["challenge"])
        user = await self._account_of(claims)
        if user is None or not user.is_active:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        if user.password_change_required_at is None:
            raise AuthError(AuthErrorCode.PASSWORD_CHANGE_NOT_REQUIRED)
        if self._hasher.verify(body.new_password, user.password_hash):
            raise AuthError(AuthErrorCode.PASSWORD_UNCHANGED)
        extras = await self._extras(self._hooks.password_changing, user, body)
        now = self._now()
        user = await self._users.apply(
            user,
            {
                "password_hash": self._hasher.hash(body.new_password),
                "password_change_required_at": None,
                "last_login_at": now,
                **self._cutoff(user, now),
            },
        )
        await self._sessions.end_all(user.id)
        await self._sessions_ended(user, SessionsEndedReason.FORCED_CHANGE)
        minted = now + _AFTER_CUTOFF
        session = await self._start(user, shared=shared_device_of(claims), ip=ip, device=device, now=minted)
        return await self._pair(user, session, now=minted, extras=extras)

    async def _account_of(self, claims: Mapping[str, Any]) -> UserT | None:
        try:
            return await self._users.get(int(str(claims.get("sub"))))
        except ValueError:
            return None

    async def refresh(self, body: RefreshRequest, *, ip: str) -> IssuedSession[UserT]:
        """``POST /auth/refresh`` (§11): a new pair for the same session, its idle limit
        moved on. Every refusal is ``401 session_ended`` (decision 7); nothing else here
        answers 401 or 403, so a database fault stays a 5xx, which the client retries."""
        charge(self._limiters.refresh, ip, AUTH_THROTTLED["refresh"])
        claims = self._signer.verify(body.refresh_token)
        if claims is None or claims.get("type") != REFRESH_TOKEN_TYPE:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "Not a refresh token of this app")
        user, session = await self._live(claims)
        if user.password_change_required_at is not None:
            raise AuthError(AuthErrorCode.SESSION_ENDED, "The account has to choose a new password")
        now = self._now()
        expires_at = session_expires_at(
            self._policy.sessions, shared=session.shared_device, started_at=session.started_at, last_active=now
        )
        end = _end_of(claims)
        if end is not None:
            expires_at = min(expires_at, end)
        await self._sessions.touch(session, last_active=now, expires_at=expires_at)
        return await self._pair(user, session, now=now, end=end)

    # --- the sessions ----------------------------------------------------------------------

    async def end_session(self, token: str | None) -> None:
        """``DELETE /auth/sessions/current`` (§6.3): end the session this access token names
        — also no error when it had already ended. Only an unusable token is refused."""
        claims = self._signer.verify(token) if token else None
        if claims is None or claims.get("type") != ACCESS_TOKEN_TYPE:
            raise AuthError(AuthErrorCode.NOT_SIGNED_IN, headers={"WWW-Authenticate": "Bearer"})
        sid = claims.get("sid")
        session = await self._sessions.get(sid) if isinstance(sid, str) and sid else None
        if session is not None and session.is_active and str(session.user_id) == str(claims.get("sub")):
            await self._sessions.end(session)

    async def logout_everywhere(self, user: UserT) -> None:
        """``POST /auth/logout`` (§6.3): every session ends, this one included."""
        await self._sessions.end_all(user.id)
        if cutoff := self._cutoff(user, self._now()):
            await self._users.apply(user, cutoff)
        await self._sessions_ended(user, SessionsEndedReason.LOGOUT_EVERYWHERE)

    async def sessions_of(self, signed_in: SignedIn[UserT]) -> list[SessionInfo]:
        """``GET /auth/sessions``: the account's live sessions, the latest activity first,
        the caller's marked ``current`` (§12.5 item 1)."""
        now = self._now()
        policy = self._policy.sessions
        return [
            SessionInfo(
                id=row.sid,
                device=row.device,
                ip_address=row.ip_address,
                started_at=row.started_at,
                last_active=row.last_active,
                expires_at=row.expires_at,
                shared_device=row.shared_device,
                current=row.sid == signed_in.session.sid,
            )
            for row in await self._sessions.live(signed_in.user.id)
            if not _expired(row.expires_at, now)
            and not session_expired(
                policy, shared=row.shared_device, started_at=row.started_at, last_active=row.last_active, now=now
            )
        ]

    async def end_device(self, signed_in: SignedIn[UserT], sid: str) -> None:
        """``DELETE /auth/sessions/{id}``: end one of the account's own sessions."""
        session = await self._sessions.get(sid)
        if session is None or not session.is_active or session.user_id != signed_in.user.id:
            raise AuthError(AuthErrorCode.SESSION_NOT_FOUND)
        await self._sessions.end(session)

    # --- the account ---------------------------------------------------------------------

    async def update_profile(self, user: UserT, body: ProfileUpdate) -> UserT:
        """``PATCH /auth/me`` (§6.1): the fields the body sent. A demo's pick of language,
        text size, contrast and zone stays on its device (Kurvenschmiede): only its name
        is written. An app's own profile fields are its own route."""
        names = ("first_name", "last_name")
        kept = (
            names if getattr(user, "is_demo", False) else (*names, "locale", "text_size", "contrast", "device_timezone")
        )
        changes = {name: getattr(body, name) for name in kept if name in body.model_fields_set}
        return await self._users.apply(user, changes) if changes else user

    async def _check_password(self, user: UserT, password: str) -> None:
        """A signed-in route's password check: the account's window first, every check
        charged; a wrong one is ``400 password_incorrect``, never a 401 (§11.3)."""
        charge(self._limiters.password_check, str(user.id), AUTH_THROTTLED["password_check"])
        if not self._hasher.verify(password, user.password_hash):
            raise AccountError(AccountErrorCode.PASSWORD_INCORRECT)

    async def change_password(self, signed_in: SignedIn[UserT], body: PasswordChangeRequest) -> IssuedSession[UserT]:
        """``POST /auth/me/password`` (§6.3): every OTHER session ends, the caller's goes on
        with a fresh pair minted after the cut-off. A forced change is met too."""
        user = signed_in.user
        refuse_demo(getattr(user, "is_demo", False), "changing the password")
        await self._check_password(user, body.current_password)
        if body.current_password == body.new_password:
            raise AuthError(AuthErrorCode.PASSWORD_UNCHANGED)
        extras = await self._extras(self._hooks.password_changing, user, body)
        now = self._now()
        user = await self._users.apply(
            user,
            {
                "password_hash": self._hasher.hash(body.new_password),
                "password_change_required_at": None,
                **self._cutoff(user, now),
            },
        )
        await self._sessions.end_all(user.id, keep=signed_in.session.sid)
        await self._sessions_ended(user, SessionsEndedReason.PASSWORD_CHANGED)
        return await self._pair(user, signed_in.session, now=now + _AFTER_CUTOFF, extras=extras)

    # --- the address ---------------------------------------------------------------------

    async def email_state(self, user: UserT, *, sent: bool | None = None) -> EmailChangeState:
        """``GET /auth/me/email``: the address, and the change it waits on."""
        pending = await self._tokens.pending(OneTimeTokenKind.EMAIL_CHANGE, user.id)
        return EmailChangeState(
            email=user.email,
            pending_email=pending.email if pending is not None else None,
            pending_expires_at=pending.expires_at if pending is not None else None,
            sent=sent,
        )

    async def _send_email_change(self, user: UserT, address: str) -> bool:
        """Mint the change's link for ``address``, commit, then mail it there and tell the
        old address: whether the link's mail went out."""
        charge(self._limiters.verification_resend, address, AUTH_THROTTLED["mail"])
        raw, digest = mint()
        await self._tokens.put(
            OneTimeTokenKind.EMAIL_CHANGE,
            user_id=user.id,
            token_hash=digest,
            email=address,
            expires_at=self._now() + EMAIL_CHANGE_TTL,
        )
        await self._after.commit_now()
        link = self._policy.links.url(self._policy.links.confirm_email, token=raw)
        name = self._name(user)
        sent = await self._send(address, AccountMailText.EMAIL_CHANGE, user.locale, link=link, name=name)
        await self._send(
            user.email,
            AccountMailText.EMAIL_CHANGE_NOTICE,
            user.locale,
            link=self._policy.links.url(self._policy.links.settings),
            name=name,
            new_email=address,
            contact=self._policy.contact,
        )
        return sent

    async def request_email_change(self, user: UserT, new_email: str, password: str) -> EmailChangeState:
        """``POST /auth/me/email`` (user-admin §6.2): the password again (``400
        password_incorrect``), a link to the new address, a notice to the old. Nothing
        changes until the link is opened; a new request replaces an earlier one."""
        refuse_demo(getattr(user, "is_demo", False), "changing the sign-in address")
        charge(self._limiters.email_change, str(user.id), AUTH_THROTTLED["email_change"])
        await self._check_password(user, password)
        address = normalise_email(new_email)
        if address == user.email:
            raise AuthError(AuthErrorCode.EMAIL_UNCHANGED)
        if await self._users.by_email(address) is not None:
            raise AuthError(AuthErrorCode.EMAIL_TAKEN)
        sent = await self._send_email_change(user, address)
        return await self.email_state(user, sent=sent)

    async def resend_email_change(self, user: UserT) -> EmailChangeState:
        """``POST /auth/me/email/resend``: a new link for the pending change, the old one dead."""
        refuse_demo(getattr(user, "is_demo", False), "changing the sign-in address")
        pending = await self._tokens.pending(OneTimeTokenKind.EMAIL_CHANGE, user.id)
        if pending is None:
            raise AuthError(AuthErrorCode.EMAIL_CHANGE_NOT_PENDING)
        charge(self._limiters.email_change, str(user.id), AUTH_THROTTLED["email_change"])
        sent = await self._send_email_change(user, pending.email)
        return await self.email_state(user, sent=sent)

    async def cancel_email_change(self, user: UserT) -> None:
        """``DELETE /auth/me/email``: the pending change goes; the address stays."""
        await self._tokens.drop(OneTimeTokenKind.EMAIL_CHANGE, user.id)

    async def _redeem(self, kind: OneTimeTokenKind, token: str, ip: str) -> tuple[Any, UserT]:
        """A mailed link's row and account, or the coded refusal: unknown or spent
        (``token_invalid``), run out (``token_expired``), its account gone."""
        charge(self._limiters.redeem, ip, AUTH_THROTTLED["redeem"])
        row = await self._tokens.take(kind, hash_token(token))
        if row is None:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        if _expired(row.expires_at, self._now()):
            raise AuthError(AuthErrorCode.TOKEN_EXPIRED)
        user = await self._users.get(row.user_id)
        if user is None:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        return row, user

    async def confirm_email_change(self, token: str, *, ip: str) -> EmailChangeConfirmed:
        """``POST /auth/me/email/confirm`` (user-admin §6.2), anonymous: the address switches
        and is verified, the sessions stay, and the open, unexpired invitations for the new
        address attach (§12.5 item 9). ``email_taken`` if it found an account meanwhile."""
        row, user = await self._redeem(OneTimeTokenKind.EMAIL_CHANGE, token, ip)
        if not user.is_active:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        holder = await self._users.by_email(row.email)
        if holder is not None and holder.id != user.id:
            raise AuthError(AuthErrorCode.EMAIL_TAKEN)
        now = self._now()
        user = await self._users.apply(user, {"email": row.email, "email_verified_at": now})
        for kind in (OneTimeTokenKind.EMAIL_CHANGE, OneTimeTokenKind.VERIFICATION, OneTimeTokenKind.PASSWORD_RESET):
            await self._tokens.drop(kind, user.id)
        attached = 0
        for invitation in await self._invitations.open_for(row.email):
            if _unspent(invitation) and not _expired(invitation.expires_at, now):
                await self._invitations.accept(invitation, user_id=user.id, at=now)
                if self._hooks.on_invitation_redeemed is not None:
                    await self._hooks.on_invitation_redeemed(user, invitation)
                attached += 1
        return EmailChangeConfirmed(email=user.email, invitations_attached=attached)

    # --- verification --------------------------------------------------------------------

    async def _verification_link(self, user: UserT, now: datetime) -> str:
        raw, digest = mint()
        await self._tokens.put(
            OneTimeTokenKind.VERIFICATION,
            user_id=user.id,
            token_hash=digest,
            email=user.email,
            expires_at=now + VERIFY_TTL,
        )
        return self._policy.links.url(self._policy.links.verify_email, token=raw)

    async def verify_email(self, token: str, *, ip: str) -> EmailVerificationStatus:
        """``POST /auth/verify-email`` (§7, by POST: a mail scanner's GET verifies nothing).
        A link for an address the account no longer has is ``token_invalid``."""
        row, user = await self._redeem(OneTimeTokenKind.VERIFICATION, token, ip)
        if row.email != user.email:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        if user.email_verified_at is None:
            user = await self._users.apply(user, {"email_verified_at": self._now()})
        await self._tokens.drop(OneTimeTokenKind.VERIFICATION, user.id)
        return EmailVerificationStatus(email=user.email, email_verified=True)

    async def resend_verification(self, user: UserT) -> EmailVerificationStatus:
        """``POST /auth/resend-verification``: a verified address sends nothing and spends
        no budget; otherwise the recipient's window, a new link, the commit, the mail — and
        ``503 mail_not_sent`` when it could not go, so the banner says so. A demo's address
        is nobody's: answered, never mailed (landing-demo §6.4)."""
        if user.email_verified_at is not None:
            return EmailVerificationStatus(email=user.email, email_verified=True)
        if getattr(user, "is_demo", False):
            return EmailVerificationStatus(email=user.email, email_verified=False)
        charge(self._limiters.verification_resend, user.email, AUTH_THROTTLED["mail"])
        link = await self._verification_link(user, self._now())
        await self._after.commit_now()
        if not await self._send(
            user.email, AccountMailText.VERIFICATION, user.locale, link=link, name=self._name(user)
        ):
            raise AuthError(AuthErrorCode.MAIL_NOT_SENT)
        return EmailVerificationStatus(email=user.email, email_verified=False)

    # --- the reset ---------------------------------------------------------------------

    async def request_password_reset(self, body: PasswordResetRequest, *, ip: str) -> None:
        """``POST /auth/password-reset/request``: always ``204``, account or not (§4.5). The
        caller's window first, then the address's (a request the first refused spends
        nothing of the second). The address's tagged variant is tried too; the mail goes
        to the account's own address, after the commit. A deactivated account or a demo
        gets none."""
        charge(self._limiters.reset_ip, ip, AUTH_THROTTLED["reset"])
        charge(self._limiters.reset_address, body.email, AUTH_THROTTLED["reset"])
        addresses = addresses_for_reset(body.email, self._policy.app)
        found = {row.email: row for row in await self._users.by_emails(addresses)}
        user = next((found[address] for address in addresses if address in found), None)
        if user is None or not user.is_active or getattr(user, "is_demo", False):
            return
        raw, digest = mint()
        await self._tokens.put(
            OneTimeTokenKind.PASSWORD_RESET,
            user_id=user.id,
            token_hash=digest,
            email=user.email,
            expires_at=self._now() + RESET_TTL,
        )
        link = self._policy.links.url(self._policy.links.reset_password, token=raw)
        self._send_later(user.email, AccountMailText.PASSWORD_RESET, user.locale, link=link, name=self._name(user))

    async def _reset_account(self, token: str, ip: str) -> UserT:
        row, user = await self._redeem(OneTimeTokenKind.PASSWORD_RESET, token, ip)
        if not user.is_active or row.email != user.email:
            raise AuthError(AuthErrorCode.TOKEN_INVALID)
        return user

    async def check_password_reset(self, token: str, *, ip: str) -> tuple[UserT, dict[str, Any]]:
        """``POST /auth/password-reset/check``: is the link good, without spending it; the
        account and the ``reset_extras``."""
        user = await self._reset_account(token, ip)
        return user, await self._extras(self._hooks.reset_extras, user, False)

    async def confirm_password_reset(self, body: PasswordResetConfirm, *, ip: str) -> tuple[UserT, dict[str, Any]]:
        """``POST /auth/password-reset/confirm`` (§6.3, §12.5 item 6): the new password, the
        forced change met, the address verified (the link proved it), every session ended
        — and no tokens: a reset is not a sign-in, so a second factor cannot be skipped."""
        user = await self._reset_account(body.token, ip)
        now = self._now()
        changes: dict[str, Any] = {
            "password_hash": self._hasher.hash(body.new_password),
            "password_change_required_at": None,
            **self._cutoff(user, now),
        }
        if user.email_verified_at is None:
            changes["email_verified_at"] = now
        user = await self._users.apply(user, changes)
        await self._tokens.drop(OneTimeTokenKind.PASSWORD_RESET, user.id)
        await self._sessions.end_all(user.id)
        await self._sessions_ended(user, SessionsEndedReason.PASSWORD_RESET)
        return user, await self._extras(self._hooks.reset_extras, user, True)

    # --- invitations -------------------------------------------------------------------

    async def check_invitation(self, token: str, *, ip: str) -> tuple[InvT, bool, dict[str, Any]]:
        """``POST /auth/invitations/check`` (§4.4): the invitation without spending it,
        whether its address has an account, and the ``describe_invitation`` extras."""
        charge(self._limiters.redeem, ip, AUTH_THROTTLED["redeem"])
        invitation = await self._open_invitation(token)
        exists = await self._users.by_email(invitation.email) is not None
        return invitation, exists, await self._extras(self._hooks.describe_invitation, invitation)

    async def accept_invitation(self, user: UserT, token: str, *, ip: str) -> tuple[InvT, dict[str, Any]]:
        """``POST /auth/invitations/accept`` (§4.4): an existing account takes up an
        invitation for its address (or the app's tag on it); ``on_invitation_redeemed``
        attaches what it names."""
        refuse_demo(getattr(user, "is_demo", False), "invitations")
        charge(self._limiters.redeem, ip, AUTH_THROTTLED["redeem"])
        invitation = await self._open_invitation(token)
        if not invitation_accepts(invitation.email, user.email, self._policy.app):
            raise AuthError(AuthErrorCode.INVITATION_INVALID)
        await self._invitations.accept(invitation, user_id=user.id, at=self._now())
        if self._hooks.on_invitation_redeemed is not None:
            await self._hooks.on_invitation_redeemed(user, invitation)
        return invitation, await self._extras(self._hooks.describe_invitation, invitation)

    async def invitation_rows(self) -> list[dict[str, Any]]:
        """``GET /admin/invitations`` (user-admin §5): every invitation not accepted, newest
        first, as ``InvitationRow`` fields — the inviter as a person, the status from the
        columns, never a link. A row never sent (an allow-list entry carried over) reads as
        expired, so the admin re-sends it."""
        now = self._now()
        rows = await self._invitations.listing()
        people: dict[int, PersonRef | None] = {}
        for invitation in rows:
            inviter = invitation.invited_by_user_id
            if inviter is not None and inviter not in people:
                person = await self._users.get(inviter)
                people[inviter] = PersonRef.model_validate(person, from_attributes=True) if person is not None else None
        return [
            await self._invitation_fields(invitation, now, people.get(invitation.invited_by_user_id or 0))
            for invitation in rows
        ]

    async def _invitation_fields(
        self, invitation: InvT, now: datetime, inviter: PersonRef | None, *, link: str | None = None
    ) -> dict[str, Any]:
        expires_at = invitation.expires_at
        if expires_at is None:
            status, expires_at = InvitationStatus.EXPIRED, invitation.created_at
        else:
            status = invitation_status(
                expires_at=expires_at, now=now, accepted_at=invitation.accepted_at, revoked_at=invitation.revoked_at
            )
        return {
            "id": invitation.id,
            "email": invitation.email,
            "role": invitation.role,
            "note": invitation.note,
            "locale": invitation.locale,
            "invited_by": inviter,
            "created_at": invitation.created_at,
            "expires_at": expires_at,
            "status": status,
            "link": link,
            **(await self._extras(self._hooks.describe_invitation, invitation)),
        }

    async def _mail_invitation(self, invitation: InvT, raw: str) -> tuple[str, bool]:
        """Commit, then mail the invitation in the invitee's language."""
        await self._after.commit_now()
        link = self._policy.links.url(self._policy.links.register, invite=raw)
        return link, await self._send(invitation.email, AccountMailText.INVITATION, invitation.locale, link=link)

    async def _issued(self, admin: UserT, issued: IssuedInvitation[InvT], *, show_link: bool) -> dict[str, Any]:
        inviter = PersonRef.model_validate(admin, from_attributes=True)
        fields = await self._invitation_fields(
            issued.invitation, self._now(), inviter, link=issued.link if show_link else None
        )
        return {**fields, "sent": issued.sent}

    async def create_invitation(self, admin: UserT, body: InvitationCreate, *, show_link: bool) -> dict[str, Any]:
        """``POST /admin/invitations`` (user-admin §5): an invitation to an address, in the
        invitee's language, logged as ``invite``; the mail after the commit. ``show_link``:
        the console transport's shown-once link (§12.5 item 11). A ``scope`` goes through the
        ``invitation_scope`` hook, else it is refused."""
        invitable = self._policy.invitable_roles
        if invitable is not None and body.role not in invitable:
            raise AccountError(AccountErrorCode.ROLE_NOT_GRANTABLE)
        scope: Mapping[str, Any] | None = None
        if body.scope is not None:
            if self._hooks.invitation_scope is None:
                raise AuthError(AuthErrorCode.INVITATION_SCOPE_INVALID)
            scope = await self._hooks.invitation_scope(admin, body.scope)
        charge(self._limiters.verification_resend, body.email, AUTH_THROTTLED["mail"])
        now = self._now()
        raw, digest = mint()
        invitation = await self._invitations.add(
            NewInvitation(
                email=body.email,
                token_hash=digest,
                expires_at=now + INVITE_TTL,
                locale=body.locale,
                invited_by_user_id=admin.id,
                role=body.role,
                scope=scope,
                note=body.note,
            )
        )
        await self._record(AdminAction.INVITE, admin, invitation, now)
        link, sent = await self._mail_invitation(invitation, raw)
        return await self._issued(admin, IssuedInvitation(invitation, link, sent), show_link=show_link)

    async def _closed_or_found(self, invitation_id: int) -> InvT:
        invitation = await self._invitations.get(invitation_id)
        if invitation is None:
            raise AuthError(AuthErrorCode.INVITATION_NOT_FOUND)
        return invitation

    async def resend_invitation(self, admin: UserT, invitation_id: int, *, show_link: bool) -> dict[str, Any]:
        """``POST /admin/invitations/{id}/resend``: a new token and 14 more days, the old
        link dead (§4.4), logged as ``invite_resend``. Not for an accepted or revoked one."""
        invitation = await self._closed_or_found(invitation_id)
        if not _unspent(invitation):
            raise AuthError(AuthErrorCode.INVITATION_CLOSED)
        charge(self._limiters.verification_resend, invitation.email, AUTH_THROTTLED["mail"])
        now = self._now()
        raw, digest = mint()
        invitation = await self._invitations.remint(invitation, token_hash=digest, expires_at=now + INVITE_TTL)
        await self._record(AdminAction.INVITE_RESEND, admin, invitation, now)
        link, sent = await self._mail_invitation(invitation, raw)
        return await self._issued(admin, IssuedInvitation(invitation, link, sent), show_link=show_link)

    async def revoke_invitation(self, admin: UserT, invitation_id: int) -> None:
        """``DELETE /admin/invitations/{id}``: an open or expired invitation is withdrawn,
        logged as ``invite_revoke``; a revoked one stays as it is; an accepted one is
        ``invitation_closed``."""
        invitation = await self._closed_or_found(invitation_id)
        if invitation.accepted_at is not None:
            raise AuthError(AuthErrorCode.INVITATION_CLOSED)
        if invitation.revoked_at is not None:
            return
        now = self._now()
        await self._invitations.revoke(invitation, at=now)
        await self._record(AdminAction.INVITE_REVOKE, admin, invitation, now)

    async def _record(self, action: AdminAction, admin: UserT, invitation: InvT, now: datetime) -> None:
        detail = (
            {"invitation_id": invitation.id, "role": invitation.role}
            if invitation.role
            else {"invitation_id": invitation.id}
        )
        await self._log.add(
            admin_action_record(
                action, actor_id=admin.id, target_user_id=None, target_email=invitation.email, detail=detail, now=now
            )
        )

    # --- the admin's mails (0.30) ------------------------------------------------------

    async def send_account_mail(self, user: UserT, kind: MailKind) -> tuple[bool, str]:
        """``POST /admin/users/{id}/mail`` (user-admin §4.1): the verification mail or a
        password reset, sent again by an admin, through the public flows' links and their
        lifetimes. Charged to the recipient's window first, as the public flows are
        (``verification_resend``, ``reset_address``; Kurvenschmiede): an admin's button is no
        faster way to fill an inbox. The link is minted, committed (``commit_now``), then
        mailed in the account's language. Whether it went, and the link — the router shows it
        only while mail goes to the console. The admin's refusals (a deactivated account, a
        verified address) are the router's, before this."""
        now = self._now()
        if kind is MailKind.VERIFICATION:
            charge(self._limiters.verification_resend, user.email, AUTH_THROTTLED["mail"])
            link, text = await self._verification_link(user, now), AccountMailText.VERIFICATION
        else:
            charge(self._limiters.reset_address, user.email, AUTH_THROTTLED["mail"])
            raw, digest = mint()
            await self._tokens.put(
                OneTimeTokenKind.PASSWORD_RESET,
                user_id=user.id,
                token_hash=digest,
                email=user.email,
                expires_at=now + RESET_TTL,
            )
            link = self._policy.links.url(self._policy.links.reset_password, token=raw)
            text = AccountMailText.PASSWORD_RESET
        await self._after.commit_now()
        return await self._send(user.email, text, user.locale, link=link, name=self._name(user)), link

    # --- the erasure (0.30) ------------------------------------------------------------

    async def erase(
        self, user: UserT, *, actor_id: int | None, feedback: FeedbackService[Any] | None = None
    ) -> dict[str, Any]:
        """Stage two of a deletion (user-admin §6.4), in the caller's transaction: the
        operator's ``POST /admin/users/{id}/erase`` (``actor_id`` the admin) and the
        ``after_days`` job (:meth:`erase_if_due`, no actor). The caller has checked that the
        account asked for it. In order:

        1. the ``erase`` hook: the app's own rows (what an erasure deletes or hands over is
           the app's);
        2. with ``feedback`` (the app's ``FeedbackService``), the person leaves their reports,
           comments, votes and replies (``erase_author`` with ``erasure_identifiers``); the
           files no other row names go to ``purge_files`` after the commit;
        3. the invitations the account accepted are deleted, where the store can
           (:class:`~eifi1_server_kit.user_admin.ports.AcceptedInvitations`);
        4. the admin log keeps its rows and loses the addresses: the account's and those
           invitations' (``scrub_email``); the account's id goes with the row's foreign key;
        5. every session row ends and every link is dropped, for a table that does not
           cascade;
        6. an ``erase`` row: the actor, no target, ``{feedback_anonymised, **the hook's}``;
        7. the account's row is deleted.

        Answers the row's detail."""
        email = user.email
        extras = await self._extras(self._hooks.erase, user)
        anonymised = 0
        if feedback is not None:
            erased = await feedback.erase_author(user.id, erasure_identifiers(email, user.first_name, user.last_name))
            anonymised = erased.reports
            self._purge_later(erased.keys)
        addresses = {email}
        if isinstance(self._invitations, AcceptedInvitations):
            addresses.update(await self._invitations.forget_accepted(user.id))
        for address in sorted(addresses):
            await self._log.scrub_email(address)
        await self._sessions.end_all(user.id)
        for kind in (OneTimeTokenKind.PASSWORD_RESET, OneTimeTokenKind.VERIFICATION, OneTimeTokenKind.EMAIL_CHANGE):
            await self._tokens.drop(kind, user.id)
        detail = {"feedback_anonymised": anonymised, **extras}
        await self._log.add(
            admin_action_record(
                AdminAction.ERASE,
                actor_id=actor_id,
                target_user_id=None,
                target_email=None,
                detail=detail,
                now=self._now(),
            )
        )
        await self._users.delete(user)
        logger.info("erased account %s (%s feedback reports anonymised)", user.id, anonymised)
        return detail

    def _purge_later(self, keys: Sequence[str]) -> None:
        if not keys:
            return
        purge = self._hooks.purge_files
        if purge is None:
            logger.info("an erasure left %s stored files and the app takes no purge_files", len(keys))
            return
        kept = tuple(keys)

        async def run() -> None:
            await purge(kept)

        self._after.later(run)

    async def erase_if_due(self, user_id: int, *, feedback: FeedbackService[Any] | None = None) -> bool:
        """The ``after_days`` job's one account (user-admin §6.4), in its own transaction: the
        row re-read, and erased (:meth:`erase`, no actor) only when it is deactivated, asked
        for its deletion and is due (``deletion_due``, the mail's promised date included).
        ``False`` for anything else — gone, reactivated, not yet due — and always in
        ``operator`` mode, where an operator erases. Idempotent: the app's job may run it
        twice."""
        if self._policy.deletion_mode is not DeletionMode.AFTER_DAYS:
            return False
        user = await self._users.get(user_id)
        if user is None or user.is_active or user.deletion_requested_at is None:
            return False
        if not deletion_due(
            user.deletion_requested_at,
            self._policy.deletion_days,
            self._now(),
            scheduled_at=user.deletion_scheduled_at,
        ):
            return False
        await self.erase(user, actor_id=None, feedback=feedback)
        return True

    # --- deletion and export -----------------------------------------------------------

    async def deletion_terms(self, user: UserT) -> dict[str, Any]:
        """``GET /auth/me/deletion``: the mode, the days (none in ``operator`` mode), and the
        ``deletion_terms`` extras."""
        mode = self._policy.deletion_mode
        days = self._policy.deletion_days if mode is DeletionMode.AFTER_DAYS else None
        return {"mode": str(mode), "days": days, **(await self._extras(self._hooks.deletion_terms, user))}

    async def request_deletion(self, user: UserT, password: str, confirm_email: str) -> dict[str, Any]:
        """``POST /auth/me/deletion`` (user-admin §6.4): the password (``password_incorrect``)
        and the typed address (``confirmation_required`` / ``_mismatch``); never the last
        active admin (``last_admin``). The account is deactivated at once, every session
        ends, the request is logged with the account as its actor; the confirming mail goes
        after the commit, and the answer says whether it did."""
        refuse_demo(getattr(user, "is_demo", False), "deleting the account")
        await self._check_password(user, password)
        require_confirmation(
            confirmation_level(AdminAction.DELETION_REQUEST), target_email=user.email, confirm_email=confirm_email
        )
        if user.role == self._policy.admin_role:
            refuse_last_admin(
                target_is_admin=True,
                active_admins=await self._users.count_active(role=self._policy.admin_role),
                change_removes_admin=True,
            )
        now = self._now()
        scheduled = deletion_schedule(now, self._policy.deletion_mode, self._policy.deletion_days)
        user = await self._users.apply(
            user,
            {
                "is_active": False,
                "deletion_requested_at": now,
                "deletion_scheduled_at": scheduled,
                **self._cutoff(user, now),
            },
        )
        await self._sessions.end_all(user.id)
        await self._sessions_ended(user, SessionsEndedReason.DELETION_REQUESTED)
        await self._log.add(
            admin_action_record(
                AdminAction.DELETION_REQUEST, actor_id=user.id, target_user_id=user.id, target_email=user.email, now=now
            )
        )
        extras = await self._extras(self._hooks.on_deletion_requested, user)
        await self._after.commit_now()
        values: dict[str, Any] = {"name": self._name(user), "contact": self._policy.contact, **self._policy.retention}
        if scheduled is None:
            kind = AccountMailText.ACCOUNT_DELETION_OPERATOR
        else:
            kind, values["date"] = AccountMailText.ACCOUNT_DELETION, mail_date(scheduled, user.locale)
        link = self._policy.links.url(self._policy.links.home)
        sent = await self._send(user.email, kind, user.locale, link=link, **values)
        return {"deletion_scheduled_at": scheduled, "mail_sent": sent, **extras}

    async def withdraw_deletion(self, user: UserT, *, actor_id: int) -> UserT:
        """An admin's reactivation before the erasure (user-admin §6.4; 0.30's admin route):
        both stamps cleared, the account active, ``deletion_cancel`` logged, the hook told."""
        now = self._now()
        user = await self._users.apply(
            user, {"is_active": True, "deletion_requested_at": None, "deletion_scheduled_at": None}
        )
        await self._log.add(
            admin_action_record(
                AdminAction.DELETION_CANCEL, actor_id=actor_id, target_user_id=user.id, target_email=user.email, now=now
            )
        )
        if self._hooks.on_deletion_withdrawn is not None:
            await self._hooks.on_deletion_withdrawn(user)
        return user

    async def export(self, user: UserT) -> dict[str, Any]:
        """``GET /auth/me/export`` (user-admin §6.5): the kit's envelope, the account as the
        kit knows it — the profile, the role, the dates, the sessions as device and dates,
        the passkeys as name and dates, never a secret — and the app's ``data``."""
        refuse_demo(getattr(user, "is_demo", False), "the account export")
        charge(self._limiters.export, str(user.id), AUTH_THROTTLED["export"])
        now = self._now()
        sessions = await self._sessions.live(user.id)
        passkeys = await self._passkeys.for_user(user.id) if self._passkeys is not None else []
        account = {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "locale": user.locale,
            "role": user.role,
            "email_verified": user.email_verified_at is not None,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "text_size": user.text_size,
            "contrast": user.contrast,
            "device_timezone": user.device_timezone,
            "sessions": [
                {"device": row.device, "started_at": row.started_at, "last_active": row.last_active} for row in sessions
            ],
            "passkeys": [
                {"name": row.name, "created_at": row.created_at, "last_used_at": row.last_used_at} for row in passkeys
            ],
        }
        data = await self._extras(self._hooks.export_data, user)
        logger.info("account export for user %s", user.id)
        return export_envelope(self._policy.app, account, data, now)

    # --- passkeys ----------------------------------------------------------------------

    def _passkey_parts(self) -> tuple[PasskeyStore[Any], RelyingParty]:
        if self._passkeys is None or self._rp is None:
            raise RuntimeError("passkeys need a PasskeyStore and a RelyingParty")
        return self._passkeys, self._rp

    def _ceremony_token(self, sub: int | str, kind: ChallengeKind, challenge: bytes) -> str:
        return self._signer.sign(
            challenge_claims(sub, kind, now=self._now(), extra={"challenge": base64url_encode(challenge)})
        )

    async def _spend(self, store: PasskeyStore[Any], challenge: str, kind: ChallengeKind) -> bool:
        return await store.spend_challenge(hash_token(challenge), expires_at=self._now() + CHALLENGE_LIFETIMES[kind])

    async def begin_passkey_registration(self, user: UserT, body: PasskeyRegistrationBegin) -> PasskeyOptions:
        """``POST /auth/passkey/register/begin``: the current password first (a passkey
        outlives every sign-out; Kurvenschmiede), then the options and the challenge token."""
        store, rp = self._passkey_parts()
        refuse_demo(getattr(user, "is_demo", False), "passkeys")
        await self._check_password(user, body.current_password)
        existing = await store.for_user(user.id)
        extensions = await self._extras(self._hooks.passkey_extensions, user, "registration")
        options, challenge = registration_options(
            rp,
            user_id=user.id,
            user_name=user.email,
            user_display_name=self._name(user),
            exclude=[row.credential_id for row in existing],
            extensions=extensions,
        )
        token = self._ceremony_token(user.id, ChallengeKind.WEBAUTHN_REGISTER, challenge)
        return PasskeyOptions(options=options, challenge_token=token)

    async def finish_passkey_registration(self, user: UserT, body: PasskeyRegistrationFinish) -> PasskeyRow:
        """``POST /auth/passkey/register/finish``: the answer verified (user verification
        required), the challenge spent once, the credential new to the app; the account's
        address is told after the commit (keksdose's notice). Every refusal is ``400
        passkey_registration_refused``: the caller is signed in."""
        store, rp = self._passkey_parts()
        refuse_demo(getattr(user, "is_demo", False), "passkeys")
        claims = self._signer.verify(body.challenge_token)
        challenge = claims.get("challenge") if claims is not None else None
        if (
            claims is None
            or claims.get("type") != ChallengeKind.WEBAUTHN_REGISTER
            or claims.get("sub") != str(user.id)
            or not isinstance(challenge, str)
        ):
            raise AuthError(AuthErrorCode.PASSKEY_REGISTRATION_REFUSED, "Not a registration challenge for this account")
        try:
            verified = verify_registration(rp, body.credential, base64url_decode(challenge))
        except (PasskeyCheckError, ValueError) as exc:
            logger.info("Passkey registration refused: %s", exc)
            raise AuthError(AuthErrorCode.PASSKEY_REGISTRATION_REFUSED) from exc
        if not await self._spend(store, challenge, ChallengeKind.WEBAUTHN_REGISTER):
            raise AuthError(AuthErrorCode.PASSKEY_REGISTRATION_REFUSED, "This challenge has already been answered")
        if await store.by_credential_id(verified.credential_id) is not None:
            raise AuthError(AuthErrorCode.PASSKEY_REGISTRATION_REFUSED, "This passkey is already registered")
        name = body.name or DEFAULT_PASSKEY_NAME
        row: PasskeyRow = await store.add(
            NewPasskey(
                user_id=user.id,
                credential_id=verified.credential_id,
                public_key=verified.public_key,
                sign_count=verified.sign_count,
                name=name,
                transports=verified.transports,
            )
        )
        self._send_later(
            user.email,
            AccountMailText.PASSKEY_ADDED_NOTICE,
            user.locale,
            link=self._policy.links.url(self._policy.links.settings),
            name=self._name(user),
            passkey=name,
            contact=self._policy.contact,
        )
        return row

    async def begin_passkey_login(self) -> PasskeyOptions:
        """``POST /auth/passkey/login/begin``: options with no allow-list, and the token.
        Reads nothing and writes nothing, so it tells nobody anything."""
        _, rp = self._passkey_parts()
        extensions = await self._extras(self._hooks.passkey_extensions, None, "authentication")
        options, challenge = authentication_options(rp, extensions=extensions)
        return PasskeyOptions(
            options=options, challenge_token=self._ceremony_token(_NOBODY, ChallengeKind.WEBAUTHN_AUTH, challenge)
        )

    async def finish_passkey_login(
        self, body: PasskeyLoginFinish, *, ip: str, device: str | None = None
    ) -> IssuedSession[UserT] | ChallengeIssued[UserT]:
        """``POST /auth/passkey/login/finish``: the sign-in's limit, then one ``401
        passkey_refused`` for every reason (the reason to the log). A verified answer spends
        its challenge once (after the signature, so only a genuine answer learns it is
        spent; keksdose's order), and the sign-in answers as the password one does — a
        forced change caught too. User verification makes it both factors (§12.5 item 8)."""
        store, rp = self._passkey_parts()
        charge(self._limiters.login, ip, AUTH_THROTTLED["login"])
        claims = self._signer.verify(body.challenge_token)
        challenge = claims.get("challenge") if claims is not None else None
        try:
            if claims is None or claims.get("type") != ChallengeKind.WEBAUTHN_AUTH or not isinstance(challenge, str):
                raise PasskeyCheckError("not a sign-in challenge")
            stored = await store.by_credential_id(credential_id_of(body.credential))
            if stored is None:
                raise PasskeyCheckError("no such passkey")
            handle = user_handle_of(body.credential)
            if handle is not None and handle != stored.user_id:
                raise PasskeyCheckError("the user handle names another account")
            count = verify_authentication(
                rp,
                body.credential,
                base64url_decode(challenge),
                public_key=stored.public_key,
                sign_count=stored.sign_count,
            )
            if not await self._spend(store, challenge, ChallengeKind.WEBAUTHN_AUTH):
                raise PasskeyCheckError("the challenge has already been answered")
            user = await self._users.get(stored.user_id)
            if user is None or not user.is_active:
                raise PasskeyCheckError("the account is not active")
        except (PasskeyCheckError, ValueError) as exc:
            logger.info(_REFUSED, exc)
            raise AuthError(AuthErrorCode.PASSKEY_REFUSED) from exc
        await store.used(stored, sign_count=count, at=self._now())
        return await self._sign_in(user, shared=body.shared_device, ip=ip, device=device)

    async def passkeys_of(self, user: UserT) -> Sequence[PasskeyRow]:
        """``GET /auth/passkeys``: the account's own, oldest first."""
        store, _ = self._passkey_parts()
        return await store.for_user(user.id)

    async def _own_passkey(self, store: PasskeyStore[Any], user: UserT, passkey_id: int) -> PasskeyRow:
        for row in await store.for_user(user.id):
            if row.id == passkey_id:
                return cast(PasskeyRow, row)
        raise AuthError(AuthErrorCode.PASSKEY_NOT_FOUND)

    async def rename_passkey(self, user: UserT, passkey_id: int, name: str) -> PasskeyRow:
        """``PATCH /auth/passkeys/{id}``: another account's is not found either."""
        store, _ = self._passkey_parts()
        refuse_demo(getattr(user, "is_demo", False), "passkeys")
        await store.rename(await self._own_passkey(store, user, passkey_id), name)
        return await self._own_passkey(store, user, passkey_id)

    async def remove_passkey(self, user: UserT, passkey_id: int) -> None:
        """``DELETE /auth/passkeys/{id}``: the last one included (the password stays), unless
        ``passkey_removing`` refuses (keksdose's last unlock path)."""
        store, _ = self._passkey_parts()
        refuse_demo(getattr(user, "is_demo", False), "passkeys")
        passkey = await self._own_passkey(store, user, passkey_id)
        if self._hooks.passkey_removing is not None:
            await self._hooks.passkey_removing(user, passkey)
        await store.remove(passkey)

    # --- mail ----------------------------------------------------------------------------

    def _name(self, user: UserT) -> str:
        """The recipient's name in their own language (§3.2: the recipient's, in a mail)."""
        return full_name(user.first_name, user.last_name, user.locale)

    def _message(self, kind: AccountMailText, locale: str | None, *, link: str, **values: Any) -> MailMessage:
        return render_mail(locale, self._texts[kind], link=link, app=self._policy.app_name, **values)

    async def _send(self, to: str, kind: AccountMailText, locale: str | None, *, link: str, **values: Any) -> bool:
        """Render and send now: the caller has committed (``commit_now``)."""
        return await self._mailer.send(
            to, self._message(kind, locale, link=link, **values), kind=ACCOUNT_MAIL_KINDS[kind]
        )

    def _send_later(self, to: str, kind: AccountMailText, locale: str | None, *, link: str, **values: Any) -> None:
        """Render now (a fault shows in the request), send after the app's commit."""
        message = self._message(kind, locale, link=link, **values)
        mailer, sent_as = self._mailer, ACCOUNT_MAIL_KINDS[kind]

        async def send() -> None:
            await mailer.send(to, message, kind=sent_as)

        self._after.later(send)
