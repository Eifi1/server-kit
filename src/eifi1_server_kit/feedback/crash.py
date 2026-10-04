"""Auto-filed crash reports (contract §3.6): fingerprint, row text, fold-or-file.

Lifted from keksdose ``feedback_service.py:199-370``. The QUERY stays in each app (RLS,
kastlan's ``company_id``): the app looks up the newest row with the fingerprint, and the
kit decides what to do with it::

    fingerprint = crash_fingerprint(payload, user.id)
    seen_at = crash_seen_at(payload)
    candidate = await newest_row_with(fingerprint)          # app-side
    if decide_crash(candidate.status if candidate else None) is CrashDecision.FOLD:
        candidate.context = fold_crash_context(candidate.context, seen_at)   # duplicate
    else:
        row = Feedback(
            title=crash_title(payload),
            body=crash_body(payload, user_id=..., user_email=..., fingerprint=fingerprint, seen_at=seen_at),
            context=crash_context(payload, user_id=..., user_email=..., user_display_name=...,
                                  fingerprint=fingerprint, seen_at=seen_at),
            category=FeedbackCategory.CRASH, status=FeedbackStatus.OPEN, crash_fingerprint=fingerprint,
        )
    return CrashReportResponse(stored=True, duplicate=..., feedback_id=..., reference=crash_reference(fingerprint))

**The fingerprint is byte-identical to keksdose's** (pinned by golden vectors computed
from keksdose's own code): any drift would mint a new fingerprint for every crash already
open there, and the next occurrence of each would file a twin instead of folding.

**The limiter.** keksdose charges its crash limiter BEFORE the dedupe
(``feedback_router.py:155``), so repeats count too and ``occurrences`` stops rising after
20 in an hour; the contract recommends charging after the dedupe (only new rows count).
Which one is the app's choice — the kit has no router — and keksdose keeps its order.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from eifi1_server_kit.feedback.enums import TERMINAL_STATUSES, FeedbackStatus
from eifi1_server_kit.feedback.schemas import MAX_TITLE_LENGTH, CrashReportCreate

#: Hex runs first: a minified chunk name ("index-A1b2C3.js") is all hex characters, so
#: folding digits first would break it up and leave it un-normalised (keksdose ``:205-209``).
_HEX_RUN_RE = re.compile(r"\b[0-9a-fA-F]{6,}\b")
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

#: How many characters of the fingerprint the client is shown to quote.
REFERENCE_LENGTH = 8


def normalise_crash_text(text: str) -> str:
    """Fold what differs between two occurrences of the SAME defect (keksdose ``_normalise``, ``:212``).

    Hex runs of 6+ (build hashes, chunk names, uuids) then digit runs (ids, offsets, line
    numbers) become ``#``; whitespace folds to one space; the ends are stripped. Without it
    "Loading chunk 1234 failed" and "Loading chunk 5678 failed" are two entries per visitor.
    """
    folded = _HEX_RUN_RE.sub("#", text)
    folded = _DIGITS_RE.sub("#", folded)
    return _WS_RE.sub(" ", folded).strip()


def crash_fingerprint(payload: CrashReportCreate, user_id: int | str) -> str:
    """Same user, build, route, error and top of stack → same key (keksdose ``_fingerprint``, ``:228``).

    ``sha256`` over ``\\x1f``-joined ``str(user_id)``, version, route, name, the normalised
    message and the first 3 normalised stack lines, cut to 32 hex characters. Per user by
    design — two people on the same broken page are two reports. Only 3 frames, because
    deeper ones drift with React's call path. ``origin`` / ``environment`` take no part.
    """
    frames = [normalise_crash_text(line) for line in (payload.stack or "").splitlines()[:3]]
    parts = [
        str(user_id),
        payload.version or "",
        payload.route or "",
        payload.name or "",
        normalise_crash_text(payload.message),
        *frames,
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def crash_reference(fingerprint: str | None) -> str | None:
    """The first 8 fingerprint characters, or ``None`` (keksdose ``feedback_router.py:166``)."""
    return (fingerprint or "")[:REFERENCE_LENGTH] or None


def truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the last one an ellipsis when it was cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def crash_title(payload: CrashReportCreate) -> str:
    """``[crash] <name>: <message>``, at most 255 characters and never blank (keksdose ``:254``).

    ``feedback.title`` is ``String(255)`` while a message may be 2000 characters: an
    untruncated title is a 500 on INSERT — the crash reporter crashing.
    """
    name = (payload.name or "").strip() or "Error"
    message = payload.message.strip() or "(no message)"
    return truncate(f"[crash] {name}: {message}", MAX_TITLE_LENGTH)


def crash_seen_at(payload: CrashReportCreate, now: datetime | None = None) -> str:
    """When it happened, ISO 8601: the client's ``occurred_at``, else ``now`` (UTC)."""
    return (payload.occurred_at or now or datetime.now(UTC)).isoformat()


def crash_body(
    payload: CrashReportCreate,
    *,
    user_id: int | str,
    user_email: str,
    fingerprint: str,
    seen_at: str | None = None,
) -> str:
    """The diagnostics block, as plain greppable text (keksdose ``_crash_body``, ``:262``).

    The ``User:`` line embeds the address — which is why erasure scrubs bodies too
    (:func:`eifi1_server_kit.feedback.anonymise_feedback`).
    """
    where = "the whole app shell" if payload.boundary == "app" else "a single page"
    online = "yes" if payload.online else "no" if payload.online is not None else "(unknown)"
    lines = [
        f"{payload.name or 'Error'}: {payload.message}",
        "",
        "-- Diagnostics --",
        f"Boundary:    {payload.boundary} ({where} went down)",
        f"Route:       {payload.route or '(unknown)'}",
        f"URL:         {payload.url or '(not captured)'}",
        f"Occurred at: {seen_at or crash_seen_at(payload)}",
        f"App version: {payload.version or '(unknown)'}",
        f"Viewport:    {payload.viewport or '(unknown)'}",
        f"Online:      {online}",
        f"User:        {user_email} (#{user_id})",
        f"User agent:  {payload.ua or '(not captured)'}",
        f"Fingerprint: {fingerprint}",
        "",
        "-- Stack --",
        payload.stack or "(no stack captured)",
        "",
        "-- Component stack --",
        payload.component_stack or "(no component stack captured)",
    ]
    return "\n".join(lines)


def crash_context(
    payload: CrashReportCreate,
    *,
    user_id: int | str,
    user_email: str | None,
    user_display_name: str | None,
    fingerprint: str,
    seen_at: str,
) -> dict[str, Any]:
    """A new crash row's ``context`` (keksdose ``:346-366``; contract §3.2).

    The dialog's key set — so the admin view renders a crash like a hand-filed report,
    ``origin`` and ``environment`` included (the env chip) — plus what only an automatic
    report has: ``fingerprint``, ``boundary``, ``online``, ``occurrences``,
    ``first_seen_at``, ``last_seen_at`` and ``auto_reported: true``.
    """
    return {
        "url": payload.url or "",
        "route": payload.route or "",
        "origin": payload.origin or "",
        "environment": payload.environment or "",
        "user_id": user_id,
        "user_email": user_email,
        "user_display_name": user_display_name,
        "viewport": payload.viewport or "",
        "ua": payload.ua or "",
        "version": payload.version or "",
        "fingerprint": fingerprint,
        "boundary": payload.boundary,
        "online": payload.online,
        "occurrences": 1,
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "auto_reported": True,
    }


class CrashDecision(enum.Enum):
    """What to do with a crash whose fingerprint the app has looked up."""

    #: Count it on the existing row: ``occurrences + 1``, ``last_seen_at`` (``duplicate: true``).
    FOLD = "fold"
    #: File a new CRASH row.
    FILE = "file"


def decide_crash(candidate_status: FeedbackStatus | str | None) -> CrashDecision:
    """Fold into the open row, or file a new one.

    ``candidate_status`` is the status of the row the APP'S QUERY found: the newest row with
    the same fingerprint whose status is NOT terminal — keksdose ``record_crash``
    (``feedback_service.py:312-318``) filters ``status NOT IN TERMINAL_STATUSES`` in the
    query itself, so an older re-opened row is counted even when a newer one with the same
    fingerprint was settled. Passing the newest row of ANY status instead files a duplicate
    in exactly that case (kastlan's finding). With keksdose's query the candidate always
    folds; ``None`` (no open row) files a new one — a fixed crash coming back is news, and
    hiding it inside a resolved entry is how a regression goes unnoticed. Any non-terminal
    status folds, IN_EVALUATION included.
    """
    if candidate_status is None or FeedbackStatus(candidate_status) in TERMINAL_STATUSES:
        return CrashDecision.FILE
    return CrashDecision.FOLD


def fold_crash_context(context: Mapping[str, Any] | None, seen_at: str) -> dict[str, Any]:
    """The existing row's context with one more occurrence (keksdose ``:322-331``).

    A NEW dict, never a mutation: a plain JSON column has no mutation tracking, so an
    in-place edit is never written and the counter would sit at 1 forever.
    """
    folded = {**(context or {})}
    folded["occurrences"] = int(folded.get("occurrences") or 1) + 1
    folded["last_seen_at"] = seen_at
    return folded
