"""The sealed TOTP secret (0.32, keksdose's KG-1): ``auth.seal.SecretBox``, keksdose's
``secret_box`` lifted, its stored values opening unchanged."""

import base64
import hashlib
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from eifi1_server_kit.auth.seal import (
    SEALED_PREFIX,
    SecretBox,
    SecretKeyMissingError,
    SecretSealError,
    SecretUnreadableError,
    totp_pending_secret_aad,
    totp_secret_aad,
)

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_a_sealed_secret_opens_in_its_own_row_only() -> None:
    box = SecretBox(["a-key"])
    sealed = box.seal(SECRET, aad=totp_secret_aad(7))
    assert sealed.startswith(SEALED_PREFIX) and SECRET not in sealed
    assert box.unseal(sealed, aad=totp_secret_aad(7)) == SECRET
    assert box.seal(SECRET, aad=totp_secret_aad(7)) != sealed  # a fresh nonce each time
    for elsewhere in (totp_secret_aad(8), totp_pending_secret_aad(7)):
        with pytest.raises(SecretUnreadableError):
            box.unseal(sealed, aad=elsewhere)


def test_the_newest_key_seals_and_every_key_opens() -> None:
    old = SecretBox("old-key")
    rotated = SecretBox("new-key,  old-key\n")
    by_old = old.seal(SECRET, aad=b"x")
    assert rotated.unseal(by_old, aad=b"x") == SECRET
    by_new = rotated.seal(SECRET, aad=b"x")
    assert SecretBox([" new-key "]).unseal(by_new, aad=b"x") == SECRET
    with pytest.raises(SecretUnreadableError, match="1 tried"):
        old.unseal(by_new, aad=b"x")


def test_without_a_key_nothing_is_sealed_or_opened() -> None:
    for box in (SecretBox(""), SecretBox([]), SecretBox(["  "])):
        assert box.key_is_configured is False
        with pytest.raises(SecretKeyMissingError):
            box.seal(SECRET, aad=b"x")
        with pytest.raises(SecretKeyMissingError):
            box.unseal(SEALED_PREFIX + "AAAA", aad=b"x")
    assert SecretBox("k").key_is_configured is True
    assert issubclass(SecretKeyMissingError, SecretSealError) and issubclass(SecretUnreadableError, SecretSealError)


@pytest.mark.parametrize(
    "value",
    [SECRET, SEALED_PREFIX + "abc", SEALED_PREFIX + "é", SEALED_PREFIX + base64.urlsafe_b64encode(b"short").decode()],
)
def test_what_is_not_a_sealed_value_is_unreadable(value: str) -> None:
    with pytest.raises(SecretUnreadableError):
        SecretBox("k").unseal(value, aad=b"x")


def test_keksdose_s_stored_values_open_with_its_label() -> None:
    """keksdose's ``secret_box.seal`` as it ran: its label, its AAD, its layout."""
    key = hashlib.sha256(b"keksdose.secret_box.v1|operator-key").digest()
    nonce = os.urandom(12)
    aad = b"users.totp_secret:42"
    stored = "enc:v1:" + base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, SECRET.encode(), aad)).decode()
    box = SecretBox("operator-key", label=b"keksdose.secret_box.v1")
    assert totp_secret_aad(42) == aad and totp_pending_secret_aad(42) == b"users.totp_pending_secret:42"
    assert box.unseal(stored, aad=totp_secret_aad(42)) == SECRET
    with pytest.raises(SecretUnreadableError):
        SecretBox("operator-key").unseal(stored, aad=totp_secret_aad(42))
