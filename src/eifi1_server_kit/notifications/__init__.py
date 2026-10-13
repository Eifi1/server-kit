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
* :mod:`~eifi1_server_kit.notifications.push` — the device shapes, the payload, a push
  service's answer, the VAPID subject;
* :mod:`~eifi1_server_kit.notifications.unsubscribe` — the signed one-click token;
* :mod:`~eifi1_server_kit.notifications.inbox` — the inbox's shapes;
* :mod:`~eifi1_server_kit.notifications.errors` — the coded refusals.

The mail's half — ``List-Unsubscribe`` and the footer line — is in
:mod:`eifi1_server_kit.mail` (:class:`~eifi1_server_kit.mail.MailUnsubscribe`), and the
PATCH's plain fields go through :func:`eifi1_server_kit.settings.apply_patch` with
``skip=SETTINGS_SKIP``.
"""

from __future__ import annotations

from eifi1_server_kit.notifications.delivery import Delivery, plan_delivery
from eifi1_server_kit.notifications.errors import (
    NotificationError,
    NotificationTypeError,
    PushEndpointTakenError,
    UnsubscribeTokenError,
)
from eifi1_server_kit.notifications.inbox import (
    INBOX_RETENTION,
    InboxItem,
    InboxMarkRead,
    InboxMarkReadResponse,
    InboxResponse,
)
from eifi1_server_kit.notifications.push import (
    PUSH_MAX_FAILURES,
    PUSH_PAYLOAD_LIMIT,
    PUSH_TEST_TYPE,
    PUSH_TIMEOUT,
    PUSH_TTL,
    PushData,
    PushDeviceResponse,
    PushKeyResponse,
    PushOutcome,
    PushPayload,
    PushSubscriptionIn,
    PushSubscriptionKeys,
    PushTestResponse,
    PushUnregisterIn,
    push_outcome,
    should_reap,
    vapid_subject,
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
    resolve_switches,
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
    in_quiet_hours,
    is_known_zone,
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
    "INBOX_RETENTION",
    "PUSH_MAX_FAILURES",
    "PUSH_PAYLOAD_LIMIT",
    "PUSH_TEST_TYPE",
    "PUSH_TIMEOUT",
    "PUSH_TTL",
    "QUIET_HOURS_PATTERN",
    "SETTINGS_NOT_NULLABLE",
    "SETTINGS_SKIP",
    "TYPE_ID_PATTERN",
    "Delivery",
    "DigestCadence",
    "DigestFields",
    "DigestUpdate",
    "InboxItem",
    "InboxMarkRead",
    "InboxMarkReadResponse",
    "InboxResponse",
    "NotificationError",
    "NotificationSettingsResponse",
    "NotificationSettingsUpdate",
    "NotificationType",
    "NotificationTypeError",
    "NotificationTypes",
    "PushData",
    "PushDeviceResponse",
    "PushEndpointTakenError",
    "PushKeyResponse",
    "PushOutcome",
    "PushPayload",
    "PushSubscriptionIn",
    "PushSubscriptionKeys",
    "PushTestResponse",
    "PushUnregisterIn",
    "QuietHoursFields",
    "QuietHoursUpdate",
    "StoredSwitches",
    "TypeSwitches",
    "TypeSwitchesPatch",
    "UnsubscribeClaim",
    "UnsubscribeTokenError",
    "apply_switches_patch",
    "digest_due",
    "digest_period",
    "digest_tag",
    "in_quiet_hours",
    "is_known_zone",
    "plan_delivery",
    "push_outcome",
    "read_unsubscribe_token",
    "resolve_switches",
    "resolve_zone",
    "should_reap",
    "unsubscribe_key",
    "unsubscribe_token",
    "vapid_subject",
]
