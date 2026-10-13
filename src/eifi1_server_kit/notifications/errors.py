"""The notifications' coded refusals (§3.2, §3.5, §3.6).

The same shape as the account's and billing's: a ``status_code`` and a ``code`` the page
switches on, answered ``{"detail": …, "code": …}`` by
:func:`~eifi1_server_kit.errors.install_contract_error_handlers`, where
:class:`NotificationError` is in :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`. A
:class:`ValueError` like every kit refusal, so an app-wide ``ValueError`` → 400 handler
cannot swallow its status.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = [
    "NotificationError",
    "NotificationTypeError",
    "PushEndpointTakenError",
    "UnsubscribeTokenError",
]


class NotificationError(ValueError):
    """Base class of every notifications refusal; each subclass sets ``status_code``,
    ``code`` and its English ``detail`` (for logs and API clients — the kit's pages show
    their own words for the code). Pass ``detail`` to say more."""

    status_code: int = 422
    code: str = "notification_invalid"
    default_detail: str = "This notification request is not valid"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.default_detail)


class NotificationTypeError(NotificationError):
    """A ``PATCH /notifications/settings`` naming a type this account doesn't receive (§3.2
    rule 4): 422 ``{"detail": …, "code": "notification_type_not_received", "types":
    [...]}``. Raised by :func:`~.resource.apply_switches_patch` before anything is written;
    a type no registry knows is refused earlier, by the update model."""

    status_code = 422
    code = "notification_type_not_received"
    default_detail = "This account doesn't receive these notifications"
    extra: Mapping[str, object]

    def __init__(self, type_ids: Iterable[str], detail: str | None = None) -> None:
        self.type_ids = tuple(type_ids)
        self.extra = {"types": list(self.type_ids)}
        super().__init__(detail or f"{self.default_detail}: {', '.join(self.type_ids)}")


class PushEndpointTakenError(NotificationError):
    """``POST /notifications/devices`` with an endpoint another account holds (§3.5): 409.

    Refused, never moved to this account: the server can't prove the caller holds the
    endpoint, and moving it would let one account redirect another's notifications. The
    client unsubscribes and registers a fresh endpoint (keksdose
    ``domain/services/push_service.py:62-153``).
    """

    status_code = 409
    code = "push_endpoint_taken"
    default_detail = (
        "This browser's push endpoint is registered to another account. "
        "Turn notifications off and on again to register a fresh one."
    )


class UnsubscribeTokenError(NotificationError):
    """An unsubscribe link whose token doesn't verify — tampered, cut, signed with another
    secret, or of another version (§3.6): 400. A valid token always answers 200, also for
    a switch already off or an account that is gone, so the answer reveals nothing."""

    status_code = 400
    code = "unsubscribe_invalid"
    default_detail = "This unsubscribe link is not valid"
