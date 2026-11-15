"""The account layer's decisions, as an app counts them (0.32, keksdose's review of 0.32.0.dev4).

The kit's answers fold several reasons into one code on purpose (``invalid_credentials`` for
an unknown address, a wrong password and a deactivated account), and an app's hooks never
see a refusal, so an app could no longer count its sign-in funnel (keksdose's
``keksdose_auth_events_total(event, outcome)`` and its Grafana panel). The account service
hands each decision of a signed-out flow and of the refresh to
``AuthHooks.observe(event, outcome)``: an :class:`AuthEvent`, and a short stable string —
``ok``, the internal reason where the answer is one code for several, otherwise the
refusal's own code — never an address, an id or a token. The README's table lists every
event's outcomes.
"""

from __future__ import annotations

import enum

__all__ = ["AuthEvent"]


class AuthEvent(enum.StrEnum):
    """The flow ``AuthHooks.observe`` is told a decision of. A ``StrEnum`` with stable values,
    so ``str(event)`` is the metric's label."""

    #: ``POST /auth/register``.
    REGISTER = "register"
    #: ``POST /auth/login``: the password sign-in.
    LOGIN = "login"
    #: ``POST /auth/login/2fa``: the code step, an authenticator's code or a backup code.
    LOGIN_2FA = "login_2fa"
    #: ``POST /auth/login/set-password``: the forced change an admin required.
    LOGIN_SET_PASSWORD = "login_set_password"
    #: ``POST /auth/passkey/login/finish``.
    PASSKEY_LOGIN = "passkey_login"
    #: ``POST /auth/refresh``.
    REFRESH = "refresh"
    #: ``POST /auth/password-reset/request``.
    PASSWORD_RESET_REQUEST = "password_reset_request"
    #: ``POST /auth/password-reset/check``: the link, not spent.
    PASSWORD_RESET_CHECK = "password_reset_check"
    #: ``POST /auth/password-reset/confirm``.
    PASSWORD_RESET_CONFIRM = "password_reset_confirm"
    #: ``POST /auth/verify-email``.
    EMAIL_VERIFY = "email_verify"
    #: ``POST /auth/me/email/confirm``: an address change's link.
    EMAIL_CHANGE_CONFIRM = "email_change_confirm"
    #: ``POST /auth/invitations/check``: the invitation's link, not spent.
    INVITATION_CHECK = "invitation_check"
    #: ``POST /auth/invitations/accept``: an existing account takes it up.
    INVITATION_ACCEPT = "invitation_accept"
