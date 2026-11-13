"""A software WebAuthn authenticator for the passkey tests: the options as the kit answers
them (JSON), the credential as ``@simplewebauthn/browser`` posts it (JSON).

On the pattern of keksdose's and Kurvenschmiede's ``tests/support/soft_webauthn.py`` (itself
trimmed from ``soft-webauthn`` 0.1.4, MIT, CESNET), rewritten for JSON both ways and for the
user-verified flag the kit requires: one ES256 credential, ``none`` attestation, its CBOR by
``cbor2`` and its key by ``cryptography`` (both py_webauthn's own dependencies).
"""

from __future__ import annotations

import hashlib
import json
import os
from struct import pack
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from eifi1_server_kit.auth.passkeys import base64url_decode, base64url_encode

#: User present, user verified, attested credential data.
_UP, _UV, _AT = 0x01, 0x04, 0x40


def _cose_es256(public_key: ec.EllipticCurvePublicKey) -> bytes:
    numbers = public_key.public_numbers()
    return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})


class SoftAuthenticator:
    """One authenticator holding one credential. ``verifies`` False is a security key with no
    PIN: it answers user-present only."""

    def __init__(self, origin: str, *, verifies: bool = True, counts: bool = True) -> None:
        self.origin = origin
        self.verifies = verifies
        #: False is a synced passkey: its counter stays at zero for ever (Kurvenschmiede).
        self.counts = counts
        self.credential_id = os.urandom(32)
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.rp_id = ""
        self.user_handle = ""
        self.sign_count = 0

    def _flags(self, extra: int = 0) -> bytes:
        return bytes([_UP | (_UV if self.verifies else 0) | extra])

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        """``navigator.credentials.create`` for the kit's registration options."""
        self.rp_id = options["rp"]["id"]
        self.user_handle = options["user"]["id"]
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = (
            rp_hash
            + self._flags(_AT)
            + pack(">I", self.sign_count)
            + b"\x00" * 16
            + pack(">H", len(self.credential_id))
            + self.credential_id
            + _cose_es256(self.private_key.public_key())
        )
        attestation = cbor2.dumps({"authData": auth_data, "fmt": "none", "attStmt": {}})
        client_data = self._client_data("webauthn.create", options["challenge"])
        return {
            "id": base64url_encode(self.credential_id),
            "rawId": base64url_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": base64url_encode(client_data),
                "attestationObject": base64url_encode(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options: dict[str, Any], *, user_handle: str | None = None) -> dict[str, Any]:
        """``navigator.credentials.get`` for the kit's sign-in options."""
        if options["rpId"] != self.rp_id:
            raise ValueError("another relying party")
        if self.counts:
            self.sign_count += 1
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + self._flags() + pack(">I", self.sign_count)
        client_data = self._client_data("webauthn.get", options["challenge"])
        signature = self.private_key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": base64url_encode(self.credential_id),
            "rawId": base64url_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "authenticatorData": base64url_encode(auth_data),
                "clientDataJSON": base64url_encode(client_data),
                "signature": base64url_encode(signature),
                "userHandle": self.user_handle if user_handle is None else user_handle,
            },
            "clientExtensionResults": {},
        }

    @staticmethod
    def handle_of(user_id: int) -> str:
        return base64url_encode(str(user_id).encode())

    @staticmethod
    def decode(text: str) -> bytes:
        return base64url_decode(text)
