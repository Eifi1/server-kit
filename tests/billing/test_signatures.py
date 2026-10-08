"""The webhook signatures (billing contract §5), as the providers document them:
https://developer.paddle.com/webhooks/signature-verification and
https://docs.lemonsqueezy.com/help/webhooks/signing-requests."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import ValidationError

from eifi1_server_kit.billing import (
    LEMONSQUEEZY_SIGNATURE_HEADER,
    PADDLE_SIGNATURE_HEADER,
    PADDLE_SIGNATURE_TOLERANCE,
    SIGNATURE_HEADERS,
    BillingError,
    BillingErrorCode,
    BillingProvider,
    BillingSettings,
    verify_lemonsqueezy_signature,
    verify_paddle_signature,
    verify_webhook_signature,
)

SECRET = "pdl_ntfset_01gkpjp8bkm3tm53kdgkx6sms7_example"
BODY = b'{"event_id":"evt_01","event_type":"subscription.updated","data":{"id":"sub_01"}}'
TS = 1_791_460_800


def _h1(body: bytes = BODY, ts: int = TS, secret: str = SECRET) -> str:
    """The docs' recipe, written out independently: HMAC-SHA256 of ``ts:body``, hex."""
    return hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()


def _refused(reason: str) -> pytest.RaisesExc[BillingError]:
    return pytest.raises(BillingError, match=reason)


def test_the_headers_are_the_providers() -> None:
    assert (PADDLE_SIGNATURE_HEADER, LEMONSQUEEZY_SIGNATURE_HEADER) == ("Paddle-Signature", "X-Signature")
    assert SIGNATURE_HEADERS == {
        BillingProvider.PADDLE: "Paddle-Signature",
        BillingProvider.LEMONSQUEEZY: "X-Signature",
    }
    assert PADDLE_SIGNATURE_TOLERANCE == 5.0


def test_a_paddle_signature_passes() -> None:
    verify_paddle_signature(BODY, f"ts={TS};h1={_h1()}", SECRET, now=TS)
    verify_paddle_signature(BODY, f" ts={TS} ; h1={_h1().upper()} ", SECRET, now=TS + 5)
    # During a secret rotation there is more than one h1; any match passes.
    verify_paddle_signature(BODY, f"ts={TS};h1={'0' * 64};h1={_h1()};h2=future", SECRET, now=TS - 5)


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "no Paddle-Signature header"),
        ("", "no Paddle-Signature header"),
        (f"h1={'a' * 64}", "no timestamp"),
        (f"ts=;h1={'a' * 64}", "no timestamp"),
        (f"ts=-5;h1={'a' * 64}", "no timestamp"),
        (f"ts=١٢٣;h1={'a' * 64}", "no timestamp"),
        (f"ts={TS}", "no h1 signature"),
        (f"ts={TS};h1={'a' * 64}", "no h1 matches"),
        (f"ts={TS};h1=ü", "no h1 matches"),  # non-ASCII: refused, never a TypeError
    ],
)
def test_a_paddle_signature_refuses_a_bad_header(header: str | None, reason: str) -> None:
    with _refused(reason) as refused:
        verify_paddle_signature(BODY, header, SECRET, now=TS)
    assert (refused.value.status_code, refused.value.code) == (400, BillingErrorCode.INVALID_SIGNATURE)


def test_a_paddle_signature_refuses_a_replay_a_tampered_body_and_another_secret() -> None:
    header = f"ts={TS};h1={_h1()}"
    with _refused("outside the tolerance"):
        verify_paddle_signature(BODY, header, SECRET, now=TS + 6)
    with _refused("outside the tolerance"):
        verify_paddle_signature(BODY, header, SECRET, now=TS - 6)
    verify_paddle_signature(BODY, header, SECRET, now=TS + 60, tolerance=60)
    with _refused("no h1 matches"):
        verify_paddle_signature(BODY + b" ", header, SECRET, now=TS)
    with _refused("no h1 matches"):
        verify_paddle_signature(BODY, header, "pdl_ntfset_other", now=TS)
    # The timestamp is signed too: moving it breaks the signature.
    with _refused("no h1 matches"):
        verify_paddle_signature(BODY, f"ts={TS + 1};h1={_h1()}", SECRET, now=TS)


def test_the_tolerance_is_a_setting_paddles_five_seconds_by_default() -> None:
    """A scale-to-zero host's cold start can eat five seconds; the deployment widens the
    window in its environment (keksdose runs 60), and the route passes it on."""
    assert BillingSettings().billing_signature_tolerance == PADDLE_SIGNATURE_TOLERANCE
    cold = BillingSettings(billing_signature_tolerance=60)
    headers = {"Paddle-Signature": f"ts={TS};h1={_h1()}"}
    with _refused("outside the tolerance"):
        verify_webhook_signature("paddle", BODY, headers, SECRET, now=TS + 30)
    verify_webhook_signature("paddle", BODY, headers, SECRET, now=TS + 30, tolerance=cold.billing_signature_tolerance)
    with _refused("outside the tolerance"):
        verify_webhook_signature(
            "paddle", BODY, headers, SECRET, now=TS + 61, tolerance=cold.billing_signature_tolerance
        )
    for refused in (0, -1, "soon"):
        with pytest.raises(ValidationError):
            BillingSettings.model_validate({"billing_signature_tolerance": refused})


def test_a_paddle_signature_checks_against_the_current_time_by_default() -> None:
    with _refused("outside the tolerance"):
        verify_paddle_signature(BODY, f"ts={TS};h1={_h1()}", SECRET)


def test_a_lemon_squeezy_signature_is_the_hex_hmac_of_the_raw_body() -> None:
    secret = "ls-signing-secret"
    digest = hmac.new(secret.encode(), BODY, hashlib.sha256).hexdigest()
    verify_lemonsqueezy_signature(BODY, digest, secret)
    verify_lemonsqueezy_signature(BODY, f" {digest.upper()} ", secret)
    for header, reason in [
        (None, "no X-Signature header"),
        ("", "no X-Signature header"),
        ("0" * 64, "does not match"),
        ("ü", "does not match"),
    ]:
        with _refused(reason):
            verify_lemonsqueezy_signature(BODY, header, secret)
    with _refused("does not match"):
        verify_lemonsqueezy_signature(BODY + b"\n", digest, secret)


def test_verify_webhook_signature_reads_the_providers_header_in_any_case() -> None:
    verify_webhook_signature("paddle", BODY, {"paddle-signature": f"ts={TS};h1={_h1()}"}, SECRET, now=TS)
    verify_webhook_signature(BillingProvider.PADDLE, BODY, {"Paddle-Signature": f"ts={TS};h1={_h1()}"}, SECRET, now=TS)
    digest = hmac.new(b"ls", BODY, hashlib.sha256).hexdigest()
    verify_webhook_signature(BillingProvider.LEMONSQUEEZY, BODY, {"X-SIGNATURE": digest}, "ls")
    with _refused("no X-Signature header"):
        verify_webhook_signature("lemonsqueezy", BODY, {"Paddle-Signature": "ts=1;h1=a"}, "ls")
    with pytest.raises(ValueError):
        verify_webhook_signature("stripe", BODY, {}, "s")
