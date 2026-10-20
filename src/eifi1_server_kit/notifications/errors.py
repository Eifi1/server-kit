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
    "NotificationChannelError",
    "NotificationError",
    "NotificationTypeError",
    "PushBodyInvalidError",
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


class NotificationChannelError(NotificationError):
    """A switch for a channel the type doesn't offer (§10.2, 0.16) — ``push`` in an app with
    no push, a type id a device can't mute: 422 ``{"detail": …, "code":
    "notification_channel_not_offered", "types": [...], "channel": "push"}``. The settings
    model refuses the PATCH's switch first; :func:`~.resource.apply_switches_patch` and
    :func:`~.delivery.apply_muted_types` check it again for a caller without it."""

    status_code = 422
    code = "notification_channel_not_offered"
    default_detail = "These notifications don't offer this channel"
    extra: Mapping[str, object]

    def __init__(self, type_ids: Iterable[str], channel: str, detail: str | None = None) -> None:
        self.type_ids = tuple(type_ids)
        self.channel = channel
        self.extra = {"types": list(self.type_ids), "channel": channel}
        super().__init__(detail or f"{self.default_detail} ({channel}): {', '.join(self.type_ids)}")


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


class PushBodyInvalidError(NotificationError):
    """A push route's body that is no valid model (0.16.1, keksdose S1): 422 ``{"detail":
    …, "code": "push_body_invalid", "fields": ["old_auth", …]}``. Raised by
    :func:`~.push.parse_push_body` and :func:`~.push.parse_push_replace` in place of
    FastAPI's own 422, whose ``input`` would echo the body — ``old_auth``, ``keys.auth``,
    an endpoint — back into the answer and every log that keeps it. ``fields`` names the
    failing locations only (``"body"`` for a body that is no object), never a value."""

    status_code = 422
    code = "push_body_invalid"
    default_detail = "This request body is not valid"
    extra: Mapping[str, object]

    def __init__(self, fields: Iterable[str] = (), detail: str | None = None) -> None:
        self.fields = tuple(fields)
        self.extra = {"fields": list(self.fields)}
        super().__init__(detail)


class UnsubscribeTokenError(NotificationError):
    """An unsubscribe link whose token doesn't verify — tampered, cut, signed with another
    secret, or of another version (§3.6): 400. A valid token always answers 200, also for
    a switch already off or an account that is gone, so the answer reveals nothing."""

    status_code = 400
    code = "unsubscribe_invalid"
    default_detail = "This unsubscribe link is not valid"
