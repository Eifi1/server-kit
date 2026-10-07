"""The user list's query: paging, sort, search and the state filter, parsed once.

``docs/user-admin-harmonization.md`` §3.1 in ``Eifi1/ui-kit``:
``GET /admin/users?limit=&offset=&sort=&q=&role=&state=``. keksdose's roster is the
reference (``admin_users_service``: ``SORT_KEYS``, ``STATE_TOKENS``, ``parse_sort``,
``DEFAULT_PAGE_SIZE`` / ``MAX_PAGE_SIZE``); Kurvenschmiede's unpaged list and kastlan's
``/auth/users`` move to the same query.

The kit parses and refuses; the app turns :class:`UserListQuery` into its SQL — the
columns, the RLS and kastlan's company scope stay there. A malformed query is a
:class:`RosterQueryError` (422, in
:data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`): an unknown sort key or state token is
the caller's mistake, and silently matching nothing would read as "no such users"
(keksdose's reason for refusing its unknown plans).

**Sort** (:func:`parse_sort`). ``name`` (last name, first name, then id — auth §3.2),
``email``, ``role``, ``created``, ``last_login``; descending with a leading ``-`` (the
contract) or a ``.desc`` suffix (what the kit's ``DataTable`` ``encodeSorts`` writes and
keksdose speaks today). Several keys, comma-separated, each at most once. No sort asked:
:data:`DEFAULT_ROSTER_SORT`, newest account first (keksdose). The app appends ``id`` as the
last key so equal values never reorder between pages, and puts NULLs LAST in both
directions — somebody who never signed in is not "the most recent" because the direction
flipped (keksdose ``_order``).

**State** (:data:`STATE_TOKENS`). Flags, comma-separated or repeated (``state=a,b`` and
``state=a&state=b`` alike), OR-ed together — the reading a column filter gives. An app
narrows the vocabulary to the states it has (Kurvenschmiede has no ``invited`` user rows)
or widens it with its own (keksdose's ``allowlisted``) through ``accepted_states``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eifi1_server_kit.translation_review.scope import _like_literal

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_ROSTER_SORT",
    "MAX_PAGE_SIZE",
    "MAX_SEARCH_LENGTH",
    "SORT_KEYS",
    "STATE_TOKENS",
    "RosterQueryError",
    "RosterSort",
    "UserListQuery",
    "parse_roster_query",
    "parse_sort",
    "parse_tokens",
]

#: The sort keys every app's list takes (§3.1). An app adds its own columns' keys
#: (keksdose's ``plan``, ``privacy``) through ``parse_roster_query(sort_keys=…)``.
SORT_KEYS: tuple[str, ...] = ("name", "email", "role", "created", "last_login")

#: The ``state`` vocabulary (§3.1): keksdose's tokens (``admin``, ``reviewer``,
#: ``verified``, ``unverified``, ``deactivated``, ``never_logged_in``) and the contract's
#: new ones. ``invited`` is a user row that has not completed its sign-up (kastlan's
#: interim invite); ``deletion_scheduled`` has ``deletion_requested_at`` set (§6.4).
#: keksdose's ``allowlisted`` is its own, added through ``accepted_states``.
STATE_TOKENS: tuple[str, ...] = (
    "active",
    "deactivated",
    "invited",
    "unverified",
    "verified",
    "password_change_required",
    "deletion_scheduled",
    "never_logged_in",
    "admin",
    "reviewer",
)

#: One page of the list, and the most one request may ask for (keksdose): the pager makes
#: the size visible, the cap stops a hand-written ``?limit=100000``.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
#: The search box's longest query (keksdose ``Query(max_length=200)``).
MAX_SEARCH_LENGTH = 200


class RosterSort(NamedTuple):
    """One sort key and its direction."""

    key: str
    descending: bool = False


#: No sort asked: the newest account first — on an install that is growing, the row you
#: came to look at is the one that just arrived (keksdose).
DEFAULT_ROSTER_SORT: tuple[RosterSort, ...] = (RosterSort("created", descending=True),)


class RosterQueryError(ValueError):
    """A malformed user-list query: an unknown sort key, state or role, a page out of
    range (→ 422)."""

    status_code: int = 422


class UserListQuery(BaseModel):
    """A parsed ``GET /admin/users`` query — build it with :func:`parse_roster_query`.

    Field names are the wire's: ``role`` and ``state`` hold the tokens asked for (OR-ed
    within each, AND-ed between them), empty for "no filter".
    """

    model_config = ConfigDict(frozen=True)

    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
    sort: tuple[RosterSort, ...] = DEFAULT_ROSTER_SORT
    #: Trimmed; ``None`` when blank.
    q: str | None = Field(default=None, max_length=MAX_SEARCH_LENGTH)
    role: tuple[str, ...] = ()
    state: tuple[str, ...] = ()

    @property
    def search_pattern(self) -> str | None:
        """``q`` as a ``LIKE`` pattern — ``%q%``, its own ``%`` and ``_`` escaped with
        :data:`~eifi1_server_kit.translation_review.LIKE_ESCAPE` — or ``None`` without a
        search.

        ``q`` searches the first name, the last name, both orders together, and the
        email (auth §10.6), case-insensitively::

            if (pattern := query.search_pattern) is not None:
                full = User.first_name + " " + User.last_name
                reverse = User.last_name + " " + User.first_name
                stmt = stmt.where(or_(*(c.ilike(pattern, escape=LIKE_ESCAPE) for c in
                                        (User.email, User.first_name, User.last_name, full, reverse))))
        """
        return None if self.q is None else f"%{_like_literal(self.q)}%"


def _parts(raw: str | Iterable[str] | None) -> list[str]:
    """Comma-separated, repeated, or both — stripped, blanks dropped."""
    if raw is None:
        return []
    values = [raw] if isinstance(raw, str) else list(raw)
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def parse_sort(raw: str | None, *, keys: Collection[str] = SORT_KEYS) -> tuple[RosterSort, ...]:
    """``"-last_login,name"`` → ``(RosterSort("last_login", True), RosterSort("name", False))``.

    ``-key`` and ``key.desc`` sort descending, ``key`` and ``key.asc`` ascending. Nothing
    asked: :data:`DEFAULT_ROSTER_SORT`. An unknown key, an unknown direction, both a ``-``
    and a suffix, or a key twice is a :class:`RosterQueryError` naming it — two
    directions for one key have no sensible reading (keksdose ``sorting.parse_sort``).
    """
    out: list[RosterSort] = []
    seen: set[str] = set()
    for part in _parts(raw):
        descending = part.startswith("-")
        key, dot, direction = part.removeprefix("-").partition(".")
        if key not in keys:
            raise RosterQueryError(f"Unknown sort key {key!r}; expected one of {', '.join(keys)}")
        if dot and (descending or direction not in ("asc", "desc")):
            raise RosterQueryError(f"Unknown sort direction {part!r}; use 'key', '-key', 'key.asc' or 'key.desc'")
        if key in seen:
            raise RosterQueryError(f"Duplicate sort key {key!r}")
        seen.add(key)
        out.append(RosterSort(key, descending or direction == "desc"))
    return tuple(out) or DEFAULT_ROSTER_SORT


def parse_tokens(raw: str | Iterable[str] | None, accepted: Collection[str], *, what: str) -> tuple[str, ...]:
    """The tokens of a filter — comma-separated or repeated — de-duplicated in the order
    asked, every one in ``accepted``; an unknown one is a :class:`RosterQueryError`
    naming it and what is accepted."""
    tokens = list(dict.fromkeys(_parts(raw)))
    if unknown := [token for token in tokens if token not in accepted]:
        raise RosterQueryError(f"Unknown {what} {', '.join(unknown)}; expected one of {', '.join(accepted)}")
    return tuple(tokens)


def parse_roster_query(
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    sort: str | None = None,
    q: str | None = None,
    role: str | Iterable[str] | None = None,
    state: str | Iterable[str] | None = None,
    sort_keys: Collection[str] = SORT_KEYS,
    accepted_states: Collection[str] = STATE_TOKENS,
    roles: Collection[str] | None = None,
) -> UserListQuery:
    """The query of ``GET /admin/users`` (§3.1), parsed and checked, or a
    :class:`RosterQueryError` (422).

    Take the raw query parameters as the route gets them and hand them over::

        @router.get("/admin/users")
        async def list_users(limit: int = 25, offset: int = 0, sort: str | None = None,
                             q: str | None = None,
                             role: Annotated[list[str] | None, Query()] = None,
                             state: Annotated[list[str] | None, Query()] = None):
            query = kit.parse_roster_query(limit=limit, offset=offset, sort=sort, q=q,
                                           role=role, state=state, roles=[r.value for r in Role])

    * ``sort_keys`` — the keys the app sorts by: :data:`SORT_KEYS`, plus its own columns'.
    * ``accepted_states`` — the app's state vocabulary: :data:`STATE_TOKENS`, narrowed to
      the states it has, or widened with its own.
    * ``roles`` — the app's role values; ``None`` takes any role token unchecked.
    """
    sorts = parse_sort(sort, keys=sort_keys)
    states = parse_tokens(state, accepted_states, what="state")
    role_tokens = tuple(dict.fromkeys(_parts(role))) if roles is None else parse_tokens(role, roles, what="role")
    try:
        return UserListQuery(
            limit=limit,
            offset=offset,
            sort=sorts,
            q=(q or "").strip() or None,
            role=role_tokens,
            state=states,
        )
    except ValidationError as exc:
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        raise RosterQueryError(f"{field}: {error['msg']}") from exc
