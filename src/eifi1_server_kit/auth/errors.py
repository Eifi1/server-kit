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
    #: A reset or verification link that is unknown, spent or expired.
    TOKEN_INVALID = "token_invalid"


#: Each code's status. The token codes are ``400`` as keksdose answers a bad reset or
#: verification link today; the page tells them apart by ``code``, not by status.
AUTH_ERROR_STATUS: Mapping[AuthErrorCode, int] = {
    AuthErrorCode.INVALID_CREDENTIALS: 401,
    AuthErrorCode.REGISTRATION_CLOSED: 403,
    AuthErrorCode.EMAIL_TAKEN: 409,
    AuthErrorCode.INVITATION_INVALID: 400,
    AuthErrorCode.INVITATION_EXPIRED: 400,
    AuthErrorCode.TOKEN_INVALID: 400,
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
}


class AuthError(ValueError):
    """A coded refusal: ``status_code`` and ``code`` from :class:`AuthErrorCode`.

    ``AuthError(AuthErrorCode.EMAIL_TAKEN)`` is a 409 with the code's English detail; pass
    ``detail`` to say more. A ``ValueError`` like every kit refusal, and registered in
    :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`, so an app-wide ``ValueError`` → 400
    handler does not swallow its status.
    """

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 400
    code: AuthErrorCode

    def __init__(self, code: AuthErrorCode, detail: str | None = None) -> None:
        self.code = AuthErrorCode(code)
        self.status_code = AUTH_ERROR_STATUS[self.code]
        super().__init__(detail or AUTH_ERROR_DETAIL[self.code])
