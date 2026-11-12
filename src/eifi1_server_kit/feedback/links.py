"""Where a feedback notice opens (``docs/feedback-harmonization.md`` §9.11, §13.7 in ``Eifi1/ui-kit``).

The feedback pages read two parameters (ui-kit 0.59): ``?row=<id>`` opens one report, and
``?ids=<id>,<id>`` on ``/feedback`` and ``/my-feedback`` narrows the table to a SET of
reports, in place of the filters the person's list last kept — each id digits with no
leading zero and a safe integer, each once; anything else names no set. A notice links
its reports with them:

* the admins' alert, ``feedback_received`` (§11) — :func:`feedback_inbox_href`:
  ``/feedback?ids=123&row=123``, a crash bundle ``/feedback?ids=7,8,9``;
* a reply or a comment on the person's OWN report (§9.11) — :func:`my_feedback_href`:
  ``/my-feedback?ids=12&row=12``, a bundle ``/my-feedback?ids=12,14``;
* the same on a report they follow — :func:`public_feedback_href`:
  ``/all-feedback?row=12``, several ``/all-feedback?sort=updated``;

and :func:`feedback_notice_href` picks between the last two by ``mine``, as
``feedback_answered`` and ``feedback_comment`` do per recipient: their type alone does not
say the page. Lifted from keksdose (``push_notifications.feedback_notice_url``,
``feedback_alerts.feedback_url`` and ``bundle_texts``; live #500, dev #678): Marcel tapped
"4 of your feedback reports have answers" and saw none of them, because the bare
``/my-feedback`` the notice opened kept the status filter he had left on. A ``?row=``
alone has the same gap: the row opens, and the list under it stays filtered.

**One rule for every app**: the caller passes the reports the notice is about and the
link follows — one report opens in ``&row=`` beside its set of one; several name the set
and open no row; more than :data:`FEEDBACK_LINK_IDS_MAX` open the whole list. The public
list keeps no filter of its own and pages on the server, so it takes no ``ids``: one
report opens in ``?row=`` (fetched even off the page in view), several lead the list by
their last change, which the answer just was.

**A path by default**, as a push's ``data.url`` and an inbox row's ``url`` take it; with
``app_base_url`` (the app's frontend origin, perhaps with a path) the absolute link a
mail carries. The paths are the contract's routes (§4.1, §9.2, §10.2); no app has another.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from eifi1_server_kit.feedback.schemas import PublicSort

__all__ = [
    "FEEDBACK_INBOX_PATH",
    "FEEDBACK_LINK_IDS_MAX",
    "MY_FEEDBACK_PATH",
    "PUBLIC_FEEDBACK_PATH",
    "feedback_inbox_href",
    "feedback_notice_href",
    "my_feedback_href",
    "public_feedback_href",
]

#: The admin's inbox (§4.1): every report, triaged there.
FEEDBACK_INBOX_PATH = "/feedback"
#: The person's own reports (§4.1), an admin's too.
MY_FEEDBACK_PATH = "/my-feedback"
#: The public list (§9.3, "Feedback discussion"), where the reports a person follows live.
PUBLIC_FEEDBACK_PATH = "/all-feedback"

#: The most reports a link names (keksdose's ``_NOTICE_IDS_MAX``): the inbox keeps a
#: notice's link in a ``String(400)``, and fifty ids of up to six digits fill it to ~370.
#: Past it the link opens the whole list, as before ``?ids=``.
FEEDBACK_LINK_IDS_MAX = 50

#: Several followed reports lead the public list by their last change (§13.7).
_FOLLOWED_SORT: PublicSort = "updated"

#: The largest id the browser reads exactly (``Number.MAX_SAFE_INTEGER``): ``?row=`` and
#: ``?ids=`` refuse a larger one, so a link naming it would open nothing.
_MAX_SAFE_ID = 2**53 - 1

#: An http(s) origin with an optional path: no query, no fragment, and no control
#: character, backslash or space (``legal``'s ``_UNSAFE_IN_HREF``), so the page's path
#: and its query follow it as they are.
_BASE_URL = re.compile(r"https?://[^\s/\\?#\x00-\x1f\x7f]+(?:/[^\s\\?#\x00-\x1f\x7f]*)?")


def _report_ids(ids: int | Iterable[int]) -> list[int]:
    """``ids`` as a link names them: each once, in the order given. A :class:`TypeError`
    for an id that is no ``int`` (a ``bool`` or a string of digits too), a
    :class:`ValueError` for none or one the frontend would not read."""
    given = [ids] if isinstance(ids, int) else list(ids)
    for feedback_id in given:
        if isinstance(feedback_id, bool) or not isinstance(feedback_id, int):
            raise TypeError(f"a report id is an int: {feedback_id!r}")
        if not 1 <= feedback_id <= _MAX_SAFE_ID:
            raise ValueError(f"a report id is a positive integer of at most 2**53 - 1: {feedback_id}")
    if not given:
        raise ValueError("a feedback link names at least one report")
    return list(dict.fromkeys(given))


def _href(path: str, app_base_url: str | None) -> str:
    if app_base_url is None:
        return path
    if not _BASE_URL.fullmatch(app_base_url):
        raise ValueError(f"the app's base URL is an http(s) origin, perhaps with a path: {app_base_url[:60]!r}")
    return f"{app_base_url.rstrip('/')}{path}"


def _set_href(path: str, ids: int | Iterable[int], app_base_url: str | None) -> str:
    reports = _report_ids(ids)
    if len(reports) > FEEDBACK_LINK_IDS_MAX:
        return _href(path, app_base_url)
    query = "ids=" + ",".join(str(feedback_id) for feedback_id in reports)
    if len(reports) == 1:
        query += f"&row={reports[0]}"
    return _href(f"{path}?{query}", app_base_url)


def feedback_inbox_href(ids: int | Iterable[int], *, app_base_url: str | None = None) -> str:
    """The admin's inbox narrowed to ``ids`` (§13.7), for :data:`FEEDBACK_RECEIVED`:
    ``/feedback?ids=123&row=123`` for one report, ``/feedback?ids=7,8,9`` for a crash
    bundle, ``/feedback`` past :data:`FEEDBACK_LINK_IDS_MAX`. Each id once, in the order
    given. ``app_base_url`` makes it the absolute link a mail carries.

    A :class:`TypeError` for an id that is no ``int``; a :class:`ValueError` for no id,
    an id below 1 or above ``2**53 - 1``, or an ``app_base_url`` that is no http(s)
    origin (a query or a fragment in it included).
    """
    return _set_href(FEEDBACK_INBOX_PATH, ids, app_base_url)


def my_feedback_href(ids: int | Iterable[int], *, app_base_url: str | None = None) -> str:
    """The person's own reports narrowed to ``ids`` (§13.7), for a reply or a comment on
    their own report: ``/my-feedback?ids=12&row=12`` for one, ``/my-feedback?ids=12,14``
    for a bundle, ``/my-feedback`` past :data:`FEEDBACK_LINK_IDS_MAX`. Refuses what
    :func:`feedback_inbox_href` refuses."""
    return _set_href(MY_FEEDBACK_PATH, ids, app_base_url)


def public_feedback_href(ids: int | Iterable[int], *, app_base_url: str | None = None) -> str:
    """The public list for the reports ``ids`` a person follows (§13.7), for a reply or a
    comment on someone else's report: ``/all-feedback?row=12`` for one, and
    ``/all-feedback?sort=updated`` for several — the list names no set, and they were just
    answered, so they lead. No cap: no id is in the link. Refuses what
    :func:`feedback_inbox_href` refuses."""
    reports = _report_ids(ids)
    query = f"row={reports[0]}" if len(reports) == 1 else f"sort={_FOLLOWED_SORT}"
    return _href(f"{PUBLIC_FEEDBACK_PATH}?{query}", app_base_url)


def feedback_notice_href(ids: int | Iterable[int], *, mine: bool, app_base_url: str | None = None) -> str:
    """Where a :data:`FEEDBACK_ANSWERED` or :data:`FEEDBACK_COMMENT` notice about ``ids``
    opens for its recipient (§9.11): :func:`my_feedback_href` when they are the reports'
    author (``mine``), :func:`public_feedback_href` when they follow them — a commenter,
    an admin on someone else's report too. keksdose's ``feedback_notice_url``."""
    build = my_feedback_href if mine else public_feedback_href
    return build(ids, app_base_url=app_base_url)
