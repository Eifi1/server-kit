"""Erasing an account keeps the person's reports and takes the person out of them (contract §3.2).

Lifted from keksdose ``backend/keksdose/domain/services/budgets_service.py:1031-1224``
(``_FEEDBACK_CONTEXT_KEEP``, ``_ERASED_MARK``, ``_scrub_text``, ``_scrub_context``, the
picture half of ``_anonymise_feedback``). NOT lifted: ``erase_user_account`` and its
foreign-key plan — which rows exist and in what order they go is each app's schema.

What an erased report keeps is the REPORT: title, body, category, status, outcome and
dates, with the person's own email / display name in the text replaced by
``[erased]``. What it loses: ``user_id``, ``crash_fingerprint`` (derived from the user id,
so a token for the person, not the defect), every context key outside the allow-list, a
crash body's ``User:``, ``User agent:`` and ``Fingerprint:`` lines and its ``URL:`` past
the path (0.24, :func:`scrub_crash_diagnostics`),
and every picture — ``screenshot_url``, ``attachment_urls``, the rework files named in
the body's ``[screenshot] <url>`` lines AND, since 0.13, each rework entry's
``attachment_url`` (:func:`anonymise_reply`).

**The body-line fix.** keksdose cleared the two picture columns but left the rework
lines in the body, and their objects were never purged — a rework screenshot is as much a
picture of the person's own data as the capture is. :func:`anonymise_feedback` strips
those lines and reports their keys, and :func:`attachment_keys_to_purge` applies the same
shared-key check to them as to the columns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.body import split_body_attachments

#: The context keys an anonymised row KEEPS — an allow-list, never a deny-list: a
#: deny-list keeps every key nobody thought about, and those are the ones that name people.
#: Everything here describes the SOFTWARE at the moment of the report.
#:
#: ``environment`` stays (Marcel, 2026-10-04): which copy of the software, or an erased dev
#: report reads as production. ``origin`` goes although it answers the same question: on a
#: developer's machine it is a LAN address or an ``sslip.io`` name with an IP in it.
#: ``url`` goes (its query can carry what the person typed; ``route`` stays), ``ua`` goes
#: (a device fingerprint), as do ``fingerprint`` and every ``user_*`` key.
FEEDBACK_CONTEXT_KEEP: frozenset[str] = frozenset(
    {
        "route",
        "environment",
        "viewport",
        "version",
        "boundary",
        "online",
        "occurrences",
        "first_seen_at",
        "last_seen_at",
        "auto_reported",
    }
)

#: What replaces the erased person's own identifiers where they were written INTO the text
#: (keksdose ``_ERASED_MARK``). A crash body's ``User: <email> (#<id>)`` line goes whole
#: since 0.24 (:func:`scrub_crash_diagnostics`); the mark is for the rest of the text.
ERASED_MARK = "[erased]"

#: Needles shorter than this are skipped: a 3-character display name ("Bob") appears inside
#: ordinary words, and the email is the identifier that matters.
MIN_NEEDLE_LENGTH = 4

#: The heading of a crash body's diagnostics block (:func:`eifi1_server_kit.feedback.crash_body`);
#: the block runs from here to the first blank line.
CRASH_DIAGNOSTICS_HEADING = "-- Diagnostics --"

#: The diagnostics lines an erased crash report loses — the same three the context loses
#: (``user_*``, ``ua``, ``fingerprint``; keksdose legal audit 2026-11-11, C-7). The ``User:``
#: line goes WHOLE, not scrubbed: it holds the account number beside the address, and the
#: address of the day the crash was filed, which an erasure's identifiers (today's address
#: only) no longer find after a change.
CRASH_DIAGNOSTICS_DROP: tuple[str, ...] = ("User:", "User agent:", "Fingerprint:")

_URL_LINE_RE = re.compile(r"^(URL:\s+)(\S.*)$")


def scrub_text(text: str | None, needles: Iterable[str | None]) -> str:
    """Replace each needle wherever it appears, case-insensitively (keksdose ``_scrub_text``, ``:1135``).

    Only exact, known strings are removed. Free prose is left alone deliberately: a rule
    broad enough to catch a name somebody typed about themselves is broad enough to mangle
    the report, and an operator can still edit a self-identification out by hand.
    """
    out = text or ""
    for needle in needles:
        if not needle or len(needle) < MIN_NEEDLE_LENGTH:
            continue
        out = re.sub(re.escape(needle), ERASED_MARK, out, flags=re.IGNORECASE)
    return out


def _page_path(value: str) -> str:
    """A ``URL:`` line's value cut to its path. A placeholder (``(not captured)``) stays."""
    if value.startswith("("):
        return value
    return urlsplit(value).path or "/"


def scrub_crash_diagnostics(body: str | None) -> str:
    """A crash body without the diagnostics that name the person or the device (C-7).

    Inside the ``-- Diagnostics --`` block only: the ``User:``, ``User agent:`` and
    ``Fingerprint:`` lines go, and ``URL:`` keeps only its path — what the context keeps
    (``route``) and nothing of what it drops (``url``, ``origin``: a query can carry what
    the person typed, a developer's origin an IP). The message, the stack and everything
    else stay as they were; a body without the block comes back unchanged, so a hand-filed
    report with a line that starts "User:" keeps it.

    :func:`anonymise_feedback` applies it (0.24). Rows erased before 0.24 — a crash report
    whose ``user_id`` is already NULL — still have the three lines: run it once over their
    bodies. A second pass changes nothing.
    """
    out: list[str] = []
    inside = False
    for line in (body or "").split("\n"):
        if line.strip() == CRASH_DIAGNOSTICS_HEADING:
            inside = True
        elif inside and not line.strip():
            inside = False
        elif inside:
            if line.startswith(CRASH_DIAGNOSTICS_DROP):
                continue
            match = _URL_LINE_RE.match(line)
            if match:
                line = match.group(1) + _page_path(match.group(2))
        out.append(line)
    return "\n".join(out)


def scrub_context(context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Keep the diagnostics, drop everything that names the reporter (keksdose ``_scrub_context``, ``:1156``).

    A NEW dict (a JSON column has no mutation tracking); ``None`` when nothing is left.
    """
    if not context:
        return None
    kept = {key: value for key, value in context.items() if key in FEEDBACK_CONTEXT_KEEP}
    return kept or None


def feedback_attachment_urls(
    *,
    screenshot_url: str | None,
    attachment_urls: Iterable[str] | None,
    body: str | None,
    policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
) -> set[str]:
    """Every attachment URL one report names — both columns AND the body's rework lines."""
    urls = {screenshot_url} if screenshot_url else set()
    urls.update(attachment_urls or ())
    urls.update(split_body_attachments(body, prefix=policy.prefix)[1])
    return urls


@dataclass(frozen=True, slots=True)
class AnonymisedFeedback:
    """The columns to assign (``changes``) and the attachment URLs the row named (``urls``)."""

    changes: dict[str, Any]
    urls: frozenset[str]


def anonymise_feedback(
    *,
    title: str | None,
    body: str | None,
    context: Mapping[str, Any] | None,
    screenshot_url: str | None,
    attachment_urls: Iterable[str] | None,
    identifiers: Iterable[str | None],
    policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
) -> AnonymisedFeedback:
    """One report with its author taken out (keksdose ``_anonymise_feedback`` row loop, ``:1201-1214``).

    ``identifiers`` are the person's own strings — build them with
    :func:`eifi1_server_kit.auth.erasure_identifiers` (the email, the old display name, the
    full name in every order, longest first; never a bare first or last name). The body's
    ``[screenshot] <url>`` lines are stripped (the fix this module documents) BEFORE the
    scrub, as are a crash body's personal diagnostics (:func:`scrub_crash_diagnostics`,
    0.24), and every URL the row named is returned in ``urls`` for
    :func:`attachment_keys_to_purge`. ``changes`` sets ``user_id``, ``crash_fingerprint``,
    ``screenshot_url`` and ``attachment_urls`` to ``None``.
    """
    needles = list(identifiers)
    stripped, _ = split_body_attachments(body, prefix=policy.prefix)
    urls = feedback_attachment_urls(
        screenshot_url=screenshot_url, attachment_urls=attachment_urls, body=body, policy=policy
    )
    changes: dict[str, Any] = {
        "title": scrub_text(title, needles),
        "body": scrub_text(scrub_crash_diagnostics(stripped), needles),
        "context": scrub_context(context),
        "crash_fingerprint": None,
        "screenshot_url": None,
        "attachment_urls": None,
        "user_id": None,
    }
    return AnonymisedFeedback(changes=changes, urls=frozenset(urls))


def anonymise_reply(
    *, body: str | None, attachment_url: str | None, identifiers: Iterable[str | None]
) -> AnonymisedFeedback:
    """One entry of ``feedback_replies`` with its writer taken out (§9.12, §10.3): kept,
    ``author_id`` ``None``, the body scrubbed (:func:`scrub_text`). A rework entry's file
    goes as the report's do: ``attachment_url`` set to ``None`` and the URL answered in
    ``urls`` for :func:`attachment_keys_to_purge` (an entry without one is not written
    there)."""
    changes: dict[str, Any] = {"body": scrub_text(body, identifiers), "author_id": None}
    if attachment_url:
        changes["attachment_url"] = None
    return AnonymisedFeedback(changes=changes, urls=frozenset({attachment_url} if attachment_url else ()))


def attachment_keys_to_purge(
    erased_urls: Iterable[str],
    other_urls: Iterable[str],
    *,
    policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY,
) -> list[str]:
    """The keys whose objects may be deleted, sorted (keksdose ``:1182-1222``).

    Content-addressed keys carry no user, so two people who upload byte-identical files
    share one object: a URL any OTHER report still names — in a column or in its body
    (collect those with :func:`feedback_attachment_urls`) — is kept. URLs that are not this
    app's own are never purge candidates. The purge itself is the app's (keksdose prefixes
    ``feedback/`` and hands the list to its object store after the commit).
    """
    shared = set(other_urls)
    keys = {policy.key_of(url) for url in set(erased_urls) - shared}
    return sorted(key for key in keys if key is not None)
