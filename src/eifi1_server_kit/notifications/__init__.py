"""Notifications, as code (``docs/notifications-harmonization.md`` §4.1 in ``Eifi1/ui-kit``).

Layer 1, like the rest of the kit: pure functions and Pydantic models — no database, no
routes, no network, no new dependency. The app keeps its tables, its routes (the demo's
refusal among them), its triggers, its words and its sending (``pywebpush``, its mailer).
keksdose is the source (``backend/keksdose/domain/schemas/push.py`` and its services),
cited per rule.

* :mod:`~eifi1_server_kit.notifications.registry` — the app's types, their defaults, and
  the account's narrowing;
* :mod:`~eifi1_server_kit.notifications.resource` — ``GET/PATCH /notifications/settings``:
  the switches as they apply, and the PATCH's rules for ``types``;
* :mod:`~eifi1_server_kit.notifications.delivery` — who gets an inbox row, a push, a mail;
* :mod:`~eifi1_server_kit.notifications.timing` — quiet hours, the zone, the digest's period;
* :mod:`~eifi1_server_kit.notifications.retry` — a job's notification mail that failed to
  send: the window it is owed in, the marker, the ``Idempotency-Key`` (§9.4);
* :mod:`~eifi1_server_kit.notifications.push` — the device shapes, the payload, a push
  service's answer, the VAPID subject;
* :mod:`~eifi1_server_kit.notifications.unsubscribe` — the signed one-click token;
* :mod:`~eifi1_server_kit.notifications.inbox` — the inbox's shapes, and (0.16) the
  :class:`InboxStore` port;
* :mod:`~eifi1_server_kit.notifications.queue` — (0.16, layer 2, opt-in) the queue that
  holds a push through quiet hours and bundles mails, and the tick;
* :mod:`~eifi1_server_kit.notifications.errors` — the coded refusals.

Round 0.46 (server-kit 0.16, ``docs/notifications-harmonization.md`` §10): a switch per
channel (``app``, ``push``, ``email``) and per device (``muted_types``), the zone on the
account (:func:`effective_zone`), the inbox's port and the queue — ports and services
without FastAPI. Their routes are ``eifi1_server_kit.notifications.router`` (the
``fastapi`` extra: :func:`~eifi1_server_kit.notifications.router.inbox_router`,
:func:`~eifi1_server_kit.notifications.router.notifications_jobs_router`) and the fakes
``eifi1_server_kit.notifications.testing``; neither is re-exported.

The mail's half — ``List-Unsubscribe`` and the footer line, and a job's mail's
``Idempotency-Key`` — is in :mod:`eifi1_server_kit.mail`
(:class:`~eifi1_server_kit.mail.MailUnsubscribe`,
:attr:`~eifi1_server_kit.mail.MailMessage.idempotency_key`), and the
PATCH's plain fields go through :func:`eifi1_server_kit.settings.apply_patch` with
``skip=SETTINGS_SKIP``.
"""

from __future__ import annotations

from eifi1_server_kit.notifications.delivery import (
    Delivery,
    apply_muted_types,
    device_receives,
    plan_delivery,
    read_muted_types,
)
from eifi1_server_kit.notifications.errors import (
    NotificationChannelError,
    NotificationError,
    NotificationTypeError,
    PushBodyInvalidError,
    PushEndpointTakenError,
    UnsubscribeTokenError,
)
from eifi1_server_kit.notifications.inbox import (
    INBOX_PAGE_DEFAULT,
    INBOX_PAGE_MAX,
    INBOX_RETENTION,
    InboxItem,
    InboxMarkRead,
    InboxMarkReadResponse,
    InboxResponse,
    InboxStore,
    NewInboxItem,
)
from eifi1_server_kit.notifications.push import (
    PUSH_GONE_GRACE,
    PUSH_MAX_FAILURES,
    PUSH_PAYLOAD_LIMIT,
    PUSH_TEST_TYPE,
    PUSH_TIMEOUT,
    PUSH_TTL,
    PushData,
    PushDeviceResponse,
    PushDeviceUpdate,
    PushKeyResponse,
    PushOutcome,
    PushPayload,
    PushReplaceIn,
    PushSubscriptionIn,
    PushSubscriptionKeys,
    PushTestResponse,
    PushUnregisterIn,
    documented_body,
    parse_push_body,
    parse_push_replace,
    push_auth_matches,
    push_gone_expired,
    push_outcome,
    should_reap,
    vapid_subject,
)
from eifi1_server_kit.notifications.queue import (
    QUEUE_CLAIM_TIMEOUT,
    QUEUE_FLUSH_LIMIT,
    QUEUE_MAX_ATTEMPTS,
    QUEUE_MORE,
    QUEUE_RETENTION,
    FlushPort,
    FlushResult,
    NewQueueItem,
    NotificationQueue,
    QueueChannel,
    QueueItem,
    QueueService,
    notifications_tick,
)
from eifi1_server_kit.notifications.registry import TYPE_ID_PATTERN, NotificationType, NotificationTypes
from eifi1_server_kit.notifications.resource import (
    SETTINGS_NOT_NULLABLE,
    SETTINGS_SKIP,
    DigestFields,
    DigestUpdate,
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
    QuietHoursFields,
    QuietHoursUpdate,
    StoredSwitches,
    TypeSwitches,
    TypeSwitchesPatch,
    apply_switches_patch,
    migrate_enabled_to_channels,
    resolve_switches,
)
from eifi1_server_kit.notifications.retry import (
    MAIL_EVENT_PATTERN,
    MAIL_LATE_DAYS,
    MAIL_MARKER_RETENTION,
    MailMarker,
    mail_due,
    mail_event,
)
from eifi1_server_kit.notifications.timing import (
    DEFAULT_ZONE,
    DIGEST_CADENCES,
    DIGEST_LATE_DAYS,
    QUIET_HOURS_PATTERN,
    DigestCadence,
    digest_due,
    digest_period,
    digest_tag,
    effective_zone,
    in_quiet_hours,
    is_known_zone,
    local_today,
    quiet_hours_end,
    resolve_zone,
)
from eifi1_server_kit.notifications.unsubscribe import (
    UnsubscribeClaim,
    read_unsubscribe_token,
    unsubscribe_key,
    unsubscribe_token,
)

__all__ = [
    "DEFAULT_ZONE",
    "DIGEST_CADENCES",
    "DIGEST_LATE_DAYS",
    "INBOX_PAGE_DEFAULT",
    "INBOX_PAGE_MAX",
    "INBOX_RETENTION",
    "MAIL_EVENT_PATTERN",
    "MAIL_LATE_DAYS",
    "MAIL_MARKER_RETENTION",
    "PUSH_GONE_GRACE",
    "PUSH_MAX_FAILURES",
    "PUSH_PAYLOAD_LIMIT",
    "PUSH_TEST_TYPE",
    "PUSH_TIMEOUT",
    "PUSH_TTL",
    "QUEUE_CLAIM_TIMEOUT",
    "QUEUE_FLUSH_LIMIT",
    "QUEUE_MAX_ATTEMPTS",
    "QUEUE_MORE",
    "QUEUE_RETENTION",
    "QUIET_HOURS_PATTERN",
    "SETTINGS_NOT_NULLABLE",
    "SETTINGS_SKIP",
    "TYPE_ID_PATTERN",
    "Delivery",
    "DigestCadence",
    "DigestFields",
    "DigestUpdate",
    "FlushPort",
    "FlushResult",
    "InboxItem",
    "InboxMarkRead",
    "InboxMarkReadResponse",
    "InboxResponse",
    "InboxStore",
    "MailMarker",
    "NewInboxItem",
    "NewQueueItem",
    "NotificationChannelError",
    "NotificationError",
    "NotificationQueue",
    "NotificationSettingsResponse",
    "NotificationSettingsUpdate",
    "NotificationType",
    "NotificationTypeError",
    "NotificationTypes",
    "PushBodyInvalidError",
    "PushData",
    "PushDeviceResponse",
    "PushDeviceUpdate",
    "PushEndpointTakenError",
    "PushKeyResponse",
    "PushOutcome",
    "PushPayload",
    "PushReplaceIn",
    "PushSubscriptionIn",
    "PushSubscriptionKeys",
    "PushTestResponse",
    "PushUnregisterIn",
    "QueueChannel",
    "QueueItem",
    "QueueService",
    "QuietHoursFields",
    "QuietHoursUpdate",
    "StoredSwitches",
    "TypeSwitches",
    "TypeSwitchesPatch",
    "UnsubscribeClaim",
    "UnsubscribeTokenError",
    "apply_muted_types",
    "apply_switches_patch",
    "device_receives",
    "digest_due",
    "digest_period",
    "digest_tag",
    "documented_body",
    "effective_zone",
    "in_quiet_hours",
    "is_known_zone",
    "local_today",
    "mail_due",
    "mail_event",
    "migrate_enabled_to_channels",
    "notifications_tick",
    "parse_push_body",
    "parse_push_replace",
    "plan_delivery",
    "push_auth_matches",
    "push_gone_expired",
    "push_outcome",
    "quiet_hours_end",
    "read_muted_types",
    "read_unsubscribe_token",
    "resolve_switches",
    "resolve_zone",
    "should_reap",
    "unsubscribe_key",
    "unsubscribe_token",
    "vapid_subject",
]
