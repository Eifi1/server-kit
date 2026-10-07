"""The user list's query (§3.1): keksdose's roster grammar, the contract's ``-key``."""

from __future__ import annotations

import pytest

from eifi1_server_kit.translation_review import LIKE_ESCAPE
from eifi1_server_kit.user_admin import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_ROSTER_SORT,
    MAX_PAGE_SIZE,
    SORT_KEYS,
    STATE_TOKENS,
    RosterQueryError,
    RosterSort,
    UserListQuery,
    parse_roster_query,
    parse_sort,
    parse_tokens,
)


def test_the_vocabulary_is_the_contracts() -> None:
    assert SORT_KEYS == ("name", "email", "role", "created", "last_login")
    contract = {
        "active",
        "deactivated",
        "invited",
        "unverified",
        "password_change_required",
        "deletion_scheduled",
        "never_logged_in",
        "admin",
        "reviewer",
    }
    assert contract <= set(STATE_TOKENS) and set(STATE_TOKENS) - contract == {"verified"}, "keksdose's verified too"
    assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (25, 200)


def test_nothing_asked_is_the_first_page_newest_first() -> None:
    query = parse_roster_query()
    assert query == UserListQuery()
    assert (query.limit, query.offset, query.q, query.role, query.state) == (25, 0, None, (), ())
    assert query.sort == DEFAULT_ROSTER_SORT == (RosterSort("created", descending=True),)
    assert query.search_pattern is None


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("name", (("name", False),)),
        ("-name", (("name", True),)),
        ("name.desc", (("name", True),)),
        ("name.asc", (("name", False),)),
        ("-last_login, email", (("last_login", True), ("email", False))),
        ("created.desc,role,", (("created", True), ("role", False))),
        ("", (("created", True),)),
        (None, (("created", True),)),
    ],
)
def test_sort_takes_the_contracts_dash_and_the_kits_suffix(
    raw: str | None, parsed: tuple[tuple[str, bool], ...]
) -> None:
    """``-key`` is the contract's; ``key.desc`` is what DataTable's ``encodeSorts`` writes."""
    assert parse_sort(raw) == tuple(RosterSort(key, descending) for key, descending in parsed)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("plan", "Unknown sort key 'plan'"),
        ("-", "Unknown sort key ''"),
        ("name.up", "Unknown sort direction 'name.up'"),
        ("name.", "Unknown sort direction 'name.'"),
        ("-name.desc", "Unknown sort direction '-name.desc'"),
        ("name,-name", "Duplicate sort key 'name'"),
    ],
)
def test_sort_refuses_what_it_cannot_read(raw: str, message: str) -> None:
    with pytest.raises(RosterQueryError, match=message) as raised:
        parse_sort(raw)
    assert raised.value.status_code == 422


def test_an_app_adds_its_own_sort_keys() -> None:
    """keksdose sorts by plan and privacy too."""
    keys = (*SORT_KEYS, "plan", "privacy")
    assert parse_roster_query(sort="plan.desc", sort_keys=keys).sort == (RosterSort("plan", True),)


def test_state_tokens_comma_separated_or_repeated() -> None:
    assert parse_roster_query(state="active, admin").state == ("active", "admin")
    assert parse_roster_query(state=["active", "admin,reviewer", "active"]).state == ("active", "admin", "reviewer")
    with pytest.raises(RosterQueryError, match="Unknown state allowlisted, bogus; expected one of active"):
        parse_roster_query(state="allowlisted,bogus")


def test_an_app_narrows_or_widens_the_states() -> None:
    """Kurvenschmiede has no ``invited`` user rows; keksdose keeps its ``allowlisted``."""
    kurvenschmiede = tuple(token for token in STATE_TOKENS if token != "invited")
    with pytest.raises(RosterQueryError, match="Unknown state invited"):
        parse_roster_query(state="invited", accepted_states=kurvenschmiede)
    keksdose = (*STATE_TOKENS, "allowlisted")
    assert parse_roster_query(state="allowlisted", accepted_states=keksdose).state == ("allowlisted",)


def test_roles_are_checked_against_the_apps_vocabulary_when_given() -> None:
    assert parse_roster_query(role="ADMIN,anything").role == ("ADMIN", "anything")
    assert parse_roster_query(role=["ADMIN", "MEMBER"], roles=["ADMIN", "MEMBER"]).role == ("ADMIN", "MEMBER")
    with pytest.raises(RosterQueryError, match="Unknown role admin; expected one of ADMIN, MEMBER"):
        parse_roster_query(role="admin", roles=["ADMIN", "MEMBER"])
    assert parse_tokens(None, ("a",), what="x") == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit: Input should be greater than or equal to 1"),
        ({"limit": 201}, "limit: Input should be less than or equal to 200"),
        ({"offset": -1}, "offset: Input should be greater than or equal to 0"),
        ({"q": "x" * 201}, "q: String should have at most 200 characters"),
    ],
)
def test_the_page_and_the_search_are_bounded(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(RosterQueryError, match=message):
        parse_roster_query(**kwargs)  # type: ignore[arg-type]


def test_the_search_is_trimmed_and_escaped_for_like() -> None:
    query = parse_roster_query(q="  50%_off\\ ")
    assert query.q == "50%_off\\"
    assert query.search_pattern == "%50\\%\\_off\\\\%"
    assert LIKE_ESCAPE == "\\"
    assert parse_roster_query(q="   ").q is None


def test_the_query_is_frozen() -> None:
    with pytest.raises(ValueError):
        parse_roster_query().limit = 5  # type: ignore[misc]
