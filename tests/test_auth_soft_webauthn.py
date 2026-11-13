"""The software authenticator (auth §12.3), checked from the outside: every answer is taken
apart as a server would, without py_webauthn — the CBOR, the authenticator data, the COSE
key turned back into a public key, and the assertion's signature verified with it."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import re
import struct
import sys
from dataclasses import dataclass
from typing import Any

import cbor2
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from eifi1_server_kit.auth.soft_webauthn import SoftAuthenticator

ORIGIN = "https://accounts.example.org"
RP_ID = "accounts.example.org"
USER_ID = b"\x00\x01account-7"
REGISTER_CHALLENGE = bytes(range(32))
SIGN_IN_CHALLENGE = bytes(range(100, 132))

# The flags, spelt out here rather than imported, so a wrong bit in the module shows.
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    """Strict: WebAuthn's JSON is the URL-safe alphabet and no padding."""
    assert re.fullmatch(r"[A-Za-z0-9_-]*", text), text
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def creation_options(**changes: Any) -> dict[str, Any]:
    """What a py_webauthn server's register begin answers (``options_to_json``)."""
    options: dict[str, Any] = {
        "rp": {"name": "Example", "id": RP_ID},
        "user": {"id": b64url(USER_ID), "name": "a@example.org", "displayName": "A"},
        "challenge": b64url(REGISTER_CHALLENGE),
        "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
        "timeout": 60000,
        "excludeCredentials": [],
        "authenticatorSelection": {"residentKey": "required", "userVerification": "required"},
        "attestation": "none",
    }
    return options | changes


def request_options(**changes: Any) -> dict[str, Any]:
    """What a py_webauthn server's sign-in begin answers."""
    options: dict[str, Any] = {
        "challenge": b64url(SIGN_IN_CHALLENGE),
        "timeout": 60000,
        "rpId": RP_ID,
        "allowCredentials": [],
        "userVerification": "required",
    }
    return options | changes


@dataclass(frozen=True)
class AuthenticatorData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    rest: bytes


def authenticator_data(raw: bytes) -> AuthenticatorData:
    (sign_count,) = struct.unpack(">I", raw[33:37])
    return AuthenticatorData(raw[:32], raw[32], sign_count, raw[37:])


@dataclass(frozen=True)
class Attested:
    data: AuthenticatorData
    aaguid: bytes
    credential_id: bytes
    cose_key: bytes


def attested(answer: dict[str, Any]) -> Attested:
    """The attestation object taken apart: ``none``, and the attested credential data."""
    attestation: dict[str, Any] = cbor2.loads(unb64url(answer["response"]["attestationObject"]))
    assert attestation["fmt"] == "none" and attestation["attStmt"] == {}
    data = authenticator_data(attestation["authData"])
    aaguid, (length,) = data.rest[:16], struct.unpack(">H", data.rest[16:18])
    credential_id, cose_key = data.rest[18 : 18 + length], data.rest[18 + length :]
    return Attested(data, aaguid, credential_id, cose_key)


def public_key_of(cose_key: bytes) -> ec.EllipticCurvePublicKey:
    """An EC2 P-256 ES256 COSE_Key back into a key, checked field by field."""
    fields: dict[int, Any] = cbor2.loads(cose_key)
    # Nothing trails the key: no extensions were flagged, so the key ends the data.
    assert cbor2.dumps(fields) == cose_key
    assert (fields[1], fields[3], fields[-1]) == (2, -7, 1)
    assert len(fields[-2]) == len(fields[-3]) == 32
    x, y = int.from_bytes(fields[-2], "big"), int.from_bytes(fields[-3], "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def client_data(answer: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(unb64url(answer["response"]["clientDataJSON"]))
    return parsed


def signature_holds(key: ec.EllipticCurvePublicKey, answer: dict[str, Any]) -> bool:
    """WebAuthn §7.2 step 20: the signature over authenticatorData ‖ SHA-256(clientDataJSON)."""
    response = answer["response"]
    signed = unb64url(response["authenticatorData"]) + hashlib.sha256(unb64url(response["clientDataJSON"])).digest()
    try:
        key.verify(unb64url(response["signature"]), signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def rp_hash(rp_id: str) -> bytes:
    return hashlib.sha256(rp_id.encode()).digest()


# ── The registration ─────────────────────────────────────────────────────────


def test_a_registration_answers_the_browsers_json_in_base64url() -> None:
    answer = SoftAuthenticator().register(creation_options(), ORIGIN)
    assert set(answer) == {"id", "rawId", "type", "response", "authenticatorAttachment", "clientExtensionResults"}
    assert answer["id"] == answer["rawId"] and len(unb64url(answer["rawId"])) == 32
    assert (answer["type"], answer["authenticatorAttachment"], answer["clientExtensionResults"]) == (
        "public-key",
        "platform",
        {},
    )
    assert answer["response"]["transports"] == ["hybrid", "internal"]
    assert answer["response"]["publicKeyAlgorithm"] == -7


def test_the_registrations_client_data_names_the_ceremony_the_challenge_and_the_origin() -> None:
    data = client_data(SoftAuthenticator().register(creation_options(), ORIGIN))
    assert data == {
        "type": "webauthn.create",
        "challenge": b64url(REGISTER_CHALLENGE),
        "origin": ORIGIN,
        "crossOrigin": False,
    }


def test_a_padded_challenge_comes_back_unpadded_as_from_a_browser() -> None:
    padded = base64.urlsafe_b64encode(b"\x01\x02\x03\x04").decode()
    assert padded.endswith("=")
    data = client_data(SoftAuthenticator().register(creation_options(challenge=padded), ORIGIN))
    assert data["challenge"] == padded.rstrip("=")


def test_the_attestation_carries_the_rp_id_hash_presence_verification_and_the_credential() -> None:
    authenticator = SoftAuthenticator()
    answer = authenticator.register(creation_options(), ORIGIN)
    parts = attested(answer)
    assert parts.data.rp_id_hash == rp_hash(RP_ID)
    assert parts.data.flags == UP | UV | AT
    assert parts.data.sign_count == 0
    assert parts.aaguid == bytes(16)
    assert parts.credential_id == unb64url(answer["rawId"])
    (held,) = authenticator.credentials
    assert (held.credential_id, held.rp_id, held.user_handle, held.sign_count) == (
        parts.credential_id,
        RP_ID,
        USER_ID,
        0,
    )
    assert held.cose_key == parts.cose_key


def test_the_level_3_fields_agree_with_the_attestation_object() -> None:
    answer = SoftAuthenticator().register(creation_options(), ORIGIN)
    parts = attested(answer)
    attestation: dict[str, Any] = cbor2.loads(unb64url(answer["response"]["attestationObject"]))
    assert unb64url(answer["response"]["authenticatorData"]) == attestation["authData"]
    spki = serialization.load_der_public_key(unb64url(answer["response"]["publicKey"]))
    assert isinstance(spki, ec.EllipticCurvePublicKey)
    assert spki.public_numbers() == public_key_of(parts.cose_key).public_numbers()


def test_each_registration_is_a_new_key_under_a_random_id() -> None:
    authenticator = SoftAuthenticator()
    first = attested(authenticator.register(creation_options(), ORIGIN))
    second = attested(authenticator.register(creation_options(user={"id": b64url(b"other")}), ORIGIN))
    assert first.credential_id != second.credential_id
    assert public_key_of(first.cose_key).public_numbers() != public_key_of(second.cose_key).public_numbers()
    assert [held.user_handle for held in authenticator.credentials] == [USER_ID, b"other"]


def test_the_transports_and_the_attachment_are_the_authenticators() -> None:
    key = SoftAuthenticator(transports=["usb", "nfc"], attachment="cross-platform")
    answer = key.register(creation_options(), ORIGIN)
    assert (answer["response"]["transports"], answer["authenticatorAttachment"]) == (["usb", "nfc"], "cross-platform")
    signed_in = key.authenticate(request_options(), ORIGIN)
    assert signed_in["authenticatorAttachment"] == "cross-platform"


def test_a_registration_without_es256_on_offer_is_refused_and_an_empty_offer_means_es256() -> None:
    with pytest.raises(ValueError, match="ES256"):
        SoftAuthenticator().register(creation_options(pubKeyCredParams=[{"type": "public-key", "alg": -257}]), ORIGIN)
    with pytest.raises(ValueError, match="ES256"):
        SoftAuthenticator().register(creation_options(pubKeyCredParams=[{"type": "other", "alg": -7}]), ORIGIN)
    # WebAuthn §5.1.3 step 10: no parameters is ES256 and RS256.
    answer = SoftAuthenticator().register(creation_options(pubKeyCredParams=[]), ORIGIN)
    assert public_key_of(attested(answer).cose_key)


def test_a_second_registration_the_options_exclude_is_refused() -> None:
    authenticator = SoftAuthenticator()
    first = authenticator.register(creation_options(), ORIGIN)
    exclude = [{"id": first["id"], "type": "public-key", "transports": ["internal"]}]
    with pytest.raises(ValueError, match="exclude"):
        authenticator.register(creation_options(excludeCredentials=exclude), ORIGIN)
    # The same id excluded by another RP is no reason to refuse: credentials are scoped.
    elsewhere = creation_options(rp={"name": "Other", "id": "other.example.org"}, excludeCredentials=exclude)
    authenticator.register(elsewhere, "https://other.example.org")
    assert len(authenticator.credentials) == 2


def test_options_without_an_rp_id_take_the_origins_host() -> None:
    authenticator = SoftAuthenticator()
    answer = authenticator.register(creation_options(rp={"name": "Example"}), "https://host.example.org:8443")
    assert attested(answer).data.rp_id_hash == rp_hash("host.example.org")
    options = request_options()
    del options["rpId"]
    signed_in = authenticator.authenticate(options, "https://host.example.org:8443")
    assert authenticator_data(unb64url(signed_in["response"]["authenticatorData"])).rp_id_hash == rp_hash(
        "host.example.org"
    )
    with pytest.raises(ValueError, match="no host"):
        authenticator.register(creation_options(rp={"name": "Example"}), "null")


# ── The sign-in ──────────────────────────────────────────────────────────────


def test_the_attested_key_verifies_the_sign_ins_signature() -> None:
    authenticator = SoftAuthenticator()
    key = public_key_of(attested(authenticator.register(creation_options(), ORIGIN)).cose_key)
    answer = authenticator.authenticate(request_options(), ORIGIN)
    assert signature_holds(key, answer)
    # Tampered client data no longer matches: the signature covers its hash.
    forged = json.dumps(client_data(answer) | {"origin": "https://evil.example"}).encode()
    tampered = answer | {"response": answer["response"] | {"clientDataJSON": b64url(forged)}}
    assert not signature_holds(key, tampered)


def test_a_sign_in_names_its_ceremony_and_hands_back_the_credential_and_the_user() -> None:
    authenticator = SoftAuthenticator()
    registered = authenticator.register(creation_options(), ORIGIN)
    answer = authenticator.authenticate(request_options(), ORIGIN)
    assert set(answer) == {"id", "rawId", "type", "response", "authenticatorAttachment", "clientExtensionResults"}
    assert (answer["id"], answer["rawId"], answer["type"]) == (registered["id"], registered["id"], "public-key")
    assert set(answer["response"]) == {"clientDataJSON", "authenticatorData", "signature", "userHandle"}
    assert unb64url(answer["response"]["userHandle"]) == USER_ID
    assert client_data(answer) == {
        "type": "webauthn.get",
        "challenge": b64url(SIGN_IN_CHALLENGE),
        "origin": ORIGIN,
        "crossOrigin": False,
    }
    data = authenticator_data(unb64url(answer["response"]["authenticatorData"]))
    assert (data.rp_id_hash, data.flags, data.rest) == (rp_hash(RP_ID), UP | UV, b"")


def test_the_sign_count_rises_by_one_per_sign_in() -> None:
    authenticator = SoftAuthenticator()
    authenticator.register(creation_options(), ORIGIN)
    counts = [
        authenticator_data(
            unb64url(authenticator.authenticate(request_options(), ORIGIN)["response"]["authenticatorData"])
        ).sign_count
        for _ in range(3)
    ]
    assert counts == [1, 2, 3]
    assert authenticator.credentials[0].sign_count == 3


def test_a_synced_passkey_keeps_its_counter_at_zero_and_says_it_is_backed_up() -> None:
    authenticator = SoftAuthenticator(synced=True)
    key = public_key_of(attested(authenticator.register(creation_options(), ORIGIN)).cose_key)
    assert attested(authenticator.register(creation_options(user={"id": "AA"}), ORIGIN)).data.flags == (
        UP | UV | BE | BS | AT
    )
    for _ in range(2):
        answer = authenticator.authenticate(
            request_options(), ORIGIN, credential_id=authenticator.credentials[0].credential_id
        )
        data = authenticator_data(unb64url(answer["response"]["authenticatorData"]))
        assert (data.sign_count, data.flags) == (0, UP | UV | BE | BS)
        assert signature_holds(key, answer)


# ── User verification ────────────────────────────────────────────────────────


def test_an_authenticator_that_does_not_verify_reports_presence_only() -> None:
    """Even where the options require UV: the server's refusal is what the test is after."""
    key = SoftAuthenticator(verifies_user=False)
    assert attested(key.register(creation_options(), ORIGIN)).data.flags == UP | AT
    answer = key.authenticate(request_options(userVerification="required"), ORIGIN)
    assert authenticator_data(unb64url(answer["response"]["authenticatorData"])).flags == UP


def test_one_ceremony_can_go_without_verification_or_with_it() -> None:
    authenticator = SoftAuthenticator()
    assert attested(authenticator.register(creation_options(), ORIGIN, user_verified=False)).data.flags == UP | AT

    def flags(answer: dict[str, Any]) -> int:
        return authenticator_data(unb64url(answer["response"]["authenticatorData"])).flags

    assert flags(authenticator.authenticate(request_options(), ORIGIN, user_verified=False)) == UP
    # The override is for that ceremony only.
    assert flags(authenticator.authenticate(request_options(), ORIGIN)) == UP | UV
    authenticator.verifies_user = False
    assert flags(authenticator.authenticate(request_options(), ORIGIN, user_verified=True)) == UP | UV


# ── Choosing the credential ──────────────────────────────────────────────────


def test_the_sign_in_takes_the_credential_the_rp_id_and_allow_credentials_leave() -> None:
    authenticator = SoftAuthenticator()
    first = authenticator.register(creation_options(), ORIGIN)
    second = authenticator.register(creation_options(user={"id": b64url(b"second")}), ORIGIN)
    authenticator.register(
        creation_options(rp={"name": "Other", "id": "other.example.org"}), "https://other.example.org"
    )
    allow = [{"id": second["id"], "type": "public-key"}]
    answer = authenticator.authenticate(request_options(allowCredentials=allow), ORIGIN)
    assert answer["id"] == second["id"] and unb64url(answer["response"]["userHandle"]) == b"second"
    picked = authenticator.authenticate(request_options(), ORIGIN, credential_id=unb64url(first["id"]))
    assert picked["id"] == first["id"]
    elsewhere = authenticator.authenticate(request_options(rpId="other.example.org"), "https://other.example.org")
    assert unb64url(elsewhere["id"]) == authenticator.credentials[2].credential_id


def test_a_discoverable_sign_in_with_several_passkeys_wants_the_persons_pick() -> None:
    authenticator = SoftAuthenticator()
    authenticator.register(creation_options(), ORIGIN)
    authenticator.register(creation_options(user={"id": b64url(b"second")}), ORIGIN)
    with pytest.raises(ValueError, match="credential_id="):
        authenticator.authenticate(request_options(), ORIGIN)


def test_a_sign_in_with_no_passkey_to_offer_is_refused_as_a_browser_would() -> None:
    authenticator = SoftAuthenticator()
    with pytest.raises(ValueError, match="no credential"):
        authenticator.authenticate(request_options(), ORIGIN)
    authenticator.register(creation_options(), ORIGIN)
    with pytest.raises(ValueError, match=r"no credential for 'other\.example\.org'"):
        authenticator.authenticate(request_options(rpId="other.example.org"), ORIGIN)
    unknown = [{"id": b64url(b"never registered"), "type": "public-key"}]
    with pytest.raises(ValueError, match="no credential"):
        authenticator.authenticate(request_options(allowCredentials=unknown), ORIGIN)


# ── Tampering ────────────────────────────────────────────────────────────────


def test_a_wrong_origin_is_the_origin_the_test_passes() -> None:
    authenticator = SoftAuthenticator()
    assert (
        client_data(authenticator.register(creation_options(), "https://evil.example"))["origin"]
        == "https://evil.example"
    )
    assert (
        client_data(authenticator.authenticate(request_options(), "https://evil.example"))["origin"]
        == "https://evil.example"
    )


def test_a_wrong_rp_id_is_hashed_into_the_data_and_the_credential_stays_with_the_right_one() -> None:
    authenticator = SoftAuthenticator()
    parts = attested(authenticator.register(creation_options(), ORIGIN, wrong_rp_id="evil.example"))
    assert parts.data.rp_id_hash == rp_hash("evil.example")
    assert authenticator.credentials[0].rp_id == RP_ID
    answer = authenticator.authenticate(request_options(), ORIGIN, wrong_rp_id="evil.example")
    assert authenticator_data(unb64url(answer["response"]["authenticatorData"])).rp_id_hash == rp_hash("evil.example")
    # Still the credential's own signature: only the hash is wrong, as a server must notice.
    assert signature_holds(public_key_of(parts.cose_key), answer)


def test_a_reused_or_lower_counter_is_signed_and_leaves_the_credentials_own() -> None:
    authenticator = SoftAuthenticator()
    key = public_key_of(attested(authenticator.register(creation_options(), ORIGIN)).cose_key)

    def count(answer: dict[str, Any]) -> int:
        return authenticator_data(unb64url(answer["response"]["authenticatorData"])).sign_count

    assert count(authenticator.authenticate(request_options(), ORIGIN)) == 1
    assert count(authenticator.authenticate(request_options(), ORIGIN)) == 2
    for replayed in (2, 0):
        answer = authenticator.authenticate(request_options(), ORIGIN, sign_count=replayed)
        assert count(answer) == replayed and signature_holds(key, answer)
    assert authenticator.credentials[0].sign_count == 2
    assert count(authenticator.authenticate(request_options(), ORIGIN)) == 3


# ── The extra ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["cbor2", "cryptography.hazmat.primitives"])
def test_the_module_names_its_extra_without_cbor2_or_cryptography(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """``None`` in ``sys.modules`` makes the import fail as if the package were not installed."""
    monkeypatch.delitem(sys.modules, "eifi1_server_kit.auth.soft_webauthn", raising=False)
    monkeypatch.setitem(sys.modules, missing, None)
    with pytest.raises(ImportError, match=r"install the kit with its passkeys extra, eifi1-server-kit\[passkeys\]"):
        importlib.import_module("eifi1_server_kit.auth.soft_webauthn")
