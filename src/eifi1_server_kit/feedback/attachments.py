"""Which URLs a report may name as its files — this app's own, and nothing else.

The client fetches ``screenshot_url`` and every ``attachment_urls`` entry through the
authed client, WITH the bearer token (contract §3.4, §3.5). A foreign absolute URL stored
there would send the token off-origin the first time an admin opened the row, so both
fields MUST match the app's own download route — keksdose checked only
``attachment_urls`` (``schemas/feedback.py:33-36`` left ``screenshot_url`` an unvalidated
string); the kit checks both.

Keys are opaque to clients (§3.5) but each SERVER validates its own shape, so the key
pattern is a parameter:

* keksdose and Kurvenschmiede content-address: ``<sha12>.<ext>`` —
  :data:`SHA12_KEY_PATTERN` (keksdose ``upload_guards.py:237``); Kurvenschmiede's own
  list leaves out ``jpeg`` (``schemas/feedback.py:56``) and passes its own string;
* kastlan: ``<uuid32>_<sha12>.<ext>`` — :data:`UUID32_SHA12_KEY_PATTERN`
  (kastlan ``schemas/feedback.py:19``);
* :data:`OPAQUE_KEY_PATTERN` — the contract's client-side ``[\\w.-]+`` (kit
  ``feedback-record.ts:187``), minus a leading dot so ``.`` and ``..`` are never keys.
  The default, so an app that has not narrowed it still refuses every foreign URL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from eifi1_server_kit.feedback.errors import UnknownAttachmentUrlError

#: Where every app serves attachments (contract §3.3), the ``/api/v1`` prefix included,
#: because the stored URL is what a client GETs back.
DEFAULT_ATTACHMENT_URL_PREFIX = "/api/v1/feedback/attachments/"

_EXTENSIONS = "png|jpg|jpeg|webp|gif|pdf|txt"

#: Any opaque key: word characters, dots and dashes, not starting with a dot.
OPAQUE_KEY_PATTERN = r"[\w-][\w.-]*"
#: keksdose's (and, without ``jpeg``, Kurvenschmiede's) content-addressed key.
SHA12_KEY_PATTERN = rf"[a-f0-9]{{12}}\.(?:{_EXTENSIONS})"
#: kastlan's ``FileStoragePort`` key: ``<uuid4 hex>_<sha256[:12]>.<ext>``.
UUID32_SHA12_KEY_PATTERN = rf"[0-9a-f]{{32}}_[0-9a-f]{{12}}\.(?:{_EXTENSIONS})"


def _unanchored(pattern: str | re.Pattern[str]) -> str:
    """Accept a pattern with or without its anchors (kastlan's ``ATTACHMENT_KEY_RE.pattern`` has ``^…$``)."""
    text = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    text = text.removeprefix("^")
    if text.endswith("\\Z"):
        return text[:-2]
    if text.endswith("$") and not text.endswith("\\$"):
        return text[:-1]
    return text


@dataclass(frozen=True, slots=True)
class AttachmentUrlPolicy:
    """An app's attachment URL shape: ``prefix`` + a key matching ``key_pattern``.

    ``key_re`` is what the download route checks a key against (it is the only thing
    between a caller and the store path, keksdose ``feedback_router.py:90``), ``url_re``
    what a report's URLs are checked against — built from the SAME pattern, so a stored
    URL can never name a key the route would refuse (the pin keksdose's
    ``test_feedback_service.py:368`` keeps between its two hand-kept regexes).
    Matching is ASCII-only: ``\\w`` never admits a non-ASCII letter.
    """

    key_pattern: str = OPAQUE_KEY_PATTERN
    prefix: str = DEFAULT_ATTACHMENT_URL_PREFIX
    key_re: re.Pattern[str] = field(init=False, repr=False, compare=False)
    url_re: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.prefix.startswith("/") or not self.prefix.endswith("/"):
            raise ValueError(f"the prefix must be a path that starts and ends with '/': {self.prefix!r}")
        key = _unanchored(self.key_pattern)
        object.__setattr__(self, "key_pattern", key)
        # `\Z`, not `$`: Python's `$` also matches BEFORE a trailing newline, so
        # "<key>\n" passed keksdose's `^…$` patterns (`schemas/feedback.py:22`).
        object.__setattr__(self, "key_re", re.compile(rf"^(?:{key})\Z", re.ASCII))
        object.__setattr__(self, "url_re", re.compile(rf"^{re.escape(self.prefix)}(?:{key})\Z", re.ASCII))

    def is_key(self, key: str) -> bool:
        """Is ``key`` one this app minted — the download route's 404 guard."""
        return self.key_re.match(key) is not None

    def is_own_url(self, url: str) -> bool:
        """Is ``url`` one of this app's own attachment URLs (relative, this prefix, a valid key)?"""
        return self.url_re.match(url) is not None

    def key_of(self, url: str) -> str | None:
        """The key ``url`` names, or ``None`` when it is not one of this app's URLs."""
        return url.removeprefix(self.prefix) if self.is_own_url(url) else None

    def url_for(self, key: str) -> str:
        """The URL a stored key is served at; refuses a key outside the pattern."""
        if not self.is_key(key):
            raise UnknownAttachmentUrlError(f"not a feedback attachment key: {key!r}")
        return f"{self.prefix}{key}"

    def validate_url(self, url: str) -> str:
        """``url`` unchanged, or :class:`UnknownAttachmentUrlError` (→ 422)."""
        if not self.is_own_url(url):
            raise UnknownAttachmentUrlError(f"not a feedback attachment URL: {url!r}")
        return url

    def validate_urls(self, urls: Iterable[str]) -> list[str]:
        """Each URL checked, then de-duplicated in the order given.

        Keys are content-addressed in two apps, so the same picture pasted twice is the
        same URL twice — one chip, not two, is what the reporter meant (keksdose
        ``schemas/feedback.py:42-58`` ``_attachment_urls_are_ours``).
        """
        kept: list[str] = []
        for url in urls:
            self.validate_url(url)
            if url not in kept:
                kept.append(url)
        return kept


#: The policy every schema uses until an app narrows it.
DEFAULT_ATTACHMENT_URL_POLICY = AttachmentUrlPolicy()
