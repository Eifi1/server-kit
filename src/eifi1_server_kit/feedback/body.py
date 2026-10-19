"""The report body's conventions: rework rounds and file lines (contract §3.4).

**Legacy since round 0.40** (§10.2 decision 1): a rework is now an entry of its own
(``feedback_replies.kind = 'rework'``), and nothing is appended to the body any more.
:func:`split_rework_blocks` turns the old blocks into entries — each app's migration.
:func:`append_rework`, :func:`rework_count` and :func:`is_rework_append` stay for the
migration's downgrade, a client's fallback count and the coded 422 an old client's append
gets since 0.16 (:func:`~eifi1_server_kit.feedback.plan_update`; 0.13–0.15 converted it).

Before round 0.40 a rework was a block appended to the body, and the server recognised
the append and re-opened the row (see :mod:`eifi1_server_kit.feedback.rules`). The
appended shape is fixed::

    <old body>\\n\\n--- REWORK 2026-10-04 09:12 ---\\n<note>\\n[screenshot] /api/v1/feedback/attachments/<key>

Python twins of the kit's ``src/feedback/feedback-record.ts`` (``appendRework``,
``reworkCount``, ``splitBodyAttachments``), with the same regular expressions, so a body
the client wrote and a body a server migration wrote (Kurvenschmiede's ``note`` rows,
kastlan's folded comments) read the same on both sides.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_PREFIX

#: The rule a rework round opens with, matched against a whole TRIMMED line (kit
#: ``REWORK_RULE``). Anchored so "I did a REWORK --- honestly" is a sentence, not a round,
#: and so kastlan's folded ``--- COMMENT <stamp> · <name> ---`` blocks (§7.7) never count.
REWORK_RULE_RE = re.compile(r"^---\s*REWORK\b.*---$")

#: Any appended block's opening rule: a rework round or a folded kastlan comment.
BLOCK_RULE_RE = re.compile(r"^---\s*(?:REWORK|COMMENT)\b.*---$")

#: One block that :func:`split_rework_blocks` turns into an entry: exactly
#: :func:`append_rework`'s rule, matched against a whole TRIMMED line; the group is the stamp.
#: A line :data:`REWORK_RULE_RE` matches that this does not (``--- REWORK ANSWERED
#: 2026-09-16 ---``, ``--- REWORK 2026-09-04 ---``, ``--- REWORK (2) ---``) is no block.
REWORK_BLOCK_RE = re.compile(r"^--- REWORK (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ---$")

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
    """``body`` with one rework round appended — the exact text the contract's PATCH carried.

    Legacy (§10.3): a rework is an entry now; this is the shape a migration's downgrade
    writes back, and what an old client still sends (refused since 0.16).

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

    Legacy (§10.3): a report's reworks are its ``rework`` entries now; this counts an
    unmigrated body (the client's fallback without ``replies``).

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

    Legacy (§10.3): on an answered report such a PATCH is a
    :class:`~eifi1_server_kit.feedback.LegacyReworkAppendError` (coded 422, since 0.16;
    server-kit 0.13–0.15 turned it into a rework entry).

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


@dataclass(frozen=True, slots=True)
class ReworkBlock:
    """One rework round of an old body (§10.4): when it was sent (UTC, to the minute), what
    it says, and its one file — the block's first ``[screenshot]`` line — or ``None``."""

    stamp: datetime
    note: str
    url: str | None = None


def _rework_block(stamp: str, lines: list[str], pattern: re.Pattern[str]) -> ReworkBlock:
    url: str | None = None
    kept: list[str] = []
    for line in lines:
        match = pattern.match(line.strip()) if url is None else None
        if match:
            url = match.group(1)
        else:
            kept.append(line)
    at = datetime.strptime(stamp, REWORK_STAMP_FORMAT).replace(tzinfo=UTC)
    return ReworkBlock(stamp=at, note="\n".join(kept).strip(), url=url)


def split_rework_blocks(
    body: str | None, *, prefix: str = DEFAULT_ATTACHMENT_URL_PREFIX
) -> tuple[str, list[ReworkBlock]]:
    """``(description, blocks)``: a body without its rework rounds, and the rounds, in body
    order (§10.4) — what each app's migration writes as ``rework`` entries.

    A block opens on a line :data:`REWORK_BLOCK_RE` matches (after trimming) and runs to
    the next :data:`BLOCK_RULE_RE` line or the end; its first ``[screenshot]`` line under
    ``prefix`` is ``url``, the rest, trimmed, is ``note``. kastlan's folded ``--- COMMENT
    … ---`` blocks stay in the description as they are. The description loses its trailing
    whitespace; a body without a block comes back unchanged (``""`` for ``None``).

    Raises :class:`ValueError` on a line :data:`REWORK_RULE_RE` matches that is no block
    (``--- REWORK ANSWERED 2026-09-16 ---``, a date-only rule) or a stamp that is no date,
    so a migration stops rather than guess. Split ``body`` only, never ``outcome``:
    keksdose's ``outcome`` holds such lines from old loop answers.
    """
    text = body or ""
    pattern = _line_re(prefix)
    description: list[str] = []
    blocks: list[ReworkBlock] = []
    stamp: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):
        trimmed = line.strip()
        opened = REWORK_BLOCK_RE.match(trimmed)
        if opened is None and REWORK_RULE_RE.match(trimmed):
            raise ValueError(f"split_rework_blocks: a REWORK rule that is no block: {trimmed!r}")
        if stamp is not None and (opened is not None or BLOCK_RULE_RE.match(trimmed)):
            blocks.append(_rework_block(stamp, lines, pattern))
            stamp, lines = None, []
        if opened is not None:
            stamp = opened.group(1)
        elif stamp is not None:
            lines.append(line)
        else:
            description.append(line)
    if stamp is not None:
        blocks.append(_rework_block(stamp, lines, pattern))
    if not blocks:
        return text, []
    return "\n".join(description).rstrip(), blocks
