"""The webhook's signature, checked per provider with ``hmac`` — no SDK.

``docs/billing-harmonization.md`` §5 in ``Eifi1/ui-kit``: one endpoint per provider, ``POST
/webhooks/<provider>``, and **a bad signature answers 400** (:class:`BillingError`
``invalid_signature``). Check it on the RAW body, before parsing: a JSON round trip changes
bytes, and the signature is over bytes.

* **Paddle Billing** — ``Paddle-Signature: ts=1671552777;h1=eb4d…``: ``h1`` is the hex
  HMAC-SHA256 of ``"<ts>:<raw body>"`` under the notification destination's secret key
  (``pdl_ntfset_…``); there may be more than one ``h1`` while Paddle rotates a secret; and
  the timestamp guards against a replay: "To prevent replay attacks, you may like to
  check the timestamp (``ts``) against the current time and reject events that are too
  old. Our SDKs have a default tolerance of five seconds between the timestamp and the
  current time." The kit's default is that, and the settings widen it
  (``billing_signature_tolerance``).
  https://developer.paddle.com/webhooks/signature-verification (read 2026-10-08 and
  2026-10-09).
* **Lemon Squeezy** (deprecated in 0.7.0, removed in 0.8, §14.13) — ``X-Signature``: the
  hex HMAC-SHA256 of the raw body under the webhook's signing secret. It carries no timestamp, so there is no replay window to check;
  the event store's duplicate check and the ordering guard carry that
  (:func:`~eifi1_server_kit.billing.dispatch`).
  https://docs.lemonsqueezy.com/help/webhooks/signing-requests (read 2026-10-08).

Every comparison is :func:`hmac.compare_digest` on BYTES: two ``str`` with a non-ASCII
character raise ``TypeError`` there (keksdose's Pub/Sub lesson), which an unauthenticated
caller could otherwise turn into a 500 at will.

An app calls :meth:`~eifi1_server_kit.billing.BillingSettings.verify_billing_webhook`, which
passes the deployment's secret and its tolerance (§14.2).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.settings import PADDLE_SIGNATURE_TOLERANCE, BillingProvider

__all__ = [
    "LEMONSQUEEZY_SIGNATURE_HEADER",
    "PADDLE_SIGNATURE_HEADER",
    "PADDLE_SIGNATURE_TOLERANCE",
    "SIGNATURE_HEADERS",
    "verify_lemonsqueezy_signature",
    "verify_paddle_signature",
    "verify_webhook_signature",
]

#: Paddle Billing's signature header.
PADDLE_SIGNATURE_HEADER = "Paddle-Signature"
#: Lemon Squeezy's signature header (deprecated in 0.7.0, removed in 0.8).
LEMONSQUEEZY_SIGNATURE_HEADER = "X-Signature"
#: Each provider's header, for :func:`verify_webhook_signature`.
SIGNATURE_HEADERS: Mapping[BillingProvider, str] = {
    BillingProvider.PADDLE: PADDLE_SIGNATURE_HEADER,
    BillingProvider.LEMONSQUEEZY: LEMONSQUEEZY_SIGNATURE_HEADER,
}


def _refuse(reason: str) -> BillingError:
    return BillingError(BillingErrorCode.INVALID_SIGNATURE, f"Invalid webhook signature: {reason}")


def _hex_hmac(secret: str, message: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest().encode("ascii")


def verify_paddle_signature(
    raw_body: bytes,
    header: str | None,
    secret: str,
    *,
    now: float | None = None,
    tolerance: float = PADDLE_SIGNATURE_TOLERANCE,
) -> None:
    """Check a Paddle Billing webhook's ``Paddle-Signature``, or raise 400
    ``invalid_signature``.

    The header is ``;``-separated ``key=value`` pairs: one ``ts`` (Unix seconds) and at
    least one ``h1``; unknown keys are ignored, so a future scheme beside ``h1`` doesn't
    break the check. The signed payload is ``ts``, a colon, and the raw body; any ``h1``
    that matches passes (secret rotation). ``now`` is Unix seconds, the current time by
    default; ``ts`` further than ``tolerance`` seconds from it is refused as a replay.
    """
    if not header:
        raise _refuse("no Paddle-Signature header")
    timestamp: str | None = None
    signatures: list[str] = []
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            timestamp = value.strip()
        elif key == "h1":
            signatures.append(value.strip())
    if timestamp is None or not timestamp.isascii() or not timestamp.isdigit():
        raise _refuse("no timestamp")
    if not signatures:
        raise _refuse("no h1 signature")
    moment = time.time() if now is None else now
    if abs(moment - int(timestamp)) > tolerance:
        raise _refuse("the timestamp is outside the tolerance")
    expected = _hex_hmac(secret, timestamp.encode("ascii") + b":" + raw_body)
    if not any(hmac.compare_digest(expected, signature.lower().encode("utf-8")) for signature in signatures):
        raise _refuse("no h1 matches")


def verify_lemonsqueezy_signature(raw_body: bytes, header: str | None, secret: str) -> None:
    """Check a Lemon Squeezy webhook's ``X-Signature`` — the hex HMAC-SHA256 of the raw
    body — or raise 400 ``invalid_signature``.

    .. deprecated:: 0.7.0
       Removed in 0.8 with the rest of Lemon Squeezy (§14.13).
    """
    if not header:
        raise _refuse("no X-Signature header")
    if not hmac.compare_digest(_hex_hmac(secret, raw_body), header.strip().lower().encode("utf-8")):
        raise _refuse("the signature does not match")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """``name`` from ``headers``, case-insensitively — Starlette's ``Headers`` already is; a
    plain ``dict`` in a test is not."""
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    return next((item for key, item in headers.items() if key.lower() == wanted), None)


def verify_webhook_signature(
    provider: BillingProvider | str,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    now: float | None = None,
    tolerance: float = PADDLE_SIGNATURE_TOLERANCE,
) -> None:
    """Check the signature of a webhook from ``provider``, reading its header from
    ``headers`` (§5)::

        settings.require_billing_enabled()                  # 404 billing_disabled
        raw = await request.body()
        verify_webhook_signature(
            BillingProvider.PADDLE, raw, request.headers, settings.billing_webhook_key(),
            tolerance=settings.billing_signature_tolerance,
        )

    ``now`` and ``tolerance`` are Paddle's (:func:`verify_paddle_signature`); pass the
    settings' ``billing_signature_tolerance``, so a deployment widens it in its
    environment — or call
    :meth:`~eifi1_server_kit.billing.BillingSettings.verify_billing_webhook`, which does
    (§14.2). Lemon Squeezy's signature has no timestamp. An unknown provider is a
    :class:`ValueError`.
    """
    chosen = BillingProvider(provider)
    header = _header(headers, SIGNATURE_HEADERS[chosen])
    if chosen is BillingProvider.PADDLE:
        verify_paddle_signature(raw_body, header, secret, now=now, tolerance=tolerance)
    else:
        verify_lemonsqueezy_signature(raw_body, header, secret)
