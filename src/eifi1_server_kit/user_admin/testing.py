"""Test helpers for the admin router: an in-memory roster, and a conformance check per port.

``docs/auth-harmonization.md`` §12.3 ("Testing support") in ``Eifi1/ui-kit``, beside
:mod:`eifi1_server_kit.auth.testing`. In the wheel and NOT re-exported from
:mod:`eifi1_server_kit.user_admin`: import it as ``eifi1_server_kit.user_admin.testing``. No
SQLAlchemy, no FastAPI.

* :class:`InMemoryRosterStore`, a :class:`~eifi1_server_kit.user_admin.ports.RosterStore`
  over the rows of an in-memory account store (``auth.testing.InMemoryUserStore.rows``, or
  any mapping of ids to account rows); the kit's own router tests run on it;
* :func:`check_roster_store`, which an app runs once against its own roster (or the ``sql``
  extra's ``SqlRosterStore``) on its test database as it is, and
  :func:`check_accepted_invitations` for an invitation store's ``forget_accepted`` and
  :func:`check_account_log_scrub` for an admin log's ``scrub_account`` (0.31). Each
  raises :class:`AssertionError` naming the first promise that fails, writes under fresh
  addresses, and asserts only on what it adds.

Synthetic data only: every address carries a fresh token.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from eifi1_server_kit.auth.ports import (
    AccountRow,
    AdminActionLog,
    InvitationStore,
    NewAccount,
    NewInvitation,
    UserStore,
)
from eifi1_server_kit.auth.tokens import hash_token
from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.user_admin.actions import AdminAction
from eifi1_server_kit.user_admin.audit import admin_action_record
from eifi1_server_kit.user_admin.ports import AcceptedInvitations, AccountLogScrub, RosterCounts
from eifi1_server_kit.user_admin.roster import RosterSort, UserListQuery, parse_roster_query

__all__ = ["InMemoryRosterStore", "check_accepted_invitations", "check_account_log_scrub", "check_roster_store"]


def _aware(moment: datetime) -> datetime:
    """A naive read-back is UTC (SQLite hands a zoned column back naive)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _is_person(row: AccountRow) -> bool:
    return not getattr(row, "is_demo", False)


def _has_locales(row: AccountRow) -> bool:
    return bool(row.translation_review_locales)


def _matches_state(row: AccountRow, token: str, admin_role: str, reviewer_role: str | None) -> bool:
    match token:
        case "active":
            return row.is_active
        case "deactivated":
            return not row.is_active
        case "unverified":
            return row.email_verified_at is None
        case "verified":
            return row.email_verified_at is not None
        case "password_change_required":
            return row.password_change_required_at is not None
        case "deletion_scheduled":
            return row.deletion_requested_at is not None
        case "never_logged_in":
            return row.last_login_at is None
        case "admin":
            return row.role == admin_role
        case "reviewer":
            return row.role == reviewer_role if reviewer_role is not None else _has_locales(row)
        case _:
            # ``invited`` and an app's own token: no account row of the kit's is one.
            return False


def _searched(row: AccountRow, needle: str) -> bool:
    haystacks = (
        row.email,
        row.first_name,
        row.last_name,
        f"{row.first_name} {row.last_name}",
        f"{row.last_name} {row.first_name}",
    )
    return any(needle in text.lower() for text in haystacks)


#: Each sort key's values, as the SQL stores order them.
_SORT_VALUES: Mapping[str, Callable[[AccountRow], tuple[object, ...]]] = {
    "name": lambda row: (row.last_name, row.first_name),
    "email": lambda row: (row.email,),
    "role": lambda row: (row.role,),
    "created": lambda row: (_aware(row.created_at),),
    "last_login": lambda row: (None if row.last_login_at is None else _aware(row.last_login_at),),
}


def _ordered(rows: list[AccountRow], sort: Sequence[RosterSort]) -> list[AccountRow]:
    """Sorted as the port says: each key in its direction, NULLs last either way, then the id
    ascending. Stable sorts from the last key to the first."""
    ordered = sorted(rows, key=lambda row: row.id)
    for key, descending in reversed(sort):
        values = _SORT_VALUES[key]
        for index in reversed(range(len(values(ordered[0])) if ordered else 0)):
            ordered = _by_one_value(ordered, _component(values, index), descending=descending)
    return ordered


def _component(values: Callable[[AccountRow], tuple[object, ...]], index: int) -> Callable[[AccountRow], Any]:
    return lambda row: values(row)[index]


def _by_one_value(rows: list[AccountRow], value: Callable[[AccountRow], Any], *, descending: bool) -> list[AccountRow]:
    """``rows`` stably sorted by ``value``, the rows whose value is None after the rest."""
    present = sorted((row for row in rows if value(row) is not None), key=value, reverse=descending)
    return present + [row for row in rows if value(row) is None]


class InMemoryRosterStore:
    """A :class:`~eifi1_server_kit.user_admin.ports.RosterStore` over ``rows``, a live mapping
    of ids to account rows (``InMemoryUserStore.rows``), read at every call. A row with
    ``is_demo`` true is no person. An unknown sort key is a :class:`ValueError`, as the SQL
    store's."""

    def __init__(self, rows: Mapping[int, AccountRow]) -> None:
        self.rows = rows

    def _people(self) -> list[AccountRow]:
        return [row for row in self.rows.values() if _is_person(row)]

    async def page(
        self, query: UserListQuery, *, admin_role: str, reviewer_role: str | None
    ) -> tuple[Sequence[Any], int]:
        if unknown := [sort.key for sort in query.sort if sort.key not in _SORT_VALUES]:
            raise ValueError(f"the roster sorts by no {', '.join(unknown)}")
        rows = self._people()
        if query.q is not None:
            needle = query.q.lower()
            rows = [row for row in rows if _searched(row, needle)]
        if query.role:
            rows = [row for row in rows if row.role in query.role]
        if query.state:
            rows = [
                row
                for row in rows
                if any(_matches_state(row, token, admin_role, reviewer_role) for token in query.state)
            ]
        ordered = _ordered(rows, query.sort)
        return ordered[query.offset : query.offset + query.limit], len(ordered)

    async def counts(self, *, since: datetime) -> RosterCounts:
        people = self._people()
        return RosterCounts(
            people=len(people),
            new=sum(_aware(row.created_at) >= _aware(since) for row in people),
            deletion_requests=sum(row.deletion_requested_at is not None for row in people),
        )

    async def deletion_requested(self) -> Sequence[int]:
        asked = [row for row in self._people() if row.deletion_requested_at is not None and not row.is_active]
        asked.sort(key=lambda row: (_aware(row.deletion_requested_at or row.created_at), row.id))
        return [row.id for row in asked]


# ── The checks ──────────────────────────────────────────────────────────────


def _check(condition: bool, message: str) -> None:
    """``assert`` that survives ``python -O``."""
    if not condition:
        raise AssertionError(message)


async def _ids(page: Awaitable[tuple[Sequence[Any], int]]) -> tuple[list[int], int]:
    rows, total = await page
    return [row.id for row in rows], total


async def check_roster_store(
    roster: Any,
    users: UserStore[Any],
    *,
    admin_role: str = "admin",
    member_role: str = "member",
    reviewer_role: str = "reviewer",
    add_demo: Callable[[NewAccount], Awaitable[AccountRow]] | None = None,
) -> None:
    """Does the app's roster keep the port's promises (user-admin §3.1)? Raises
    :class:`AssertionError` naming the first that fails.

    ``users`` is the app's account store on the same session: the check adds four accounts
    under fresh addresses through it (and a demo one with ``add_demo``), sets their states
    with ``apply``, and deletes them at the end; every page it asks for searches the fresh
    token, so the app's own rows never take part. ``admin_role``, ``member_role`` and
    ``reviewer_role`` are three of the app's role values. Every count is checked by the
    change the check makes, so the database may hold rows of its own.
    """
    token = secrets.token_hex(6)
    ahead, behind = utc_now() + timedelta(days=1), utc_now() - timedelta(days=1)
    before = await roster.counts(since=behind)
    later = await roster.counts(since=ahead)
    queued = set(await roster.deletion_requested())

    def account(label: str, first: str, last: str, role: str) -> NewAccount:
        return NewAccount(
            email=f"check-{token}-{label}@example.test",
            password_hash=f"check-hash-{token}",
            first_name=first,
            last_name=f"{last}{token}",
            locale="en",
            role=role,
        )

    ada = await users.add(account("a", "Ada", "Lovelace", admin_role))
    bob = await users.add(account("b", "Bob", "Babbage", member_role))
    cy = await users.add(account("c", "Cy", "Hopper", reviewer_role))
    dee = await users.add(account("d", "Dee", "Hopper", member_role))
    first_login, second_login = behind + timedelta(minutes=1), behind + timedelta(minutes=2)
    await users.apply(ada, {"email_verified_at": behind, "last_login_at": first_login})
    await users.apply(bob, {"is_active": False})
    await users.apply(
        cy,
        {
            "is_active": False,
            "email_verified_at": behind,
            "last_login_at": second_login,
            "password_change_required_at": behind,
            "deletion_requested_at": behind,
            "deletion_scheduled_at": ahead,
            "translation_review_locales": ["fr"],
        },
    )
    await users.apply(dee, {"is_active": False, "deletion_requested_at": behind - timedelta(hours=1)})
    demo = None
    if add_demo is not None:
        demo = await add_demo(account("demo", "Demo", "Demo", admin_role))

    def ask(**raw: Any) -> Awaitable[tuple[list[int], int]]:
        query = parse_roster_query(q=raw.pop("q", token), **raw)
        return _ids(roster.page(query, admin_role=admin_role, reviewer_role=reviewer_role))

    every = {ada.id, bob.id, cy.id, dee.id}

    # 1. The search, people only.
    found, total = await ask(limit=10)
    _check(set(found) == every and total == 4, "page: q finds the addresses, never a demo account")
    _check((await ask(q=f"ADA LOVELACE{token.upper()}"))[0] == [ada.id], "page: q is first and last name, any case")
    _check((await ask(q=f"Hopper{token} Cy"))[0] == [cy.id], "page: q is last and first name too")
    _check((await ask(q=f"Babbage{token}"))[0] == [bob.id], "page: q is the last name")
    _check(await ask(q=f"%{token}") == ([], 0), "page: a % in q matches itself, not anything")
    _check(await ask(q=f"_heck-{token}") == ([], 0), "page: an _ in q matches itself, not any character")

    # 2. The filters: role, and state tokens OR-ed among themselves, AND-ed with role.
    _check((await ask(role=admin_role))[0] == [ada.id], "page: role filters by the account's role")
    states = {
        "active": {ada.id},
        "deactivated": {bob.id, cy.id, dee.id},
        "unverified": {bob.id, dee.id},
        "verified": {ada.id, cy.id},
        "password_change_required": {cy.id},
        "deletion_scheduled": {cy.id, dee.id},
        "never_logged_in": {bob.id, dee.id},
        "admin": {ada.id},
        "reviewer": {cy.id},
        "invited": set(),
    }
    for state, wanted in states.items():
        got, counted = await ask(state=state, limit=10)
        _check(set(got) == wanted and counted == len(wanted), f"page: state={state}")
    got, _ = await ask(state="admin,reviewer", limit=10)
    _check(set(got) == {ada.id, cy.id}, "page: state tokens are OR-ed")
    got, _ = await ask(state="deactivated", role=member_role, limit=10)
    _check(set(got) == {bob.id, dee.id}, "page: state and role are AND-ed")

    # 3. The sort: each direction, NULLs last either way, the id last.
    _check((await ask(sort="name"))[0] == [bob.id, cy.id, dee.id, ada.id], "page: name is last, then first name")
    _check((await ask(sort="-name"))[0] == [ada.id, dee.id, cy.id, bob.id], "page: -name descends")
    _check((await ask(sort="email.desc"))[0] == [dee.id, cy.id, bob.id, ada.id], "page: email.desc descends")
    logins = await ask(sort="last_login")
    _check(logins[0][:2] == [ada.id, cy.id], "page: last_login ascends")
    _check(set(logins[0][2:]) == {bob.id, dee.id}, "page: a never signed-in account sorts last")
    logins = await ask(sort="-last_login")
    _check(logins[0][:2] == [cy.id, ada.id], "page: -last_login descends")
    _check(logins[0][2:] == sorted([bob.id, dee.id]), "page: NULLs last descending too, then the id")
    by_role = sorted(
        [
            (admin_role, "Lovelace", "Ada", ada.id),
            (member_role, "Babbage", "Bob", bob.id),
            (reviewer_role, "Hopper", "Cy", cy.id),
            (member_role, "Hopper", "Dee", dee.id),
        ]
    )
    _check((await ask(sort="role,name"))[0] == [row[3] for row in by_role], "page: role sorts, then the next key")
    tied = (await ask(sort="name", q=f"Hopper{token}"))[0]
    _check(tied == [cy.id, dee.id], "page: the first name breaks a tie in the last name")

    # 4. Paging: limit and offset walk the same order; the total is every match.
    walked: list[int] = []
    for offset in (0, 2, 4):
        rows, counted = await ask(sort="name", limit=2, offset=offset)
        _check(counted == 4, "page: the total counts every match, not the page")
        walked += rows
    _check(walked == [bob.id, cy.id, dee.id, ada.id], "page: offset and limit walk the same order")

    # 5. The counts.
    now = await roster.counts(since=behind)
    _check(now.people - before.people == 4, "counts: every person, deactivated ones too, never a demo")
    _check(now.new - before.new == 4, "counts: an account made since then is new")
    _check(now.deletion_requests - before.deletion_requests == 2, "counts: the deletion requests")
    _check((await roster.counts(since=ahead)).new == later.new, "counts: an account made before since is not new")

    # 6. The job's candidates.
    asked = [user_id for user_id in await roster.deletion_requested() if user_id not in queued]
    _check(asked == [dee.id, cy.id], "deletion_requested: the deactivated requests, the oldest first")

    for row in (ada, bob, cy, dee, *([demo] if demo is not None else [])):
        await users.delete(row)


async def check_accepted_invitations(
    store: InvitationStore[Any], *, inviter_id: int, user_id: int, other_user_id: int
) -> None:
    """Does the app's invitation store forget what an account accepted (user-admin §6.4)?
    Raises :class:`AssertionError` naming the first promise that fails, and
    :class:`TypeError` for a store without ``forget_accepted``.

    ``inviter_id``, ``user_id`` and ``other_user_id`` are existing accounts. It adds four
    invitations under fresh addresses: two accepted by ``user_id``, one by
    ``other_user_id``, one open.
    """
    if not isinstance(store, AcceptedInvitations):
        raise TypeError("the invitation store has no forget_accepted (user_admin.ports.AcceptedInvitations)")
    token = secrets.token_hex(6)
    now = utc_now()

    def invitation(label: str) -> NewInvitation:
        return NewInvitation(
            email=f"check-{token}-{label}@example.test",
            token_hash=hash_token(f"check-{token}-{label}"),
            expires_at=now + timedelta(days=14),
            locale="en",
            invited_by_user_id=inviter_id,
        )

    first = await store.add(invitation("first"))
    second = await store.add(invitation("second"))
    others = await store.add(invitation("other"))
    still_open = await store.add(invitation("open"))
    for row, by in ((first, user_id), (second, user_id), (others, other_user_id)):
        await store.accept(row, user_id=by, at=now)

    forgotten = sorted(await store.forget_accepted(user_id))
    _check(forgotten == sorted([first.email, second.email]), "forget_accepted: answers the addresses it forgot")
    _check(await store.get(first.id) is None and await store.get(second.id) is None, "forget_accepted: deletes them")
    _check(await store.get(others.id) is not None, "forget_accepted: another account's stay")
    _check(await store.get(still_open.id) is not None, "forget_accepted: an open invitation stays")
    _check(list(await store.forget_accepted(user_id)) == [], "forget_accepted: twice forgets nothing more")


async def _every_row(log: AdminActionLog[Any], target_user_id: int | None) -> dict[int, Any]:
    """Every row ``page`` answers for ``target_user_id``, by id, a page at a time."""
    found: dict[int, Any] = {}
    offset = 0
    while True:
        rows, total = await log.page(target_user_id=target_user_id, limit=200, offset=offset)
        found.update({row.id: row for row in rows})
        offset += len(rows)
        if not rows or offset >= total:
            return found


async def check_account_log_scrub(log: AdminActionLog[Any], *, actor_id: int, user_id: int, other_user_id: int) -> None:
    """Does the app's admin log scrub the rows about an account by its id (0.31, G2)? Raises
    :class:`AssertionError` naming the first promise that fails, and :class:`TypeError` for a
    log without ``scrub_account``
    (:class:`~eifi1_server_kit.user_admin.ports.AccountLogScrub`).

    ``actor_id``, ``user_id`` and ``other_user_id`` are existing accounts. It adds four rows
    under fresh addresses: two about ``user_id`` (an earlier address and the current one), one
    about ``other_user_id`` that ``user_id`` took, and an invitation with no account to the
    current address — that one is ``scrub_email``'s, and stays here.
    """
    if not isinstance(log, AccountLogScrub):
        raise TypeError("the admin log has no scrub_account (user_admin.ports.AccountLogScrub)")
    token = secrets.token_hex(6)
    now = utc_now()
    earlier, current, other = (f"check-{token}-{label}@example.test" for label in ("earlier", "current", "other"))

    def record(action: AdminAction, **fields: Any) -> dict[str, Any]:
        return admin_action_record(action, now=now, **fields)

    about = [
        await log.add(
            record(
                AdminAction.DEACTIVATE,
                actor_id=actor_id,
                target_user_id=user_id,
                target_email=earlier,
                detail={"sessions_ended": True},
            )
        ),
        await log.add(record(AdminAction.REACTIVATE, actor_id=actor_id, target_user_id=user_id, target_email=current)),
    ]
    theirs = await log.add(record(AdminAction.ROLE, actor_id=user_id, target_user_id=other_user_id, target_email=other))
    invitation = await log.add(record(AdminAction.INVITE, actor_id=actor_id, target_user_id=None, target_email=current))

    await log.scrub_account(user_id)
    rows = await _every_row(log, user_id)
    _check(
        all(row.id in rows and rows[row.id].target_email is None for row in about),
        "scrub_account: every row about the account loses its address, the earlier one too",
    )
    _check(
        [(rows[row.id].action, dict(rows[row.id].detail)) for row in about]
        == [("deactivate", {"sessions_ended": True}), ("reactivate", {})],
        "scrub_account: the rows stay as they were but for the address",
    )
    others = await _every_row(log, other_user_id)
    _check(
        theirs.id in others and others[theirs.id].target_email == other,
        "scrub_account: another account's rows keep their address, whoever acted",
    )
    unnamed = await _every_row(log, None)
    _check(
        invitation.id in unnamed and unnamed[invitation.id].target_email == current,
        "scrub_account: a row about no account keeps its address (scrub_email's)",
    )
    await log.scrub_account(user_id)
