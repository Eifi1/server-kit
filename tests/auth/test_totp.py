"""The second factor's code (RFC 6238): the step a code matches, and a step accepted once
(keksdose AU-10, the kastlan reuse audit's proposal 9)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta, timezone

import pytest

from eifi1_server_kit.auth import (
    CHALLENGE_LIFETIMES,
    TOTP_DIGITS,
    TOTP_STEP_CLAIM,
    TOTP_STEP_SECONDS,
    TOTP_WINDOW,
    ChallengeKind,
    totp_matched_step,
    totp_step_accepted,
    two_factor_challenge_claims,
    two_factor_challenge_is_spent,
)

#: RFC 6238 Appendix B's SHA-1 seed, the ASCII "12345678901234567890", in base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
#: A secret as kastlan mints it (20 random bytes, base32, the padding stripped) and seeds it.
KASTLAN_SECRET = "QSP725C3W7I4K537FU2S3XDFV2YTKOSK"


def _code(secret: str, step: int) -> str:
    """An independent HOTP for the tests that need a code of a given step: RFC 4226 §5.3
    written out, not the module's own helper."""
    import base64

    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[19] & 0xF
    binary = (digest[offset] & 0x7F) << 24 | digest[offset + 1] << 16 | digest[offset + 2] << 8 | digest[offset + 3]
    return f"{binary % 1_000_000:06d}"


def _at(step: int, into: float = 0.0) -> float:
    """A POSIX time ``into`` seconds inside ``step``."""
    return step * TOTP_STEP_SECONDS + into


def test_the_constants_are_every_authenticator_apps() -> None:
    assert (TOTP_DIGITS, TOTP_STEP_SECONDS, TOTP_WINDOW) == (6, 30, 1)


@pytest.mark.parametrize(
    ("unix_time", "eight_digits"),
    [
        # RFC 6238 Appendix B, SHA-1: the six-digit code is the last six of the eight.
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc_6238_test_vectors(unix_time: int, eight_digits: str) -> None:
    step = unix_time // 30
    assert totp_matched_step(RFC_SECRET, eight_digits[-6:], now=unix_time, window=0) == step
    assert totp_matched_step(RFC_SECRET, eight_digits[-6:], now=datetime.fromtimestamp(unix_time, UTC)) == step


@pytest.mark.parametrize(
    ("counter", "code"),
    # RFC 4226 Appendix D: HOTP at counters 0–9, which TOTP is at steps 0–9.
    list(
        enumerate(["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"])
    ),
)
def test_rfc_4226_hotp_values_are_the_steps_codes(counter: int, code: str) -> None:
    assert totp_matched_step(RFC_SECRET, code, now=_at(counter, 15), window=0) == counter


def test_the_independent_helper_agrees_with_the_rfc() -> None:
    assert _code(RFC_SECRET, 1) == "287082"


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_one_step_either_side_matches(offset: int) -> None:
    now = 1_793_000_000.0
    step = int(now // 30) + offset
    assert totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step), now=now) == step


@pytest.mark.parametrize("offset", [-2, 2, 3, -10])
def test_two_steps_away_does_not(offset: int) -> None:
    now = 1_793_000_000.0
    step = int(now // 30) + offset
    assert totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step), now=now) is None


def test_the_window_edges_are_the_step_boundaries() -> None:
    """A code of step s is good from the first second of s-1 to the last of s+1: 90 s."""
    step = 59_766_666
    code = _code(KASTLAN_SECRET, step)
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step - 1)) == step
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step + 1, 29.999)) == step
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step - 2, 29.999)) is None
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step + 2)) is None


def test_a_wider_or_a_closed_window() -> None:
    step = 59_766_666
    code = _code(KASTLAN_SECRET, step)
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step + 2), window=2) == step
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step + 1), window=0) is None
    assert totp_matched_step(KASTLAN_SECRET, code, now=_at(step), window=0) == step
    with pytest.raises(ValueError, match="0 or more"):
        totp_matched_step(KASTLAN_SECRET, code, now=_at(step), window=-1)


def test_the_latest_match_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code matching two steps in the window counts for the newer (keksdose's ``max``)."""
    import eifi1_server_kit.auth.totp as totp

    monkeypatch.setattr(totp, "_code", lambda _key, _step: "123456")
    assert totp_matched_step(KASTLAN_SECRET, "123456", now=_at(1000, 5)) == 1001


def test_steps_before_the_epoch_are_never_computed() -> None:
    assert totp_matched_step(RFC_SECRET, "755224", now=0) == 0  # step 0; step -1 skipped
    assert totp_matched_step(RFC_SECRET, "287082", now=0) == 1


def test_now_is_a_datetime_or_a_timestamp() -> None:
    step = 59_766_666
    code = _code(KASTLAN_SECRET, step)
    aware = datetime.fromtimestamp(_at(step, 10), UTC)
    assert totp_matched_step(KASTLAN_SECRET, code, now=aware) == step
    assert totp_matched_step(KASTLAN_SECRET, code, now=aware.replace(tzinfo=None)) == step, "naive is UTC"
    zurich = aware.astimezone(timezone(timedelta(hours=1)))
    assert totp_matched_step(KASTLAN_SECRET, code, now=zurich) == step
    assert totp_matched_step(KASTLAN_SECRET, code, now=int(_at(step, 10))) == step


def test_a_secret_in_any_case_with_or_without_its_padding() -> None:
    step = 37037036
    code = "081804"
    for secret in (RFC_SECRET, RFC_SECRET.lower(), f" {RFC_SECRET} "):
        assert totp_matched_step(secret, code, now=_at(step), window=0) == step
    padded = "MFRGG==="  # "abc", padded as base64 writes it
    assert totp_matched_step(padded, _code("MFRGG", step), now=_at(step), window=0) == step


@pytest.mark.parametrize("secret", ["", "   ", "====", "not base32!", "AB1"])
def test_a_secret_that_is_no_secret_fails_closed(secret: str) -> None:
    with pytest.raises(ValueError, match="TOTP secret"):
        totp_matched_step(secret, "123456", now=0)


def test_a_typed_code_is_trimmed_and_normalised() -> None:
    step = 59_766_666
    code = _code(KASTLAN_SECRET, step)
    full_width = "".join(chr(ord(digit) - ord("0") + ord("０")) for digit in code)
    for typed in (f" {code}\n", full_width):
        assert totp_matched_step(KASTLAN_SECRET, typed, now=_at(step)) == step


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        "12345a",
        "123 456",
        "-12345",
        "+12345",
        "١٢٣٤٥٦",  # Arabic-Indic digits: digits to Python, not to an authenticator
        "ABCD-EFGH-IJKL-MNOP",  # kastlan's backup code, checked after the TOTP code fails
        "94287082",  # the RFC's eight digits
    ],
)
def test_a_malformed_code_matches_nothing(code: str) -> None:
    assert totp_matched_step(RFC_SECRET, code, now=59) is None


def test_the_same_step_is_accepted_once() -> None:
    step = 59_766_666
    code = _code(KASTLAN_SECRET, step)
    last: int | None = None
    matched = totp_matched_step(KASTLAN_SECRET, code, now=_at(step, 3))
    assert totp_step_accepted(matched, last)
    last = matched
    again = totp_matched_step(KASTLAN_SECRET, code, now=_at(step, 20))
    assert again == step and not totp_step_accepted(again, last), "the same code, the same step: replayed"


def test_an_older_step_still_in_the_window_is_refused_after_a_newer_one() -> None:
    step = 59_766_666
    newer = totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step + 1), now=_at(step))
    assert totp_step_accepted(newer, None)
    older = totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step), now=_at(step, 1))
    assert older == step and not totp_step_accepted(older, newer)


def test_a_later_step_is_accepted() -> None:
    step = 59_766_666
    first = totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step), now=_at(step))
    assert totp_step_accepted(first, None)
    second = totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step + 1), now=_at(step + 1))
    assert second == step + 1 and totp_step_accepted(second, first)


@pytest.mark.parametrize(
    ("matched", "last_step", "accepted"),
    [
        (None, None, False),
        (None, 5, False),
        (5, None, True),
        (5, 4, True),
        (5, 5, False),
        (5, 6, False),
        (0, None, True),
    ],
)
def test_the_replay_rule(matched: int | None, last_step: int | None, accepted: bool) -> None:
    assert totp_step_accepted(matched, last_step) is accepted


# --- one challenge, one sign-in (0.21) -------------------------------------------------------

NOW = datetime(2026, 11, 4, 9, 30, 15, 250_000, tzinfo=UTC)


def test_the_claim_is_keksdoses_and_kastlans() -> None:
    assert TOTP_STEP_CLAIM == "totp_step"


@pytest.mark.parametrize("last_step", [None, 0, 59_766_666])
def test_the_challenge_carries_the_last_accepted_step(last_step: int | None) -> None:
    claims = two_factor_challenge_claims(7, last_step=last_step, now=NOW)
    assert claims == {
        "sub": "7",
        "type": "2fa_challenge",
        "iat": NOW.timestamp(),
        "exp": int((NOW + CHALLENGE_LIFETIMES[ChallengeKind.TWO_FACTOR]).timestamp()),
        "totp_step": last_step,
    }


def test_a_challenge_completes_one_sign_in() -> None:
    """AU-10: minted at step None, redeemed (the step moves), then replayed with the next code."""
    step = 59_766_666
    last: int | None = None
    challenge = two_factor_challenge_claims(7, last_step=last, now=NOW)
    assert not two_factor_challenge_is_spent(challenge, last)
    matched = totp_matched_step(KASTLAN_SECRET, _code(KASTLAN_SECRET, step), now=_at(step))
    assert totp_step_accepted(matched, last)
    last = matched
    assert two_factor_challenge_is_spent(challenge, last), "the copy no longer matches"
    fresh = two_factor_challenge_claims(7, last_step=last, now=NOW)
    assert not two_factor_challenge_is_spent(fresh, last)


def test_a_challenge_from_before_the_claim_reads_as_none() -> None:
    old = {"sub": "7", "type": "2fa_challenge"}
    assert not two_factor_challenge_is_spent(old, None), "an account that has accepted no code since"
    assert two_factor_challenge_is_spent(old, 5)


@pytest.mark.parametrize("copy", [True, False, "5", 5.0, [5], {"step": 5}])
def test_anything_but_an_integer_or_none_is_spent(copy: object) -> None:
    assert two_factor_challenge_is_spent({TOTP_STEP_CLAIM: copy}, 5)
    assert two_factor_challenge_is_spent({TOTP_STEP_CLAIM: copy}, None)


@pytest.mark.parametrize(("copy", "last_step", "spent"), [(5, 5, False), (4, 5, True), (6, 5, True), (5, None, True)])
def test_spent_when_the_copy_differs(copy: int, last_step: int | None, spent: bool) -> None:
    assert two_factor_challenge_is_spent({TOTP_STEP_CLAIM: copy}, last_step) is spent
