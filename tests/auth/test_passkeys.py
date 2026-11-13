"""The passkey helpers (auth.passkeys) that the routes' tests do not reach."""

from __future__ import annotations

import pytest

from eifi1_server_kit.auth.passkeys import (
    PasskeyCheckError,
    RelyingParty,
    base64url_decode,
    base64url_encode,
    credential_id_of,
    registration_options,
    user_handle_of,
    verify_registration,
)

RP = RelyingParty(rp_id="app.example", rp_name="App", origins=("https://app.example",))


@pytest.mark.parametrize(
    ("rp_id", "origins"),
    [
        ("", ("https://app.example",)),
        ("App.Example", ("https://app.example",)),
        ("app.example", ()),
        ("app.example", ("app.example",)),
    ],
)
def test_a_relying_party_is_a_lower_case_domain_and_its_origins(rp_id: str, origins: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        RelyingParty(rp_id=rp_id, rp_name="App", origins=origins)


def test_base64url_round_trips_and_refuses_anything_else() -> None:
    for data in (b"", b"a", b"ab", b"abc", bytes(range(256))):
        assert base64url_decode(base64url_encode(data)) == data
    assert base64url_decode("YQ==") == b"a"
    for text in ("a b", "+/", "Y"):
        with pytest.raises(ValueError):
            base64url_decode(text)


def test_an_assertion_s_id_and_handle_are_read_carefully() -> None:
    with pytest.raises(PasskeyCheckError, match="no rawId"):
        credential_id_of({"rawId": ""})
    assert user_handle_of({"response": None}) is None
    assert user_handle_of({"response": {"userHandle": ""}}) is None
    assert user_handle_of({"response": {"userHandle": base64url_encode(b"42")}}) == 42
    for handle in (base64url_encode(b"forty-two"), base64url_encode(b"\xff\xfe")):
        with pytest.raises(PasskeyCheckError, match="not an account id"):
            user_handle_of({"response": {"userHandle": handle}})


def test_a_registration_without_transports_stores_none() -> None:
    from tests.auth.soft_webauthn import SoftAuthenticator

    options, challenge = registration_options(
        RP, user_id=7, user_name="ada@app.example", user_display_name="", exclude=[]
    )
    assert options["user"]["displayName"] == "ada@app.example"
    credential = SoftAuthenticator("https://app.example").create(options)
    credential["response"].pop("transports")
    assert verify_registration(RP, credential, challenge).transports is None
    credential["response"] = "nonsense"
    with pytest.raises(PasskeyCheckError):
        verify_registration(RP, credential, challenge)
