"""The coded refusals of sign-in, sign-up and the one-time links (§5.2).

Every refusal carries a ``code`` the pages switch on — the kit's ``isAuthError(err,
code)`` — instead of matching the server's English ``detail``. Raise :class:`AuthError`
and :func:`eifi1_server_kit.errors.install_contract_error_handlers` answers it as
``{"detail": …, "code": …}`` at the code's status; an app that maps it itself reads
``exc.status_code`` and ``exc.code``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping


class AuthErrorCode(enum.StrEnum):
    """The ``code`` of a refusal (§5.2). A ``StrEnum``, so ``str(code)`` is the wire value."""

    #: An unknown address, a wrong password and a deactivated account alike — one
    #: answer, in the same time (§5.2, §2.10).
    INVALID_CREDENTIALS = "invalid_credentials"
    #: Neither the first user nor invited (§4.3).
    REGISTRATION_CLOSED = "registration_closed"
    #: An account has this address already.
    EMAIL_TAKEN = "email_taken"
    #: The invitation token is unknown, spent, or not for this address.
    INVITATION_INVALID = "invitation_invalid"
    #: The invitation has run out; "resend" mints a new one (§4.4).
    INVITATION_EXPIRED = "invitation_expired"
    #: A one-time link (reset, verification, email change) that is unknown or spent — and
    #: expired, for an app that does not tell expiry apart.
    TOKEN_INVALID = "token_invalid"
    #: A one-time link that WAS valid and ran out: the page says "expired, ask for a new
    #: one" instead of "invalid" (ui-kit 0.30.0; keksdose's verification and reset steps
    #: tell the two apart). Only the link's holder can learn it existed, so it is no
    #: oracle.
    TOKEN_EXPIRED = "token_expired"
    #: Every definitive refusal of ``POST /auth/refresh`` (auth §11.2, decision 7), and since
    #: 0.29 a signed-in request whose session is over (the account layer's ``current_user``):
    #: the
    #: refresh token is unknown, malformed, expired or no refresh token at all; the
    #: cut-off or the session row revoked it, or the session ran past its limits
    #: (``session_expired``, 0.27); the account is deactivated or must choose a
    #: new password. The session is over, and ui-kit's ``attachSessionRefresh`` signs the
    #: user out. The client decides by the STATUS — what may answer it, and what must not,
    #: is :mod:`~eifi1_server_kit.auth.tokens`' "Refreshing a session".
    SESSION_ENDED = "session_ended"
    # -- 0.29: the account layer (§12.5 item 3: a code on every refusal) -------------------
    #: A signed-in route asked without a usable bearer token: none, unreadable, not this
    #: app's, expired, or not an access token. ``401``: the client renews the session and
    #: asks again (§11.3).
    NOT_SIGNED_IN = "not_signed_in"
    #: A signed-in admin route asked by an account that is no admin.
    ADMIN_REQUIRED = "admin_required"
    #: A new password that is the current one (keksdose, Kurvenschmiede: "New password must
    #: be different"), at a change or at the forced change of the sign-in.
    PASSWORD_UNCHANGED = "password_unchanged"
    #: The sign-in's set-password step for an account that no longer has to change its
    #: password: already done, or an admin withdrew the requirement (keksdose).
    PASSWORD_CHANGE_NOT_REQUIRED = "password_change_not_required"
    #: An address change to the address the account already has.
    EMAIL_UNCHANGED = "email_unchanged"
    #: A re-send of an address change when none is pending (keksdose's 404).
    EMAIL_CHANGE_NOT_PENDING = "email_change_not_pending"
    #: A mail the answer depends on could not be sent (the verification's "Send again").
    MAIL_NOT_SENT = "mail_not_sent"
    #: A passkey sign-in refused, whatever the reason: an unknown credential, a bad
    #: signature, a spent or foreign challenge, no user verification, an inactive account.
    #: One answer, like ``invalid_credentials``; the reason goes to the log.
    PASSKEY_REFUSED = "passkey_refused"
    #: A passkey the signed-in account tried to add was refused: a bad or spent ceremony,
    #: no user verification, or a credential already registered. ``400``, never ``401``.
    PASSKEY_REGISTRATION_REFUSED = "passkey_registration_refused"
    #: No such passkey among the caller's own (another account's is not found either).
    PASSKEY_NOT_FOUND = "passkey_not_found"
    #: No such session among the caller's own.
    SESSION_NOT_FOUND = "session_not_found"
    #: No such invitation (the admin's routes).
    INVITATION_NOT_FOUND = "invitation_not_found"
    #: The invitation was accepted or revoked: it can be neither re-sent nor revoked again.
    INVITATION_CLOSED = "invitation_closed"
    #: An invitation names a scope this app does not take, or one the admin may not invite into.
    INVITATION_SCOPE_INVALID = "invitation_scope_invalid"


#: Each code's status. The token codes are ``400`` as keksdose answers a bad reset or
#: verification link today; the page tells them apart by ``code``, not by status. A
#: refresh's refusal is ``401``, never ``403``: the three apps' refresh routes already
#: answer so (auth §11.1).
AUTH_ERROR_STATUS: Mapping[AuthErrorCode, int] = {
    AuthErrorCode.INVALID_CREDENTIALS: 401,
    AuthErrorCode.REGISTRATION_CLOSED: 403,
    AuthErrorCode.EMAIL_TAKEN: 409,
    AuthErrorCode.INVITATION_INVALID: 400,
    AuthErrorCode.INVITATION_EXPIRED: 400,
    AuthErrorCode.TOKEN_INVALID: 400,
    AuthErrorCode.TOKEN_EXPIRED: 400,
    AuthErrorCode.SESSION_ENDED: 401,
    AuthErrorCode.NOT_SIGNED_IN: 401,
    AuthErrorCode.ADMIN_REQUIRED: 403,
    AuthErrorCode.PASSWORD_UNCHANGED: 400,
    AuthErrorCode.PASSWORD_CHANGE_NOT_REQUIRED: 409,
    AuthErrorCode.EMAIL_UNCHANGED: 400,
    AuthErrorCode.EMAIL_CHANGE_NOT_PENDING: 404,
    AuthErrorCode.MAIL_NOT_SENT: 503,
    AuthErrorCode.PASSKEY_REFUSED: 401,
    AuthErrorCode.PASSKEY_REGISTRATION_REFUSED: 400,
    AuthErrorCode.PASSKEY_NOT_FOUND: 404,
    AuthErrorCode.SESSION_NOT_FOUND: 404,
    AuthErrorCode.INVITATION_NOT_FOUND: 404,
    AuthErrorCode.INVITATION_CLOSED: 409,
    AuthErrorCode.INVITATION_SCOPE_INVALID: 400,
}

#: The English ``detail`` of each code, keksdose's words where it had them. The pages
#: show the kit's own text for the code; this is for logs and API clients.
AUTH_ERROR_DETAIL: Mapping[AuthErrorCode, str] = {
    AuthErrorCode.INVALID_CREDENTIALS: "Invalid credentials",
    AuthErrorCode.REGISTRATION_CLOSED: "Registration is by invitation only",
    AuthErrorCode.EMAIL_TAKEN: "Email already registered",
    AuthErrorCode.INVITATION_INVALID: "This invitation is not valid",
    AuthErrorCode.INVITATION_EXPIRED: "This invitation has expired",
    AuthErrorCode.TOKEN_INVALID: "This link is invalid or has expired",
    AuthErrorCode.TOKEN_EXPIRED: "This link has expired",
    # Kurvenschmiede's ``TokenRefusal.ENDED`` (``auth_service.py:187``).
    AuthErrorCode.SESSION_ENDED: "This session has ended. Please sign in again.",
    AuthErrorCode.NOT_SIGNED_IN: "Not signed in",
    AuthErrorCode.ADMIN_REQUIRED: "Insufficient role",
    AuthErrorCode.PASSWORD_UNCHANGED: "New password must be different",
    AuthErrorCode.PASSWORD_CHANGE_NOT_REQUIRED: "This account no longer has to change its password. Please sign in normally.",
    AuthErrorCode.EMAIL_UNCHANGED: "This is already the account's address",
    AuthErrorCode.EMAIL_CHANGE_NOT_PENDING: "No address change is pending",
    AuthErrorCode.MAIL_NOT_SENT: "The mail could not be sent. Please try again later.",
    AuthErrorCode.PASSKEY_REFUSED: "That passkey could not sign you in.",
    AuthErrorCode.PASSKEY_REGISTRATION_REFUSED: "The passkey could not be added.",
    AuthErrorCode.PASSKEY_NOT_FOUND: "Passkey not found",
    AuthErrorCode.SESSION_NOT_FOUND: "Session not found",
    AuthErrorCode.INVITATION_NOT_FOUND: "Invitation not found",
    AuthErrorCode.INVITATION_CLOSED: "This invitation was accepted or revoked",
    AuthErrorCode.INVITATION_SCOPE_INVALID: "This invitation's scope is not allowed",
}


class AuthError(ValueError):
    """A coded refusal: ``status_code`` and ``code`` from :class:`AuthErrorCode`.

    ``AuthError(AuthErrorCode.EMAIL_TAKEN)`` is a 409 with the code's English detail; pass
    ``detail`` to say more. ``headers`` (0.29) go on the answer: a bearer refusal's
    ``WWW-Authenticate: Bearer`` (RFC 6750). A ``ValueError`` like every kit refusal, and registered in
    :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`, so an app-wide ``ValueError`` → 400
    handler does not swallow its status.
    """

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 400
    code: AuthErrorCode
    #: Extra answer headers, read by :func:`~eifi1_server_kit.errors.contract_error_response`.
    headers: Mapping[str, str] | None

    def __init__(
        self, code: AuthErrorCode, detail: str | None = None, *, headers: Mapping[str, str] | None = None
    ) -> None:
        self.code = AuthErrorCode(code)
        self.status_code = AUTH_ERROR_STATUS[self.code]
        self.headers = dict(headers) if headers else None
        super().__init__(detail or AUTH_ERROR_DETAIL[self.code])
