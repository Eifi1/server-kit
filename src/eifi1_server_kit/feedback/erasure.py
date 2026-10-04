"""Erasing an account keeps the person's reports and takes the person out of them (contract §3.2).

Lifted from keksdose ``backend/keksdose/domain/services/budgets_service.py:1031-1224``
(``_FEEDBACK_CONTEXT_KEEP``, ``_ERASED_MARK``, ``_scrub_text``, ``_scrub_context``, the
picture half of ``_anonymise_feedback``). NOT lifted: ``erase_user_account`` and its
foreign-key plan — which rows exist and in what order they go is each app's schema.

What an erased report keeps is the REPORT: title, body, category, status, outcome and
dates, with the person's own email / display name in the text replaced by
``[erased]``. What it loses: ``user_id``, ``crash_fingerprint`` (derived from the user id,
so a token for the person, not the defect), every context key outside the allow-list,
and every picture — ``screenshot_url``, ``attachment_urls`` AND the rework files named in
the body's ``[screenshot] <url>`` lines.

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
#: (a crash body's ``User: <email> (#<id>)`` line, keksdose ``_ERASED_MARK``).
ERASED_MARK = "[erased]"

#: Needles shorter than this are skipped: a 3-character display name ("Bob") appears inside
#: ordinary words, and the email is the identifier that matters.
MIN_NEEDLE_LENGTH = 4


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

    ``identifiers`` are the person's own strings — their email and display name. The body's
    ``[screenshot] <url>`` lines are stripped (the fix this module documents) BEFORE the
    scrub, and every URL the row named is returned in ``urls`` for
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
        "body": scrub_text(stripped, needles),
        "context": scrub_context(context),
        "crash_fingerprint": None,
        "screenshot_url": None,
        "attachment_urls": None,
        "user_id": None,
    }
    return AnonymisedFeedback(changes=changes, urls=frozenset(urls))


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
