"""The report body's conventions: rework rounds and file lines (contract §3.4).

A rework is not a counter column — it is a block appended to the body, and the server
recognises the append and re-opens the row (see :mod:`eifi1_server_kit.feedback.rules`).
The appended shape is fixed::

    <old body>\\n\\n--- REWORK 2026-10-04 09:12 ---\\n<note>\\n[screenshot] /api/v1/feedback/attachments/<key>

Python twins of the kit's ``src/feedback/feedback-record.ts`` (``appendRework``,
``reworkCount``, ``splitBodyAttachments``), with the same regular expressions, so a body
the client wrote and a body a server migration wrote (Kurvenschmiede's ``note`` rows,
kastlan's folded comments) read the same on both sides.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_PREFIX

#: The rule a rework round opens with, matched against a whole TRIMMED line (kit
#: ``REWORK_RULE``). Anchored so "I did a REWORK --- honestly" is a sentence, not a round,
#: and so kastlan's folded ``--- COMMENT <stamp> · <name> ---`` blocks (§7.7) never count.
REWORK_RULE_RE = re.compile(r"^---\s*REWORK\b.*---$")

#: Any appended block's opening rule: a rework round or a folded kastlan comment.
BLOCK_RULE_RE = re.compile(r"^---\s*(?:REWORK|COMMENT)\b.*---$")

#: ``YYYY-MM-DD HH:MM``, UTC — ``new Date().toISOString().slice(0, 16).replace("T", " ")``
#: (keksdose ``feedback-page.tsx:351``).
REWORK_STAMP_FORMAT = "%Y-%m-%d %H:%M"

#: The marker of one file line. It says ``[screenshot]`` for a PDF or a text log too: the
#: name is history, the format is contract (keksdose live #291).
ATTACHMENT_MARKER = "[screenshot]"


def _attachment_line_re(prefix: str) -> re.Pattern[str]:
    # The kit's ATTACHMENT_LINE: this path and nothing else, alone on its (trimmed) line.
    return re.compile(rf"^\[screenshot\]\s+({re.escape(prefix)}[\w.-]+)\s*$", re.ASCII)


_DEFAULT_LINE_RE = _attachment_line_re(DEFAULT_ATTACHMENT_URL_PREFIX)


def _line_re(prefix: str) -> re.Pattern[str]:
    return _DEFAULT_LINE_RE if prefix == DEFAULT_ATTACHMENT_URL_PREFIX else _attachment_line_re(prefix)


def rework_stamp(now: datetime) -> str:
    """The stamp of a rework rule: ``now`` in UTC, to the minute. A naive ``now`` is read as UTC."""
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(REWORK_STAMP_FORMAT)


def attachment_line(url: str) -> str:
    """The body line for one uploaded file (kit ``feedbackAttachmentLine``)."""
    return f"{ATTACHMENT_MARKER} {url}"


def append_rework(
    body: str,
    note: str,
    url: str | None = None,
    *,
    now: datetime,
    prefix: str = DEFAULT_ATTACHMENT_URL_PREFIX,
) -> str | None:
    """``body`` with one rework round appended — the exact text the contract's PATCH carries.

    Mirrors the kit's ``appendRework``: the note is required and trimmed (a file alone
    cannot be sent — ``None`` for a blank note, as the kit returns ``null``); ONE file per
    round, as one ``[screenshot] <url>`` line; ``\\n\\n`` is left out when the old body is
    ``""``. The old body is kept byte for byte, so the result strictly extends it — which
    is what :func:`eifi1_server_kit.feedback.is_rework_append` recognises.

    Raises :class:`ValueError` for a ``url`` outside ``prefix``: written anyway it would sit
    in the body as prose no reader recognises as a file.
    """
    text = note.strip()
    if not text:
        return None
    if url and not _line_re(prefix).match(attachment_line(url)):
        raise ValueError(f"append_rework: not a feedback attachment URL: {url!r}")
    block = f"--- REWORK {rework_stamp(now)} ---\n{text}"
    if url:
        block = f"{block}\n{attachment_line(url)}"
    return f"{body}\n\n{block}" if body else block


def rework_count(body: str | None) -> int:
    """How many times a report has been sent back: its ``--- REWORK … ---`` rule lines.

    Derived, never stored (contract §3.1), so nothing can forget to set it. Folded kastlan
    comments do not count (kit ``reworkCount``).
    """
    return sum(1 for line in (body or "").split("\n") if REWORK_RULE_RE.match(line.strip()))


def split_body_attachments(body: str | None, *, prefix: str = DEFAULT_ATTACHMENT_URL_PREFIX) -> tuple[str, list[str]]:
    """``(text, urls)``: the body without its file lines (trailing blank lines dropped), and
    the URLs those lines named, in body order (kit ``splitBodyAttachments``)."""
    pattern = _line_re(prefix)
    urls: list[str] = []
    kept: list[str] = []
    for line in (body or "").split("\n"):
        match = pattern.match(line.strip())
        if match:
            urls.append(match.group(1))
        else:
            kept.append(line)
    return "\n".join(kept).rstrip("\n"), urls


def body_attachment_urls(body: str | None, *, prefix: str = DEFAULT_ATTACHMENT_URL_PREFIX) -> list[str]:
    """The URLs a body's ``[screenshot]`` lines name — a rework's files live there, not in the columns."""
    return split_body_attachments(body, prefix=prefix)[1]


def is_rework_append(old_body: str | None, changes: Mapping[str, Any]) -> bool:
    """Is this PATCH an APPEND to the body — the rework composer's shape?

    keksdose ``feedback_service.py:93`` ``_is_rework_append``, as the contract states it
    (§3.4): the changed fields are ``{"body"}`` and nothing else, and the new body strictly
    extends the old one, so the original is still there verbatim and first. A rewrite, a
    truncation, or a body change bundled with any other field (a status included) is not
    one. Asked of every actor, admins included (keksdose live #331).

    ``changes`` is what the client actually sent — ``payload.model_dump(exclude_unset=True)``.
    """
    if set(changes) != {"body"}:
        return False
    new_body = changes["body"]
    old = old_body or ""
    return isinstance(new_body, str) and len(new_body) > len(old) and new_body.startswith(old)


def appended_text(old_body: str | None, new_body: str) -> str:
    """What a rework append added (``""`` when ``new_body`` does not extend ``old_body``)."""
    old = old_body or ""
    return new_body[len(old) :] if new_body.startswith(old) else ""


def opens_with_rework_rule(old_body: str | None, new_body: str) -> bool:
    """Does the appended part open with a ``--- REWORK … ---`` rule line?

    A STRICTER test than the contract's append rule, for an app that wants to tell a
    rework from any other append (the contract does not ask it to; keksdose never has).
    """
    added = appended_text(old_body, new_body).lstrip("\n")
    first = added.split("\n", 1)[0]
    return REWORK_RULE_RE.match(first.strip()) is not None
