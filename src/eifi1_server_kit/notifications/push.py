"""Web Push: the device shapes, the payload, and what a push service's answer means (§3.5, §2.7).

keksdose's rules (``domain/services/push_service.py``, ``domain/schemas/push.py``), with
VAPID keys per app (``<APP>_VAPID_PUBLIC_KEY``, ``_PRIVATE_KEY``; unset = push off). The
sending stays in the app — ``pywebpush`` is blocking, so it runs off the event loop — and
the kit answers the parts every app must get the same:

* **TTL and timeout explicit** (:data:`PUSH_TTL`, :data:`PUSH_TIMEOUT`): pywebpush defaults
  to ``ttl=0`` — "discard unless the device is connected now" — and to no timeout at all
  (``push_service.py:1-16``, :212-228).
* **The outcome** (:func:`push_outcome`): a 404 or 410 means the browser dropped the
  subscription, so the row is deleted at once; anything else counts a failure, and the
  daily job deletes a row at :data:`PUSH_MAX_FAILURES` in a row (:func:`should_reap`;
  ``push_service.py:180-206``, :252-262).
* **The payload** (:class:`PushPayload`): ``{title, body, tag, data: {url, type}}``,
  under 3 KB, the body cut to fit. ``data.type`` is new, so the page can refresh its inbox
  and pick an icon; it sits under the service worker's own message type (keksdose nests
  the payload as ``{type: "PUSH_RECEIVED", data}``), so nothing collides.
* **The endpoint never goes in a URL**: it is a capability. Unregistering takes it in a
  body (:class:`PushUnregisterIn`); another account's endpoint is a 409
  (:class:`~.errors.PushEndpointTakenError`), never moved.
"""

from __future__ import annotations

import enum
import json
import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eifi1_server_kit.notifications.registry import TYPE_ID_PATTERN

__all__ = [
    "PUSH_MAX_FAILURES",
    "PUSH_PAYLOAD_LIMIT",
    "PUSH_TEST_TYPE",
    "PUSH_TIMEOUT",
    "PUSH_TTL",
    "PushData",
    "PushDeviceResponse",
    "PushKeyResponse",
    "PushOutcome",
    "PushPayload",
    "PushSubscriptionIn",
    "PushSubscriptionKeys",
    "PushTestResponse",
    "PushUnregisterIn",
    "push_outcome",
    "should_reap",
    "vapid_subject",
]

#: The encoded payload's ceiling in bytes. Web Push carries about 4 KB once encrypted; 3 KB
#: of JSON leaves room for the encryption's overhead.
PUSH_PAYLOAD_LIMIT = 3072
#: Seconds a push service keeps a message for a device that is offline (keksdose
#: ``push_ttl_seconds``): a day.
PUSH_TTL = 86_400
#: Seconds one send may take before it counts as failed (keksdose ``push_timeout_seconds``).
PUSH_TIMEOUT = 10.0
#: Failures in a row after which the daily job deletes a device (keksdose
#: ``push_max_failures``). Every success resets the count.
PUSH_MAX_FAILURES = 10
#: The payload's ``data.type`` for a test push, the account's own and an admin's alike.
PUSH_TEST_TYPE = "test"

#: A push service's endpoint is an https URL; anything longer than this is no endpoint.
_ENDPOINT_MAX_LENGTH = 2048
_ELLIPSIS = "…"
#: A path in the app: one ``/`` and not a second one or a backslash after it, which a
#: browser reads as another host (``//evil.example``, ``/\\evil.example``).
_APP_PATH = re.compile(r"^/(?![/\\])")
#: What a browser strips from a URL (tab, newline) or reads as ``/`` (backslash), and the
#: other control characters: none of them belongs in a path the server writes.
_UNSAFE_IN_PATH = re.compile(r"[\x00-\x1f\x7f\\]")
_MAILBOX = re.compile(r"^[^@\s<>:]+@[^@\s<>:]+\.[^@\s<>:]+$")


class PushSubscriptionKeys(BaseModel):
    """The two keys a payload is encrypted for (``PushSubscription.toJSON().keys``)."""

    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscriptionIn(BaseModel):
    """``POST /notifications/devices``: the browser's ``PushSubscription.toJSON()`` and a
    label to tell devices apart (keksdose ``schemas/push.py:16-22``). Registering is the
    switch: an upsert by endpoint for the same account, a 409 for another's.

    Its other fields (``expirationTime``) are ignored, as the browser sends them. The
    endpoint is an https URL — a push service's — so no row ever points the server's
    sends at a plain-http address.
    """

    endpoint: str = Field(min_length=1, max_length=_ENDPOINT_MAX_LENGTH, pattern=r"^https://\S+$")
    keys: PushSubscriptionKeys
    #: ``navigator.userAgent``, for the device list.
    user_agent: str | None = Field(default=None, max_length=400)


class PushUnregisterIn(BaseModel):
    """``POST /notifications/devices/unregister {endpoint}``: the caller's own device, by
    its endpoint in the body — never in a URL. Silent when there is none."""

    endpoint: str = Field(min_length=1, max_length=_ENDPOINT_MAX_LENGTH)


class PushKeyResponse(BaseModel):
    """``GET /notifications/push-key``: the VAPID public key; ``""`` when push is off."""

    key: str


class PushTestResponse(BaseModel):
    """``POST /notifications/test``: how many devices took the test push."""

    sent: int


class PushDeviceResponse(BaseModel):
    """One of the caller's devices in ``GET /notifications/devices``, oldest first. The
    owner gets the whole endpoint, to mark "this device" (keksdose ``schemas/push.py:128-139``)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | int
    endpoint: str
    user_agent: str | None
    created_at: datetime
    last_success_at: datetime | None
    failure_count: int


class PushData(BaseModel):
    """The payload's ``data``: where a click goes, and the notification's type."""

    #: A path in the app (``/support``), opened by the service worker on the app's origin;
    #: never ``//``, a backslash or a scheme.
    url: str
    #: The type id, or :data:`PUSH_TEST_TYPE` for a test push.
    type: str = Field(pattern=TYPE_ID_PATTERN)

    @field_validator("url")
    @classmethod
    def _app_path(cls, value: str) -> str:
        if not _APP_PATH.match(value) or _UNSAFE_IN_PATH.search(value):
            raise ValueError("a push url is a path in the app: /…, never // or a scheme")
        return value


class PushPayload(BaseModel):
    """What the service worker receives: ``{title, body, tag, data: {url, type}}``.

    ``tag`` is one per event: a repeat of the same event replaces the earlier notification
    (``digest:2026-W42``), distinct events don't (``support:<uuid>``). Send
    :meth:`encode`'s bytes.
    """

    title: str
    body: str
    tag: str = Field(min_length=1)
    data: PushData

    def _encoded(self, body: str) -> bytes:
        content = self.model_dump(mode="json")
        content["body"] = body
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def encode(self, limit: int = PUSH_PAYLOAD_LIMIT) -> bytes:
        """The payload as compact UTF-8 JSON of at most ``limit`` bytes.

        A body that doesn't fit is cut, and ends in "…", at the longest length that fits; a
        :class:`ValueError` when even an empty body doesn't — a title or a tag too long for
        a push.
        """
        whole = self._encoded(self.body)
        if len(whole) <= limit:
            return whole
        if len(self._encoded("")) > limit:
            raise ValueError(f"the push payload is over {limit} bytes even with an empty body")
        # The longest cut that fits: the encoded length only grows with the cut's length.
        low, high = 0, len(self.body) - 1
        best = b""
        while low <= high:
            middle = (low + high) // 2
            candidate = self._encoded(self.body[:middle].rstrip() + _ELLIPSIS)
            if len(candidate) <= limit:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best or self._encoded("")


class PushOutcome(enum.StrEnum):
    """What one send's answer means for the device's row (§2.7)."""

    #: 2xx: delivered. Reset the failure count and stamp ``last_success_at``.
    DELIVERED = "delivered"
    #: 404 or 410: the browser dropped the subscription. Delete the row now.
    GONE = "gone"
    #: Anything else, a timeout included: count it; :func:`should_reap` says when to stop.
    FAILED = "failed"


def push_outcome(status: int | None) -> PushOutcome:
    """The :class:`PushOutcome` of a push service's HTTP status; ``None`` — no answer, a
    timeout, a connection error — is :attr:`PushOutcome.FAILED` (keksdose
    ``push_service.py:242-273``)."""
    if status is None:
        return PushOutcome.FAILED
    if 200 <= status < 300:
        return PushOutcome.DELIVERED
    if status in (404, 410):
        return PushOutcome.GONE
    return PushOutcome.FAILED


def should_reap(failure_count: int) -> bool:
    """Whether the daily job deletes a device: :data:`PUSH_MAX_FAILURES` failures in a row
    (keksdose ``reap_dead_subscriptions``)."""
    return failure_count >= PUSH_MAX_FAILURES


def vapid_subject(support_address: str) -> str:
    """The VAPID ``sub`` claim: ``mailto:`` the app's support address (§2.7), not
    keksdose's ``admin@``. Pass the app's reply-to — server-kit
    :func:`~eifi1_server_kit.mail.support_address` by default. A new subject leaves existing
    subscriptions working. A :class:`ValueError` for anything but a bare address."""
    address = support_address.strip()
    if not _MAILBOX.match(address):
        raise ValueError(f"a VAPID subject is mailto: a bare address, like support@example.com: {support_address!r}")
    return f"mailto:{address}"
