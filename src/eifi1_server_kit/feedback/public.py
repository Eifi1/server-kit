"""Public feedback's rules as data (``docs/feedback-harmonization.md`` §9 in ``Eifi1/ui-kit``).

Round 0.38 opens the reports to the app's other users: they read a report's title and
its public body, say "I want this too", and comment; the admin's answer is a list of
replies. Layer 1: pure functions over plain values, no ports, no FastAPI.

* :func:`public_body` and :func:`public_text` — what a body says to someone who is
  neither its writer nor an admin: no file line, no attachment URL anywhere (P-b, P-e),
  and kastlan's folded comment headers without the name (9.4);
* :func:`is_public` — a report is public when its flag is set and it is no CRASH (9.3);
* :func:`feedback_recipients` — who hears of a new reply or comment (9.11);
* the notification type ids, the body bounds, and the three field lists of 9.10 that
  :func:`eifi1_server_kit.feedback.testing.assert_public_view` holds every public answer to.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from eifi1_server_kit.feedback.attachments import DEFAULT_ATTACHMENT_URL_POLICY, AttachmentUrlPolicy
from eifi1_server_kit.feedback.body import split_body_attachments
from eifi1_server_kit.feedback.enums import FeedbackCategory

__all__ = [
    "COMMENT_MAX",
    "FEEDBACK_ANSWERED",
    "FEEDBACK_COMMENT",
    "PUBLIC_COMMENT_FIELDS",
    "PUBLIC_PAGE_MAX",
    "PUBLIC_PAGE_SIZE",
    "PUBLIC_REPLY_FIELDS",
    "PUBLIC_REPORT_FIELDS",
    "REMOVED_URL",
    "REPLY_MAX",
    "feedback_recipients",
    "is_public",
    "public_body",
    "public_text",
]

#: The notification type a reply fires, to the author and the earlier commenters (9.11).
#: A reply only (``kind = 'reply'``): a rework entry sends nothing — no
#: ``feedback_answered``, commenters not told, no admin mail (§10.2 decision 10); the REWORK
#: status, or the Rework chip on an OPEN row, is the inbox's signal. An admin's reply with
#: status REWORK is a reply and fires it.
FEEDBACK_ANSWERED = "feedback_answered"
#: The notification type a visible comment fires, to the same people (9.11).
FEEDBACK_COMMENT = "feedback_comment"

#: A comment's body, after trimming: 1–4000 characters, plain text, no files (9.8).
COMMENT_MAX = 4000
#: A reply's body, after trimming: 1–8000 characters (9.7).
REPLY_MAX = 8000

#: The public list's page (9.9): 50 rows unless asked, at most 100.
PUBLIC_PAGE_SIZE = 50
PUBLIC_PAGE_MAX = 100

#: What a removed attachment URL leaves behind (9.4).
REMOVED_URL = "[…]"

#: What a report says to anyone but its author and an admin (9.10): the list item's
#: fields, and the detail's ``body`` (the public body) and ``replies``.
PUBLIC_REPORT_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "category",
        "status",
        "created_at",
        "updated_at",
        "resolved_at",
        "author_name",
        "mine",
        "votes",
        "voted",
        "comment_count",
        "body",
        "replies",
    }
)
#: What a comment says to anyone (9.10); ``body`` is null when hidden, but for its writer
#: and an admin.
PUBLIC_COMMENT_FIELDS: frozenset[str] = frozenset(
    {"id", "author_name", "mine", "staff", "body", "hidden", "created_at"}
)
#: What a reply or rework entry says in the public detail (9.10, §10.3): ``kind`` carried,
#: never ``status_from``, never ``attachment_url``.
PUBLIC_REPLY_FIELDS: frozenset[str] = frozenset({"id", "author_name", "body", "status_to", "created_at", "kind"})

#: kastlan's folded comment header (§7.7): ``--- COMMENT <stamp> · <name> ---``, the name
#: perhaps an address. Matched against a whole trimmed line.
_COMMENT_HEADER_RE = re.compile(r"^---\s*COMMENT\b\s*(?P<stamp>[^·]*?)\s*·.*---$")


def _url_re(policy: AttachmentUrlPolicy) -> re.Pattern[str]:
    # The app's prefix and key, wherever they stand, with the origin a browser's address
    # bar adds when a user copies the link (kk and ka serve a key to any signed-in user).
    return re.compile(rf"(?:https?://[^\s/]+)?{re.escape(policy.prefix)}(?:{policy.key_pattern})", re.ASCII)


def public_text(text: str | None, *, policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY) -> str:
    """``text`` with every attachment URL of ``policy`` removed, wherever it stands (9.4):
    each leaves ``[…]``. A path under another prefix is kept.

    Applied to every comment and reply body that leaves for someone other than its writer
    and an admin (P-e), and by :func:`public_body`.
    """
    return _url_re(policy).sub(REMOVED_URL, text or "")


def _rewrite_comment_header(line: str) -> str:
    match = _COMMENT_HEADER_RE.match(line.strip())
    if match is None:
        return line
    stamp = match.group("stamp").strip()
    return f"--- COMMENT {stamp} ---" if stamp else "--- COMMENT ---"


def public_body(body: str | None, *, policy: AttachmentUrlPolicy = DEFAULT_ATTACHMENT_URL_POLICY) -> str:
    """A report's body as the public detail shows it (9.4), applied by the server, never
    the client.

    Every ``[screenshot] <url>`` line is dropped (§3.4's pattern); every other attachment
    URL is removed wherever it stands (:func:`public_text`); kastlan's folded headers
    ``--- COMMENT <stamp> · <name> ---`` become ``--- COMMENT <stamp> ---``. The
    ``--- REWORK <stamp> ---`` lines and the notes stay: the author's own words.
    """
    text, _ = split_body_attachments(body, prefix=policy.prefix)
    return public_text("\n".join(_rewrite_comment_header(line) for line in text.split("\n")), policy=policy)


def is_public(category: FeedbackCategory | str, public: bool | None) -> bool:
    """Is a report in the public parts (9.3)? Its ``public`` flag is set and it is no
    CRASH: a crash's body carries the reporter's address, whatever its flag (decision 9)."""
    return bool(public) and FeedbackCategory(category) is not FeedbackCategory.CRASH


def feedback_recipients(
    *, author_id: int | None, commenter_ids: Iterable[int | None], writer_id: int | None
) -> list[int]:
    """Who hears of a new reply or comment (9.11): the report's author first, then each
    earlier commenter in the order of their first comment; never the writer, nobody twice,
    no ``None`` (an erased account). Never asked for a rework entry, which tells nobody
    (§10.2 decision 10).

    The app still drops anyone who may no longer see the report (a commenter outside
    ``public_viewer``, a report switched off) and any deactivated, erased or demo account.
    """
    out: list[int] = []
    for person in (author_id, *commenter_ids):
        if person is not None and person != writer_id and person not in out:
            out.append(person)
    return out
