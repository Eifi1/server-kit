"""Where a feedback notice opens (feedback-harmonization §9.11, §13.7; keksdose live #500)."""

from __future__ import annotations

from typing import Any

import pytest

from eifi1_server_kit.feedback import (
    FEEDBACK_INBOX_PATH,
    FEEDBACK_LINK_IDS_MAX,
    MY_FEEDBACK_PATH,
    PUBLIC_FEEDBACK_PATH,
    feedback_inbox_href,
    feedback_notice_href,
    my_feedback_href,
    public_feedback_href,
)

BASE = "https://kurvenschmiede.example"


def test_the_pages_are_the_contracts_routes() -> None:
    assert FEEDBACK_INBOX_PATH == "/feedback"
    assert MY_FEEDBACK_PATH == "/my-feedback"
    assert PUBLIC_FEEDBACK_PATH == "/all-feedback"
    # keksdose's `_NOTICE_IDS_MAX`: the inbox row's `url` is a String(400).
    assert FEEDBACK_LINK_IDS_MAX == 50


def test_an_admin_alert_opens_its_report_in_the_inbox_set_of_one() -> None:
    # keksdose `feedback_alerts.feedback_url`: the set keeps a status filter the admin left on
    # from hiding the very report the alert is about.
    assert feedback_inbox_href(123) == "/feedback?ids=123&row=123"
    assert feedback_inbox_href([123]) == "/feedback?ids=123&row=123"


def test_a_crash_bundle_opens_the_inbox_narrowed_to_its_reports() -> None:
    # keksdose `bundle_texts`: oldest first, as the bundle's lines; no row opened.
    assert feedback_inbox_href([7, 8, 9]) == "/feedback?ids=7,8,9"


def test_the_authors_own_reports_open_in_my_feedback() -> None:
    assert my_feedback_href(12) == "/my-feedback?ids=12&row=12"
    assert my_feedback_href([12, 14]) == "/my-feedback?ids=12,14"


def test_a_followed_report_opens_on_the_public_list() -> None:
    # The public list keeps no filter and pages on the server: one opens in `?row=`, several
    # lead the list by their last change, which the answer just was.
    assert public_feedback_href(12) == "/all-feedback?row=12"
    assert public_feedback_href([12, 14]) == "/all-feedback?sort=updated"


def test_a_notice_picks_the_page_by_whose_report_it_is() -> None:
    # keksdose `push_notifications.feedback_notice_url`, verbatim in effect.
    assert feedback_notice_href([12], mine=True) == "/my-feedback?ids=12&row=12"
    assert feedback_notice_href([12], mine=False) == "/all-feedback?row=12"
    assert feedback_notice_href([12, 14], mine=True) == "/my-feedback?ids=12,14"
    assert feedback_notice_href([12, 14], mine=False) == "/all-feedback?sort=updated"
    assert feedback_notice_href(12, mine=True, app_base_url=BASE) == f"{BASE}/my-feedback?ids=12&row=12"


def test_each_report_is_named_once_in_the_order_given() -> None:
    assert feedback_inbox_href([9, 7, 9, 8, 7]) == "/feedback?ids=9,7,8"
    # A repeat is one report: a set of one opens it.
    assert my_feedback_href([12, 12]) == "/my-feedback?ids=12&row=12"
    assert public_feedback_href((12, 12)) == "/all-feedback?row=12"
    assert feedback_inbox_href(iter([3, 4])) == "/feedback?ids=3,4"


def test_past_the_cap_the_whole_list_opens() -> None:
    fifty = list(range(1, FEEDBACK_LINK_IDS_MAX + 1))
    assert my_feedback_href(fifty) == "/my-feedback?ids=" + ",".join(map(str, fifty))
    assert my_feedback_href([*fifty, 51]) == "/my-feedback"
    assert feedback_inbox_href([*fifty, 51]) == "/feedback"
    assert feedback_inbox_href([*fifty, 51], app_base_url=BASE) == f"{BASE}/feedback"
    # Repeats are counted once: fifty reports named twice stay a set.
    assert my_feedback_href(fifty + fifty) == my_feedback_href(fifty)
    # The public list names no ids, so it has no cap.
    assert public_feedback_href([*fifty, 51]) == "/all-feedback?sort=updated"


def test_a_base_url_makes_the_link_absolute_for_a_mail() -> None:
    assert feedback_inbox_href(5, app_base_url=BASE) == f"{BASE}/feedback?ids=5&row=5"
    assert feedback_inbox_href(5, app_base_url=f"{BASE}/") == f"{BASE}/feedback?ids=5&row=5"
    assert my_feedback_href([5, 6], app_base_url="http://localhost:5173") == "http://localhost:5173/my-feedback?ids=5,6"
    assert public_feedback_href(5, app_base_url=f"{BASE}/app/") == f"{BASE}/app/all-feedback?row=5"


def test_the_largest_id_the_browser_reads_exactly_is_linked() -> None:
    top = 2**53 - 1
    assert feedback_inbox_href(top) == f"/feedback?ids={top}&row={top}"


@pytest.mark.parametrize("bad", [0, -3, 2**53], ids=["zero", "negative", "past-safe"])
def test_an_id_the_frontend_would_not_read_is_refused(bad: int) -> None:
    # `?row=` and `?ids=` take digits with no leading zero, a safe integer (§13.2, §13.7).
    with pytest.raises(ValueError, match="report id"):
        feedback_inbox_href([1, bad])
    with pytest.raises(ValueError, match="report id"):
        public_feedback_href([1, bad])


@pytest.mark.parametrize("bad", [True, "12", 12.0, None], ids=["bool", "str", "float", "none"])
def test_an_id_that_is_no_int_is_refused(bad: Any) -> None:
    with pytest.raises(TypeError, match="report id"):
        my_feedback_href([bad])


def test_a_string_is_no_list_of_ids() -> None:
    with pytest.raises(TypeError, match="report id"):
        feedback_inbox_href("12")  # type: ignore[arg-type]


@pytest.mark.parametrize("empty", [[], ()], ids=["list", "tuple"])
def test_a_link_names_at_least_one_report(empty: Any) -> None:
    for build in (feedback_inbox_href, my_feedback_href, public_feedback_href):
        with pytest.raises(ValueError, match="at least one report"):
            build(empty)


@pytest.mark.parametrize(
    "base",
    [
        "kurvenschmiede.example",
        "ftp://kurvenschmiede.example",
        "//kurvenschmiede.example",
        "https://",
        "https:///feedback",
        "https://kurvenschmiede.example/?next=1",
        "https://kurvenschmiede.example/#top",
        "https://kurvenschmiede .example",
        "https://kurvenschmiede.example\n",
        "https://kurvenschmiede.example\\x",
    ],
    ids=["no-scheme", "ftp", "relative", "no-host", "empty-host", "query", "hash", "space", "newline", "backslash"],
)
def test_a_base_url_that_is_no_http_origin_is_refused(base: str) -> None:
    with pytest.raises(ValueError, match="base URL"):
        feedback_inbox_href(5, app_base_url=base)
    # Past the cap as well: the bare list is built from the same base.
    with pytest.raises(ValueError, match="base URL"):
        my_feedback_href(range(1, FEEDBACK_LINK_IDS_MAX + 2), app_base_url=base)
