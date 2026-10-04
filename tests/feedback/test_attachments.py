"""The attachment URL policy: an app's own URLs and nothing else (contract §3.4, §3.5)."""

from __future__ import annotations

import re

import pytest

from eifi1_server_kit.feedback import (
    DEFAULT_ATTACHMENT_URL_POLICY,
    DEFAULT_ATTACHMENT_URL_PREFIX,
    OPAQUE_KEY_PATTERN,
    SHA12_KEY_PATTERN,
    UUID32_SHA12_KEY_PATTERN,
    AttachmentUrlPolicy,
    UnknownAttachmentUrlError,
)
from eifi1_server_kit.uploads import ACCEPTED_MEDIA_TYPES, content_addressed_key

PREFIX = "/api/v1/feedback/attachments/"
KEKSDOSE = AttachmentUrlPolicy(SHA12_KEY_PATTERN)
KASTLAN = AttachmentUrlPolicy(UUID32_SHA12_KEY_PATTERN)
#: Kurvenschmiede's own key, verbatim with its anchors (``schemas/feedback.py:56``).
KURVENSCHMIEDE = AttachmentUrlPolicy(r"^[a-f0-9]{12}\.(?:png|jpg|webp|gif|pdf|txt)$")


def test_the_url_pattern_is_the_download_routes_key_pattern() -> None:
    """keksdose ``test_feedback_service.py:368``: ``ATTACHMENT_URL_RE`` restated
    ``ATTACHMENT_KEY_RE``, and this pin kept a key the route would refuse from ever being
    stored. Here both are BUILT from one pattern; the pin stays."""
    for policy in (DEFAULT_ATTACHMENT_URL_POLICY, KEKSDOSE, KASTLAN, KURVENSCHMIEDE):
        assert policy.url_re.pattern == "^" + re.escape(policy.prefix) + policy.key_re.pattern.lstrip("^")


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/x.png",
        "https://evil.example/api/v1/feedback/attachments/aaaaaaaaaaaa.png",
        "//evil.example/api/v1/feedback/attachments/aaaaaaaaaaaa.png",
        "/api/v1/feedback/attachments/../../etc/passwd",
        "/api/v1/feedback/attachments/aaaaaaaaaaaa.svg",
        "/api/v1/feedback/attachments/",
        "/api/v1/other/aaaaaaaaaaaa.png",
        "",
    ],
)
def test_anything_but_our_own_route_is_refused(bad: str) -> None:
    """keksdose ``test_feedback_service.py:378``, for every app's pattern: a foreign absolute
    URL would carry the bearer token off-origin (§3.4 MUST)."""
    for policy in (DEFAULT_ATTACHMENT_URL_POLICY, KEKSDOSE, KASTLAN, KURVENSCHMIEDE):
        if bad.endswith(".svg") and policy is DEFAULT_ATTACHMENT_URL_POLICY:
            continue  # an opaque key is opaque: the download route, not the URL, knows the type
        assert not policy.is_own_url(bad), (policy.key_pattern, bad)
        with pytest.raises(UnknownAttachmentUrlError):
            policy.validate_url(bad)


def test_each_apps_keys() -> None:
    sha = "aaaaaaaaaaaa.png"
    kastlan = f"{'0' * 32}_{'b' * 12}.pdf"
    assert KEKSDOSE.is_own_url(PREFIX + sha) and KEKSDOSE.is_own_url(PREFIX + "aaaaaaaaaaaa.jpeg")
    assert not KEKSDOSE.is_own_url(PREFIX + kastlan)
    assert KASTLAN.is_own_url(PREFIX + kastlan) and not KASTLAN.is_own_url(PREFIX + sha)
    # Kurvenschmiede mints `jpg`, never `jpeg`.
    assert KURVENSCHMIEDE.is_own_url(PREFIX + "aaaaaaaaaaaa.jpg")
    assert not KURVENSCHMIEDE.is_own_url(PREFIX + "aaaaaaaaaaaa.jpeg")
    # Upper case is outside every hex class (keksdose test_feedback_attachments.py:102).
    for policy in (KEKSDOSE, KASTLAN, KURVENSCHMIEDE):
        assert not policy.is_key("ABCDEF123456.png")
    # The opaque default takes all three shapes.
    for key in (sha, kastlan, "1ed84e1bc3c7.webp"):
        assert DEFAULT_ATTACHMENT_URL_POLICY.is_own_url(PREFIX + key)


def test_the_opaque_default_is_still_a_key_and_never_a_path() -> None:
    policy = DEFAULT_ATTACHMENT_URL_POLICY
    assert policy.key_pattern == OPAQUE_KEY_PATTERN
    for bad in (".", "..", ".hidden", "a/b", "a b", "ä.png", "a\nb", "x.png\n"):
        assert not policy.is_key(bad), bad
        assert not policy.is_own_url(PREFIX + bad), bad


def test_every_accepted_type_yields_a_key_the_route_matches_again() -> None:
    """keksdose ``test_upload_guards.py:131``: a type added to the upload map and not to the
    key regex would upload fine and 404 on the way back."""
    for extension in set(ACCEPTED_MEDIA_TYPES.values()):
        key = content_addressed_key(b"bytes", extension)
        assert KEKSDOSE.is_key(key), key
        assert DEFAULT_ATTACHMENT_URL_POLICY.is_key(key), key


def test_keys_and_urls_round_trip() -> None:
    key = "abc123abc123.png"
    url = KEKSDOSE.url_for(key)
    assert url == PREFIX + key
    assert KEKSDOSE.key_of(url) == key
    assert KEKSDOSE.key_of("https://evil.example/" + key) is None
    with pytest.raises(UnknownAttachmentUrlError):
        KEKSDOSE.url_for("../secret.png")


def test_validate_urls_deduplicates_in_order() -> None:
    a, b = PREFIX + "aaaaaaaaaaaa.png", PREFIX + "bbbbbbbbbbbb.pdf"
    assert KEKSDOSE.validate_urls([a, b, a]) == [a, b]
    with pytest.raises(UnknownAttachmentUrlError):
        KEKSDOSE.validate_urls([a, "https://evil.example/x.png"])


def test_a_custom_prefix() -> None:
    policy = AttachmentUrlPolicy(SHA12_KEY_PATTERN, prefix="/api/v2/files/")
    assert policy.is_own_url("/api/v2/files/aaaaaaaaaaaa.png")
    assert not policy.is_own_url(DEFAULT_ATTACHMENT_URL_PREFIX + "aaaaaaaaaaaa.png")
    for bad in ("api/v1/", "/api/v1", "https://x.y/api/"):
        with pytest.raises(ValueError, match="prefix"):
            AttachmentUrlPolicy(prefix=bad)


def test_a_compiled_or_anchored_pattern_is_accepted() -> None:
    compiled = re.compile(r"^[0-9a-f]{32}_[0-9a-f]{12}\.(png|jpg|jpeg|webp|gif|pdf|txt)$")
    policy = AttachmentUrlPolicy(compiled.pattern)
    assert policy.is_key(f"{'0' * 32}_{'b' * 12}.png")
    assert not policy.is_key("x")
    # A trailing escaped dollar is part of the key, not an anchor; `\Z` is an anchor too.
    assert AttachmentUrlPolicy(r"a\$").is_key("a$")
    assert AttachmentUrlPolicy(r"^[a-f]{3}\Z").is_key("abc")


def test_a_trailing_newline_is_not_a_key() -> None:
    """Python's ``$`` matches before a trailing newline; keksdose's ``^…$`` key and URL patterns
    accept ``"<key>\\n"``. The policy anchors with ``\\Z``."""
    for policy in (DEFAULT_ATTACHMENT_URL_POLICY, KEKSDOSE, KASTLAN, KURVENSCHMIEDE):
        assert not policy.is_key("aaaaaaaaaaaa.png\n")
        assert not policy.is_own_url(PREFIX + "aaaaaaaaaaaa.png\n")
