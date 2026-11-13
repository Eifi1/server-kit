"""Passkeys (WebAuthn): the two ceremonies' options and checks, over py_webauthn.

``docs/auth-harmonization.md`` §12.3 and §12.5 item 8 in ``Eifi1/ui-kit``. The cryptography —
attestation, signature, origin, RP id, the flags — is py_webauthn's, behind the ``passkeys``
extra (``eifi1-server-kit[passkeys]``) and imported only when a ceremony runs, so an app
without passkeys installs nothing (the ``gcs`` extra's pattern, :mod:`eifi1_server_kit.objects`).
:class:`~eifi1_server_kit.auth.service.AccountService` owns the orchestration around these:
the challenge tokens, the store, the one use per challenge, the session.

The rules the kit settles for every app (§12.5 item 8), each one app's today:

* **Usernameless sign-in only** (Kurvenschmiede): the options carry no allow-list, and the
  registration demands a discoverable credential (``resident_key`` required), so the
  browser offers every passkey it holds for the RP and the answer names the account. An
  address hint would make the first step say something about the account behind it.
* **User verification required**, in both ceremonies and in the check
  (``require_user_verification``): a passkey sign-in then proves the device AND its PIN or
  biometric, so it counts as both factors and a person with 2FA is not asked for a code
  after it. No app required it before (keksdose's 2026-11-13 change is the model).
* **One use per challenge** through the store's ``spend_challenge`` (Kurvenschmiede's
  table): the signature counter cannot be the replay guard, because a synced passkey's
  stays at zero.

``extensions`` (the ``passkey_extensions`` hook) are merged into the options as they are:
keksdose's ``prf`` for its private mode. The kit reads no extension output.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_PASSKEY_NAME",
    "PasskeyCeremony",
    "PasskeyCheckError",
    "RelyingParty",
    "VerifiedPasskey",
    "authentication_options",
    "base64url_decode",
    "base64url_encode",
    "credential_id_of",
    "registration_options",
    "rp_id_fits",
    "user_handle_of",
    "verify_authentication",
    "verify_registration",
]

#: What a passkey added with a blank name is called (Kurvenschmiede ``DEFAULT_NAME``): the
#: same word in every language the apps speak.
DEFAULT_PASSKEY_NAME = "Passkey"

#: Which ceremony a ``passkey_extensions`` hook is asked for.
type PasskeyCeremony = Literal["registration", "authentication"]


def rp_id_fits(rp_id: str, origin: str) -> bool:
    """Is ``rp_id`` the host of ``origin``, or a suffix of it at a dot? The browser's rule for
    a ceremony on that page (``staunlabor.app`` fits ``https://staunlabor.app`` and
    ``https://www.staunlabor.app``; ``localhost`` fits ``http://localhost:5176`` and not
    ``http://192.168.1.20:5176``), held once for :class:`RelyingParty` and an app's boot
    check, which may only warn on a developer's machine (Staunlabor's and Kurvenschmiede's
    ``webauthn_rp_id_fits``, 0.29).

    It does not know the public suffix list (``app`` alone fits ``https://staunlabor.app``
    here and is refused by every browser): that refusal is the browser's, and a second copy
    of the list would be a second, wrong one. An unreadable origin fits nothing."""
    rp_id = rp_id.strip().lower()
    try:
        host = (urlsplit(origin).hostname or "").rstrip(".")
    except ValueError:
        return False
    return bool(rp_id) and (host == rp_id or host.endswith("." + rp_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class RelyingParty:
    """The app as WebAuthn knows it: ``rp_id`` its registrable domain (``staunlabor.app``;
    ``localhost`` in development), ``rp_name`` what the authenticator shows, and ``origins``
    the exact page origins an answer may come from (``https://staunlabor.app``).

    Since 0.30 every origin must fit the RP id (:func:`rp_id_fits`), or construction is a
    :class:`ValueError` naming each that does not: a browser refuses every ceremony on such a
    page, in the person's browser and in no log, so the mistake is caught where it is made.
    An app whose development page does not fit (a dev server opened over the LAN) asks
    :func:`rp_id_fits` first and builds no relying party there; its passkey routes stay
    unmounted and the password still works."""

    rp_id: str
    rp_name: str
    origins: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.rp_id.strip() or self.rp_id != self.rp_id.strip().lower():
            raise ValueError(f"rp_id is a lower-case domain: {self.rp_id!r}")
        if not self.origins or not all(origin.startswith(("https://", "http://")) for origin in self.origins):
            raise ValueError("origins are one or more http(s) origins")
        misfits = [origin for origin in self.origins if not rp_id_fits(self.rp_id, origin)]
        if misfits:
            raise ValueError(
                f"rp_id {self.rp_id!r} does not fit {', '.join(map(repr, misfits))}: it is neither"
                " the origin's host nor a suffix of it at a dot, so no browser runs a passkey ceremony there"
            )


class PasskeyCheckError(ValueError):
    """A ceremony's answer did not verify. The message is py_webauthn's or the kit's, for the
    log only: it is attacker-influenced text, never an answer."""


@dataclass(frozen=True, slots=True)
class VerifiedPasskey:
    """A registration that verified: what the store keeps."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    #: Comma-separated, as the browser reported them; ``None`` when it did not.
    transports: str | None


def base64url_encode(data: bytes) -> str:
    """Unpadded base64url, as WebAuthn writes every binary field (and a challenge claim)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def base64url_decode(text: str) -> bytes:
    """The bytes of unpadded (or padded) base64url; :class:`ValueError` for anything else."""
    if not isinstance(text, str) or any(char not in _BASE64URL for char in text.rstrip("=")):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text.rstrip("=") + "=" * (-len(text.rstrip("=")) % 4))
    except binascii.Error as exc:
        raise ValueError("not base64url") from exc


_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _webauthn() -> Any:
    """py_webauthn, or an :class:`ImportError` naming the extra."""
    try:
        import webauthn
    except ImportError as exc:
        raise ImportError(
            "The passkey routes need py_webauthn: install the kit with its passkeys extra, eifi1-server-kit[passkeys]."
        ) from exc
    return webauthn


def _refusals() -> tuple[type[Exception], ...]:
    """Every exception py_webauthn raises for an answer that does not verify, and the plain
    ones its parsing lets through: 3.x gives them one base, 2.x none, so the module's
    classes are collected (a malformed credential must be a refusal, never a 500)."""
    from webauthn.helpers import exceptions

    own = tuple(
        value
        for value in vars(exceptions).values()
        if isinstance(value, type) and issubclass(value, Exception) and value.__module__ == exceptions.__name__
    )
    return (*own, ValueError, KeyError, TypeError)


def _with_extensions(options_json: str, extensions: Mapping[str, Any] | None) -> dict[str, Any]:
    options: dict[str, Any] = json.loads(options_json)
    if extensions:
        options["extensions"] = {**options.get("extensions", {}), **extensions}
    return options


def registration_options(
    rp: RelyingParty,
    *,
    user_id: int,
    user_name: str,
    user_display_name: str,
    exclude: Sequence[bytes],
    extensions: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """``(options, challenge)`` for ``navigator.credentials.create``: a discoverable
    credential with user verification required, the account's existing passkeys excluded (an
    authenticator that holds one says so instead of minting a second). ``user_id`` goes in
    as the user handle, the account id's decimal digits (keksdose, Kurvenschmiede)."""
    webauthn = _webauthn()
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    options = webauthn.generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=rp.rp_name,
        user_id=str(user_id).encode(),
        user_name=user_name,
        user_display_name=user_display_name or user_name,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=raw) for raw in exclude],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    return _with_extensions(webauthn.options_to_json(options), extensions), bytes(options.challenge)


def authentication_options(
    rp: RelyingParty, *, extensions: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any], bytes]:
    """``(options, challenge)`` for ``navigator.credentials.get``: no allow-list (the
    usernameless sign-in), user verification required."""
    webauthn = _webauthn()
    from webauthn.helpers.structs import UserVerificationRequirement

    options = webauthn.generate_authentication_options(
        rp_id=rp.rp_id, user_verification=UserVerificationRequirement.REQUIRED
    )
    return _with_extensions(webauthn.options_to_json(options), extensions), bytes(options.challenge)


def verify_registration(rp: RelyingParty, credential: Mapping[str, Any], challenge: bytes) -> VerifiedPasskey:
    """The registration's answer, checked against ``challenge``, the RP and its origins, with
    user verification required; :class:`PasskeyCheckError` for any fault."""
    webauthn = _webauthn()
    try:
        verified = webauthn.verify_registration_response(
            credential=dict(credential),
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=list(rp.origins),
            require_user_verification=True,
        )
    except _refusals() as exc:
        raise PasskeyCheckError(f"the registration did not verify: {exc}") from exc
    response = credential.get("response")
    reported = response.get("transports") if isinstance(response, Mapping) else None
    transports = ",".join(str(item) for item in reported) if isinstance(reported, list) and reported else None
    return VerifiedPasskey(
        credential_id=bytes(verified.credential_id),
        public_key=bytes(verified.credential_public_key),
        sign_count=int(verified.sign_count),
        transports=transports,
    )


def credential_id_of(credential: Mapping[str, Any]) -> bytes:
    """The raw credential id an assertion names (``rawId``, base64url);
    :class:`PasskeyCheckError` when it is missing or malformed."""
    raw = credential.get("rawId")
    if not isinstance(raw, str) or not raw:
        raise PasskeyCheckError("the credential names no rawId")
    try:
        return base64url_decode(raw)
    except ValueError as exc:
        raise PasskeyCheckError("the credential's rawId is malformed") from exc


def user_handle_of(credential: Mapping[str, Any]) -> int | None:
    """The account id an assertion's user handle names, ``None`` when it carries none;
    :class:`PasskeyCheckError` for one that is not an id this app wrote. A cross-check: the
    credential row is the binding (Kurvenschmiede)."""
    response = credential.get("response")
    handle = response.get("userHandle") if isinstance(response, Mapping) else None
    if not handle:
        return None
    try:
        text = base64url_decode(str(handle)).decode()
        if not text.isdigit():
            raise ValueError(text)
        return int(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PasskeyCheckError("the user handle is not an account id") from exc


def verify_authentication(
    rp: RelyingParty,
    credential: Mapping[str, Any],
    challenge: bytes,
    *,
    public_key: bytes,
    sign_count: int,
) -> int:
    """A sign-in's answer, checked against ``challenge``, the RP, its origins and the stored
    key, with user verification required; the new signature counter, or
    :class:`PasskeyCheckError`."""
    webauthn = _webauthn()
    try:
        verified = webauthn.verify_authentication_response(
            credential=dict(credential),
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=list(rp.origins),
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=True,
        )
    except _refusals() as exc:
        raise PasskeyCheckError(f"the assertion did not verify: {exc}") from exc
    return int(verified.new_sign_count)
