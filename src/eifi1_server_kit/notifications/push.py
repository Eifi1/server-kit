"""Web Push: the device shapes, the payload, and what a push service's answer means (§3.5, §2.7).

keksdose's rules (``domain/services/push_service.py``, ``domain/schemas/push.py``), with
VAPID keys per app (``<APP>_VAPID_PUBLIC_KEY``, ``_PRIVATE_KEY``; unset = push off). The
sending stays in the app — ``pywebpush`` is blocking, so it runs off the event loop — and
the kit answers the parts every app must get the same:

* **TTL and timeout explicit** (:data:`PUSH_TTL`, :data:`PUSH_TIMEOUT`): pywebpush defaults
  to ``ttl=0`` — "discard unless the device is connected now" — and to no timeout at all
  (``push_service.py:1-16``, :212-228).
* **The outcome** (:func:`push_outcome`): a 404 or 410 means the browser dropped the
  subscription, so the row is marked gone (``gone_at``, 0.16, §10.6 rule 4) and gets no
  more sends; a replace may still claim it for :data:`PUSH_GONE_GRACE`, and the daily job
  deletes it after that. Anything else counts a failure, and the daily job deletes a row
  at :data:`PUSH_MAX_FAILURES` in a row (:func:`should_reap`; ``push_service.py:180-206``,
  :252-262).
* **The payload** (:class:`PushPayload`): ``{title, body, tag, data: {url, type, inbox}}``,
  under 3 KB, the body cut to fit. ``data.type`` is new, so the page can refresh its inbox
  and pick an icon; it sits under the service worker's own message type (keksdose nests
  the payload as ``{type: "PUSH_RECEIVED", data}``), so nothing collides.
* **The endpoint never goes in a URL**: it is a capability. Unregistering takes it in a
  body (:class:`PushUnregisterIn`); another account's endpoint is a 409
  (:class:`~.errors.PushEndpointTakenError`), never moved.
* **A rotated subscription is replaced in place** (0.16, :class:`PushReplaceIn`): the
  browser's service worker re-registers on ``pushsubscriptionchange`` (ui-kit
  ``handlePushSubscriptionChange``), proving the device by the old endpoint and its
  ``auth`` secret (:func:`push_auth_matches`), not by a session a worker cannot send.
* **A bad body is never echoed** (0.16.1, keksdose S1): FastAPI's own 422 puts each
  error's ``input`` in the answer — for :class:`PushReplaceIn` that is ``old_auth``, the
  proof that redirects a person's pushes; for :class:`PushSubscriptionIn` the new
  ``keys.auth`` and ``p256dh``; for :class:`PushUnregisterIn` the endpoint, a capability.
  Take the body untyped (``body: Annotated[Any, Body()]``) and parse it with
  :func:`parse_push_replace` or :func:`parse_push_body`: a bad one raises
  :class:`~.errors.PushBodyInvalidError`, a plain coded 422 naming the failing fields and
  no value. (Malformed JSON stays FastAPI's ``json_invalid``, whose ``input`` is empty.)
  The unsubscribe token travels in the path, typed plain ``str``: give it no pattern or
  length, or a 422 would echo it — :func:`~.unsubscribe.read_unsubscribe_token` refuses a
  bad one with a 400 that echoes nothing.
"""

from __future__ import annotations

import enum
import hmac
import json
import re
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from eifi1_server_kit.notifications.errors import PushBodyInvalidError
from eifi1_server_kit.notifications.registry import TYPE_ID_PATTERN

__all__ = [
    "PUSH_GONE_GRACE",
    "PUSH_MAX_FAILURES",
    "PUSH_PAYLOAD_LIMIT",
    "PUSH_TEST_TYPE",
    "PUSH_TIMEOUT",
    "PUSH_TTL",
    "PushData",
    "PushDeviceResponse",
    "PushDeviceUpdate",
    "PushKeyResponse",
    "PushOutcome",
    "PushPayload",
    "PushReplaceIn",
    "PushSubscriptionIn",
    "PushSubscriptionKeys",
    "PushTestResponse",
    "PushUnregisterIn",
    "parse_push_body",
    "parse_push_replace",
    "push_auth_matches",
    "push_gone_expired",
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
#: How long a device row a push service answered 404/410 for stays claimable by
#: ``POST /notifications/devices/replace`` (§10.6 rule 4): the browser may rotate the
#: subscription, a push hit the old endpoint, and the worker's replace comes after. The
#: reaper marks the row ``gone_at`` and stops sending to it; the daily reap deletes rows
#: gone longer than this.
PUSH_GONE_GRACE = timedelta(days=7)
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


class PushReplaceIn(BaseModel):
    """``POST /notifications/devices/replace``: the browser rotated a subscription
    (``pushsubscriptionchange``) and its service worker re-registers it (0.16, NT:1028).

    No session: a worker has no ``Authorization`` header to send. The proof is the OLD
    subscription — its endpoint and its ``keys.auth`` secret, which only that browser and
    this server hold (:func:`push_auth_matches` against the stored row). The route, in
    this order (§10.6, settled 2026-10-19):

    1. no device row has ``old_endpoint`` — a row marked gone (``gone_at``) within
       :data:`PUSH_GONE_GRACE` still counts, an older one does not
       (:func:`push_gone_expired`) —, or its stored
       ``auth`` does not match → 404, the same answer for both (nothing to tell apart);
       the page then shows push as off;
    2. the row's account is inactive (deactivated, a deletion request) or a demo → the
       same 404, and the row is deleted: a signed-out or closed account keeps no pushes;
    3. only now, with the device proven, ``subscription.endpoint`` held by ANOTHER
       account's row → 409 (:class:`~.errors.PushEndpointTakenError`), as on
       registering — an unproven caller never learns that an endpoint is taken;
    4. else the row moves in place: the new endpoint and keys, the same account, the user
       agent if sent, ``failure_count`` reset, ``gone_at`` cleared → 200
       :class:`PushDeviceResponse`. The new endpoint already on the SAME account (a
       register raced the worker) deletes the old row instead and answers the existing one.

    And around it:

    * **The app deletes an account's push rows** on "sign out everywhere", on
      deactivation (kastlan's deletion request deactivates) and on erasure, or a
      device's worker could rotate its subscription forever.
    * **No RLS on device rows**: a device row is the account's, never a company's (the
      role is checked per company at send time); a sessionless route under forced RLS
      answers 404. An app whose table is FORCE RLS (keksdose) runs the lookup and the
      update under its bypass with an explicit endpoint filter — this route only.
    * **Never log or echo the body**: its 422 answers a plain detail, never the input (a
      validation echo would hand ``old_auth`` back) — take the body untyped and parse it
      with :func:`parse_push_replace` (0.16.1). A leaked row's ``keys.auth`` is enough to
      redirect that person's future pushes.
    * Rate-limit it as the other unauthenticated routes.
    """

    #: The rotated-away subscription's endpoint, as registered.
    old_endpoint: str = Field(min_length=1, max_length=_ENDPOINT_MAX_LENGTH)
    #: Its ``keys.auth``, as registered (base64url): the proof.
    old_auth: str = Field(min_length=1, max_length=256)
    #: The browser's new subscription, as on registering.
    subscription: PushSubscriptionIn


def parse_push_body[M: BaseModel](model: type[M], body: object) -> M:
    """``body`` validated as ``model`` — a push route's body taken untyped
    (``body: Annotated[Any, Body()]``) so FastAPI never answers it with a 422 that echoes
    the secret inside (0.16.1, keksdose S1).

    A body that is no valid ``model`` raises :class:`~.errors.PushBodyInvalidError` (422,
    ``push_body_invalid``) naming the failing fields, with no value and no chained
    :class:`~pydantic.ValidationError` (whose text carries the input) — so neither the
    answer nor a logged traceback holds ``old_auth`` or a key. Log nothing of the body
    either."""
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        fields = dict.fromkeys(".".join(str(part) for part in error["loc"]) or "body" for error in exc.errors())
    raise PushBodyInvalidError(fields)


def parse_push_replace(body: object) -> PushReplaceIn:
    """:class:`PushReplaceIn` from ``POST /notifications/devices/replace``'s untyped body:
    :func:`parse_push_body` for the replace route, whose ``old_auth`` must never come back
    in a 422::

        @router.post("/notifications/devices/replace", response_model=PushDeviceResponse)
        async def replace(body: Annotated[Any, Body()]) -> PushDeviceResponse:
            request = parse_push_replace(body)  # PushBodyInvalidError → plain 422
            ...
    """
    return parse_push_body(PushReplaceIn, body)


def push_auth_matches(stored: str, given: str) -> bool:
    """Whether a :class:`PushReplaceIn`'s ``old_auth`` is the stored row's ``keys.auth``,
    in constant time (a timing difference would leak the secret byte by byte). Base64url
    padding is ignored on both sides: browsers send none, a stored one may carry it."""
    return hmac.compare_digest(stored.rstrip("=").encode(), given.rstrip("=").encode())


def push_gone_expired(gone_at: datetime | None, now: datetime, *, grace: timedelta = PUSH_GONE_GRACE) -> bool:
    """Whether a device row marked gone (``gone_at``, a push service's 404/410) is past its
    grace (§10.6 rule 4): the daily reap deletes it, and the replace route treats it as
    absent (404). ``False`` for a row that is not gone (``None``); within the grace a
    replace may still claim it. Aware datetimes."""
    return gone_at is not None and now - gone_at > grace


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
    #: The types this device mutes (§10.2 rule 3, 0.16), in the registry's order: the
    #: stored list through :func:`~.delivery.read_muted_types`, so an id the registry
    #: dropped is left out.
    muted_types: list[str] = Field(default_factory=list)


class PushDeviceUpdate(BaseModel):
    """``PATCH /notifications/devices/{id} {"muted_types": [...]}`` (§10.2 rule 3, 0.16):
    the WHOLE list of types this device mutes; ``[]`` mutes none. Checked against the
    account's registry by :func:`~.delivery.apply_muted_types` (422 for an id the account
    doesn't receive, or one without push); another account's device is a 404."""

    model_config = ConfigDict(extra="forbid")

    muted_types: list[str] = Field(max_length=100)


class PushData(BaseModel):
    """The payload's ``data``: where a click goes, the notification's type, and whether
    the inbox holds it."""

    #: A path in the app (``/support``), opened by the service worker on the app's origin;
    #: never ``//``, a backslash or a scheme.
    url: str
    #: The type id, or :data:`PUSH_TEST_TYPE` for a test push.
    type: str = Field(pattern=TYPE_ID_PATTERN)
    #: An inbox row was written for it (§10.2, 0.16). ``False`` (push on, in-app off): the
    #: service worker shows the notification even while a window has focus, as nothing
    #: else would show it.
    inbox: bool = True

    @field_validator("url")
    @classmethod
    def _app_path(cls, value: str) -> str:
        if not _APP_PATH.match(value) or _UNSAFE_IN_PATH.search(value):
            raise ValueError("a push url is a path in the app: /…, never // or a scheme")
        return value


class PushPayload(BaseModel):
    """What the service worker receives: ``{title, body, tag, data: {url, type, inbox}}``.

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
    #: 404 or 410: the browser dropped the subscription. Mark the row gone (``gone_at``)
    #: and send to it no more; delete it after :data:`PUSH_GONE_GRACE` (§10.6 rule 4).
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


def should_reap(failure_count: int, *, limit: int = PUSH_MAX_FAILURES) -> bool:
    """Whether the daily job deletes a device: ``limit`` failures in a row,
    :data:`PUSH_MAX_FAILURES` unless the app sets its own (keksdose's ``push_max_failures``
    setting, ``reap_dead_subscriptions``). A ``limit`` below 1 is a :class:`ValueError`: it
    would reap a device that never failed."""
    if limit < 1:
        raise ValueError(f"a reap limit is 1 or more: {limit}")
    return failure_count >= limit


def vapid_subject(support_address: str) -> str:
    """The VAPID ``sub`` claim: ``mailto:`` the app's support address (§2.7), not
    keksdose's ``admin@``. Pass the app's reply-to — server-kit
    :func:`~eifi1_server_kit.mail.support_address` by default. A new subject leaves existing
    subscriptions working. A :class:`ValueError` for anything but a bare address."""
    address = support_address.strip()
    if not _MAILBOX.match(address):
        raise ValueError(f"a VAPID subject is mailto: a bare address, like support@example.com: {support_address!r}")
    return f"mailto:{address}"
