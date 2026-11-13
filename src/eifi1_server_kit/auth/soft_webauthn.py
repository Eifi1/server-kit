"""A software WebAuthn authenticator, so a passkey test runs both ceremonies without a browser.

``docs/auth-harmonization.md`` §12.3 (the testing support) and §12.5 item 8 in
``Eifi1/ui-kit``. :class:`SoftAuthenticator` plays the browser and the authenticator behind
it: it takes the options a server's begin route answers
(``PublicKeyCredentialCreationOptionsJSON``, ``PublicKeyCredentialRequestOptionsJSON``, with
base64url fields) and answers the JSON a browser posts to the finish route
(``RegistrationResponseJSON``, ``AuthenticationResponseJSON``), so the server's own WebAuthn
library checks real signatures and nothing about the cryptography is mocked::

    authenticator = SoftAuthenticator()
    begin = client.post("/auth/passkey/register/begin", ...).json()
    credential = authenticator.register(begin["options"], ORIGIN)
    ...
    begin = client.post("/auth/passkey/login/begin", ...).json()
    credential = authenticator.authenticate(begin["options"], ORIGIN)

* :meth:`SoftAuthenticator.register`: a new ES256 (P-256) credential with a random id,
  attested ``none``;
* :meth:`SoftAuthenticator.authenticate`: an assertion signed over the authenticator data
  and the client data's hash, its counter one up per sign-in;
* **user presence always, user verification (UV) by default**, switchable for the
  authenticator (``verifies_user=False``) or for one ceremony (``user_verified=False``): the
  kit requires UV for a passkey sign-in (§12.5 item 8), and a test has to show the refusal
  without it;
* a synced passkey (``synced=True``, iCloud Keychain, Google Password Manager): backed up,
  and its counter stays at zero on every assertion, which is the replay a counter check
  cannot catch and one use per challenge must;
* the tampering a test needs: a wrong origin is the ``origin`` argument, ``wrong_rp_id=``
  hashes another RP id into the authenticator data, ``sign_count=`` signs one assertion
  with a reused or lower counter.

What it does not do: any attestation but ``none`` (a platform passkey's, and what the apps
ask for), any algorithm but ES256, extensions (``clientExtensionResults`` is always empty,
so no PRF), or the browser's own checks of the origin against the RP id. It never refuses a
ceremony for the options' ``userVerification``: a real client fails one it cannot verify
for, but then the server never sees an answer, and the server's refusal is what a test is
after. Its private keys sit in memory: a test double, not a security device.

Merged from the two apps' ``tests/support/soft_webauthn.py`` (keksdose's, and
Kurvenschmiede's copy of it), both vendored from ``soft-webauthn`` 0.1.4
(https://github.com/bodik/soft-webauthn, MIT License, Copyright (C) since 2019 CESNET,
z.s.p.o): the authenticator data, the COSE key and the signature are theirs; keksdose's
``verifies_user`` (UV is the device's ability, here regardless of what the options ask) and
Kurvenschmiede's ``counts`` (here ``synced``, which also sets the backup flags a synced
passkey reports) are both kept. Unlike them it speaks the server's JSON both ways, so the
tests' translation helpers (``@simplewebauthn/browser``'s job in the app) go, and it holds
any number of credentials and honours ``allowCredentials`` and ``excludeCredentials``.

Needs the ``passkeys`` extra (``eifi1-server-kit[passkeys]``): cbor2 and cryptography, both
dependencies of py_webauthn, which the server side verifies with.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit

try:
    import cbor2
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.soft_webauthn needs cbor2 and cryptography: "
        "install the kit with its passkeys extra, eifi1-server-kit[passkeys]."
    ) from exc

__all__ = ["SoftAuthenticator", "SoftCredential"]

# The authenticator data's flags (WebAuthn §6.1).
_USER_PRESENT: Final = 0x01
_USER_VERIFIED: Final = 0x04
_BACKUP_ELIGIBLE: Final = 0x08
_BACKED_UP: Final = 0x10
_ATTESTED_CREDENTIAL: Final = 0x40

#: ECDSA with SHA-256 on P-256, the COSE algorithm every server offers and the one key type
#: this authenticator makes.
_ES256: Final = -7

#: A ``none`` attestation names no authenticator model.
_NO_AAGUID: Final = bytes(16)

_CREDENTIAL_ID_BYTES: Final = 32


def _b64url(data: bytes) -> str:
    # WebAuthn's JSON is base64url without padding; a padded value is a different string.
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _from_b64url(text: str) -> bytes:
    # The options come without padding, and the decoder wants it back.
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _client_data(ceremony: str, challenge: str, origin: str) -> bytes:
    """The ``clientDataJSON`` a browser serialises, compact as Chrome's.

    The challenge is decoded and encoded again, as a browser turns the options into a buffer
    before it writes the client data: a server that pads its challenge sees it unpadded here,
    as it would from a browser."""
    fields = {"type": ceremony, "challenge": _b64url(_from_b64url(challenge)), "origin": origin, "crossOrigin": False}
    return json.dumps(fields, separators=(",", ":")).encode("utf-8")


def _rp_id(named: str | None, origin: str) -> str:
    """The RP id the options name or, as a browser defaults it when they name none, the
    origin's host."""
    if named:
        return named
    host = urlsplit(origin).hostname
    if not host:
        raise ValueError(f"the options name no RP id, and the origin {origin!r} has no host to default it to")
    return host


def _authenticator_data(rp_id: str, flags: int, sign_count: int) -> bytes:
    """The fixed head of the authenticator data: the RP id's hash, the flags, the counter."""
    return _sha256(rp_id.encode("utf-8")) + bytes([flags]) + struct.pack(">I", sign_count)


@dataclass(slots=True)
class SoftCredential:
    """One passkey the authenticator holds: its key, the RP and the user it was made for,
    and the counter of its last signature."""

    credential_id: bytes
    rp_id: str
    user_handle: bytes
    private_key: ec.EllipticCurvePrivateKey
    #: 0 at registration, one up per sign-in; a synced passkey's stays 0.
    sign_count: int = 0

    @property
    def cose_key(self) -> bytes:
        """The public key as the attested credential data carries it, and as a server stores
        it: a COSE_Key (RFC 9053 EC2, ``alg`` ES256) in CTAP2's canonical CBOR."""
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: _ES256,  # alg
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            },
            canonical=True,
        )


class SoftAuthenticator:
    """A browser with one authenticator behind it, holding any number of passkeys.

    ``verifies_user`` is whether the authenticator verifies its person (a PIN, a fingerprint):
    a platform passkey does, a bare security key does not and reports presence only.
    ``synced`` makes it a synced passkey provider. ``transports`` and ``attachment`` are what
    the browser reports with a registration (a platform passkey's by default)."""

    def __init__(
        self,
        *,
        verifies_user: bool = True,
        synced: bool = False,
        transports: Sequence[str] = ("hybrid", "internal"),
        attachment: Literal["platform", "cross-platform"] = "platform",
    ) -> None:
        self.verifies_user = verifies_user
        self.synced = synced
        self.transports = tuple(transports)
        self.attachment = attachment
        self._credentials: list[SoftCredential] = []

    @property
    def credentials(self) -> tuple[SoftCredential, ...]:
        """The passkeys registered so far, oldest first."""
        return tuple(self._credentials)

    def register(
        self,
        options: Mapping[str, Any],
        origin: str,
        *,
        user_verified: bool | None = None,
        wrong_rp_id: str | None = None,
    ) -> dict[str, Any]:
        """A new credential for the options' RP and user, as the browser's
        ``RegistrationResponseJSON``: the attestation object (``none``), the client data,
        and the level-3 fields a browser adds (the authenticator data, the public key as
        SubjectPublicKeyInfo, its algorithm).

        ``user_verified`` overrides the authenticator's UV for this ceremony;
        ``wrong_rp_id`` is hashed into the authenticator data instead of the options' RP id,
        while the credential stays scoped to the right one.

        Raises :class:`ValueError` where a browser would fail the ceremony: the options offer
        no ES256, or exclude a credential this authenticator already holds for the RP."""
        offered = options.get("pubKeyCredParams") or [{"type": "public-key", "alg": _ES256}]
        if not any(param.get("type") == "public-key" and param.get("alg") == _ES256 for param in offered):
            raise ValueError("the options offer no ES256 (alg -7), the one key type this authenticator makes")
        rp_id = _rp_id(options["rp"].get("id"), origin)
        excluded = {_from_b64url(entry["id"]) for entry in options.get("excludeCredentials") or ()}
        if any(held.rp_id == rp_id and held.credential_id in excluded for held in self._credentials):
            raise ValueError("the authenticator already holds a credential the options exclude (InvalidStateError)")

        credential = SoftCredential(
            credential_id=secrets.token_bytes(_CREDENTIAL_ID_BYTES),
            rp_id=rp_id,
            user_handle=_from_b64url(options["user"]["id"]),
            private_key=ec.generate_private_key(ec.SECP256R1()),
        )
        client_data = _client_data("webauthn.create", options["challenge"], origin)
        authenticator_data = (
            _authenticator_data(wrong_rp_id or rp_id, _ATTESTED_CREDENTIAL | self._flags(user_verified), 0)
            + _NO_AAGUID
            + struct.pack(">H", len(credential.credential_id))
            + credential.credential_id
            + credential.cose_key
        )
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": authenticator_data}, canonical=True)
        public_key = credential.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self._credentials.append(credential)
        return {
            "id": _b64url(credential.credential_id),
            "rawId": _b64url(credential.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "attestationObject": _b64url(attestation_object),
                "authenticatorData": _b64url(authenticator_data),
                "publicKey": _b64url(public_key),
                "publicKeyAlgorithm": _ES256,
                "transports": list(self.transports),
            },
            "authenticatorAttachment": self.attachment,
            "clientExtensionResults": {},
        }

    def authenticate(
        self,
        options: Mapping[str, Any],
        origin: str,
        *,
        user_verified: bool | None = None,
        wrong_rp_id: str | None = None,
        sign_count: int | None = None,
        credential_id: bytes | None = None,
    ) -> dict[str, Any]:
        """An assertion by the one credential the options' RP id and ``allowCredentials``
        (none: any of the RP's, a discoverable sign-in) leave, as the browser's
        ``AuthenticationResponseJSON``, with the user handle the credential was made for.

        ``user_verified`` and ``wrong_rp_id`` as for :meth:`register`; ``sign_count`` signs
        this assertion with that counter (a replayed or lower one) and leaves the
        credential's own where it was; ``credential_id`` is the person's pick where several
        credentials match.

        Raises :class:`ValueError` where a browser would find no passkey (NotAllowedError),
        and where several match and no ``credential_id`` picks one."""
        rp_id = _rp_id(options.get("rpId"), origin)
        allowed = {_from_b64url(entry["id"]) for entry in options.get("allowCredentials") or ()}
        found = [
            held
            for held in self._credentials
            if held.rp_id == rp_id
            and (not allowed or held.credential_id in allowed)
            and (credential_id is None or held.credential_id == credential_id)
        ]
        if not found:
            raise ValueError(f"the authenticator holds no credential for {rp_id!r} that the options allow")
        if len(found) > 1:
            raise ValueError("several credentials match: pass credential_id= to pick one, as the person would")
        credential = found[0]

        if sign_count is None:
            credential.sign_count += 0 if self.synced else 1
            sign_count = credential.sign_count
        client_data = _client_data("webauthn.get", options["challenge"], origin)
        authenticator_data = _authenticator_data(wrong_rp_id or rp_id, self._flags(user_verified), sign_count)
        signature = credential.private_key.sign(authenticator_data + _sha256(client_data), ec.ECDSA(hashes.SHA256()))
        return {
            "id": _b64url(credential.credential_id),
            "rawId": _b64url(credential.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "authenticatorData": _b64url(authenticator_data),
                "signature": _b64url(signature),
                "userHandle": _b64url(credential.user_handle),
            },
            "authenticatorAttachment": self.attachment,
            "clientExtensionResults": {},
        }

    def _flags(self, user_verified: bool | None) -> int:
        """User presence always; UV as the ceremony or else the authenticator says; a synced
        passkey is backup-eligible and backed up (py_webauthn refuses the second without the
        first)."""
        flags = _USER_PRESENT
        if self.verifies_user if user_verified is None else user_verified:
            flags |= _USER_VERIFIED
        if self.synced:
            flags |= _BACKUP_ELIGIBLE | _BACKED_UP
        return flags
