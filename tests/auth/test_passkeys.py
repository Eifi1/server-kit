"""The passkey helpers (auth.passkeys) that the routes' tests do not reach."""

from __future__ import annotations

import logging

import pytest

from eifi1_server_kit.auth.passkeys import (
    PasskeyCheckError,
    RelyingParty,
    base64url_decode,
    base64url_encode,
    credential_id_of,
    registration_options,
    rp_id_fits,
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


@pytest.mark.parametrize(
    ("rp_id", "origin"),
    [
        ("app.example", "https://app.example"),
        ("app.example", "https://www.app.example"),
        ("app.example", "https://APP.example:8443/path"),
        ("localhost", "http://localhost:5176"),
        ("staunlabor.app", "https://staunlabor.app"),
    ],
)
def test_an_rp_id_fits_its_origin_s_host_or_a_suffix_of_it_at_a_dot(rp_id: str, origin: str) -> None:
    assert rp_id_fits(rp_id, origin) is True


@pytest.mark.parametrize(
    ("rp_id", "origin"),
    [
        ("app.example", "https://other.example"),
        ("app.example", "https://evilapp.example"),  # a suffix, but not at a dot
        ("www.app.example", "https://app.example"),  # longer than the host
        ("localhost", "http://192.168.1.20:5176"),  # a dev server reached over the LAN
        ("app.example", "https://app.example.evil"),
        ("", "https://app.example"),
        ("app.example", "https://"),
        ("app.example", "http://[::1"),
    ],
)
def test_an_rp_id_that_is_not_its_origin_s_host_nor_a_suffix_does_not_fit(rp_id: str, origin: str) -> None:
    assert rp_id_fits(rp_id, origin) is False


def test_a_relying_party_whose_rp_id_does_not_fit_an_origin_is_built_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A browser refuses every ceremony for such an origin, in the person's browser and in no
    log, so the construction writes the log line; it still builds, as 0.29 did: a dev server
    opened over the LAN starts, and its passwords work."""
    origins = ("https://app.example", "https://other.example", "http://192.168.1.20:5176")
    with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.auth.passkeys"):
        rp = RelyingParty(rp_id="app.example", rp_name="App", origins=origins)
    assert rp.origins == origins
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "'app.example'" in record.getMessage()
    assert "'https://other.example', 'http://192.168.1.20:5176'" in record.getMessage()
    assert "'https://app.example'" not in record.getMessage()


def test_every_origin_under_the_rp_id_is_a_relying_party_s_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="eifi1_server_kit.auth.passkeys"):
        both = RelyingParty(
            rp_id="app.example", rp_name="App", origins=("https://app.example", "https://www.app.example")
        )
        RelyingParty(rp_id="localhost", rp_name="App", origins=("http://localhost:5176",))
    assert both.origins == ("https://app.example", "https://www.app.example")
    assert caplog.records == []


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
