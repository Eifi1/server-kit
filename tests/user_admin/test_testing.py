"""``user_admin.testing``: the in-memory roster keeps the port's promises, and each check
fails on a store that breaks one."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from eifi1_server_kit.auth.ports import NewAccount
from eifi1_server_kit.auth.testing import InMemoryAdminActionLog, InMemoryInvitationStore, InMemoryUserStore
from eifi1_server_kit.user_admin import parse_roster_query
from eifi1_server_kit.user_admin.ports import AcceptedInvitations, AccountLogScrub, RosterStore
from eifi1_server_kit.user_admin.testing import (
    InMemoryRosterStore,
    _matches_state,
    check_accepted_invitations,
    check_account_log_scrub,
    check_roster_store,
)


async def test_the_in_memory_roster_keeps_the_contract_with_a_demo() -> None:
    users = InMemoryUserStore()
    roster: RosterStore[Any] = InMemoryRosterStore(users.rows)
    await check_roster_store(roster, users, add_demo=users.add_demo)
    assert users.rows == {}, "the check cleans up after itself"


async def test_the_check_runs_beside_the_app_s_own_rows() -> None:
    users = InMemoryUserStore()
    await users.add(_new("own@example.org", "member"))
    await check_roster_store(InMemoryRosterStore(users.rows), users)
    assert [row.email for row in users.rows.values()] == ["own@example.org"]


async def test_a_reviewer_without_a_role_is_an_account_with_review_locales() -> None:
    users = InMemoryUserStore()
    plain = await users.add(_new("plain@example.org", "member"))
    reviewer = await users.add(_new("rev@example.org", "member"))
    await users.apply(reviewer, {"translation_review_locales": ["fr"]})
    roster = InMemoryRosterStore(users.rows)
    rows, total = await roster.page(parse_roster_query(state="reviewer"), admin_role="admin", reviewer_role=None)
    assert [row.id for row in rows] == [reviewer.id] and total == 1
    assert plain.id not in [row.id for row in rows]


async def test_an_unknown_sort_key_is_refused() -> None:
    roster = InMemoryRosterStore({})
    query = parse_roster_query(sort="plan", sort_keys=("plan",))
    with pytest.raises(ValueError, match="sorts by no plan"):
        await roster.page(query, admin_role="admin", reviewer_role="reviewer")


async def test_the_in_memory_invitations_forget_what_an_account_accepted() -> None:
    store = InMemoryInvitationStore()
    assert isinstance(store, AcceptedInvitations)
    await check_accepted_invitations(store, inviter_id=1, user_id=2, other_user_id=3)


async def test_a_store_without_forget_accepted_is_named() -> None:
    class Plain:
        pass

    with pytest.raises(TypeError, match="forget_accepted"):
        await check_accepted_invitations(Plain(), inviter_id=1, user_id=2, other_user_id=3)  # type: ignore[arg-type]


async def test_the_in_memory_log_scrubs_an_account_s_rows() -> None:
    log = InMemoryAdminActionLog()
    assert isinstance(log, AccountLogScrub)
    await check_account_log_scrub(log, actor_id=1, user_id=2, other_user_id=3)


async def test_the_in_memory_log_pages_by_address() -> None:
    from tests.user_admin._log_search import assert_pages_by_email

    await assert_pages_by_email(InMemoryAdminActionLog(), actor_id=1, user_id=2)


async def test_a_log_without_scrub_account_is_named() -> None:
    class Plain:
        pass

    with pytest.raises(TypeError, match="scrub_account"):
        await check_account_log_scrub(Plain(), actor_id=1, user_id=2, other_user_id=3)  # type: ignore[arg-type]


class _BrokenLog(InMemoryAdminActionLog):
    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    async def scrub_account(self, user_id: int) -> None:
        for row in self.rows.values():
            if self.broken == "lazy" and row.target_email is not None and "earlier" in row.target_email:
                continue
            if row.target_user_id == user_id or self.broken == "greedy":
                row.target_email = None
            if self.broken == "drops" and row.target_user_id == user_id:
                row.detail = {}


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ("lazy", "every row about the account"),
        ("greedy", "another account's rows keep"),
        ("drops", "the rows stay as they were"),
    ],
)
async def test_the_log_scrub_check_fails_on_a_log_that_breaks_a_promise(broken: str, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        await check_account_log_scrub(_BrokenLog(broken), actor_id=1, user_id=2, other_user_id=3)


# ── Broken stores the checks catch ──────────────────────────────────────────


class _Broken(InMemoryRosterStore):
    """One promise broken at a time."""

    def __init__(self, rows: Any, broken: str) -> None:
        super().__init__(rows)
        self.broken = broken

    async def page(self, query: Any, *, admin_role: str, reviewer_role: str | None) -> tuple[Sequence[Any], int]:
        if self.broken == "unescaped" and query.q is not None and query.q.startswith("%"):
            query = query.model_copy(update={"q": query.q.lstrip("%")})
        if self.broken == "state_and" and len(query.state) > 1:
            rows, _ = await super().page(
                query.model_copy(update={"state": (), "limit": 200, "offset": 0}),
                admin_role=admin_role,
                reviewer_role=reviewer_role,
            )
            kept = [
                row
                for row in rows
                if all(_matches_state(row, token, admin_role, reviewer_role) for token in query.state)
            ]
            return kept[query.offset : query.offset + query.limit], len(kept)
        rows, total = await super().page(query, admin_role=admin_role, reviewer_role=reviewer_role)
        if self.broken == "nulls_first" and query.sort[0].key == "last_login":
            rows = sorted(rows, key=lambda row: row.last_login_at is not None)
        if self.broken == "page_total":
            total = len(rows)
        if self.broken == "demo":
            demos = [row for row in self.rows.values() if getattr(row, "is_demo", False)]
            rows, total = [*rows, *demos][: query.limit], total + len(demos)
        return rows, total

    async def deletion_requested(self) -> Sequence[int]:
        found = list(await super().deletion_requested())
        return list(reversed(found)) if self.broken == "newest_first" else found


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ("unescaped", "a % in q matches itself"),
        ("state_and", "state tokens are OR-ed"),
        ("nulls_first", "last_login ascends"),
        ("page_total", "the total counts every match"),
        ("demo", "never a demo account"),
        ("newest_first", "the oldest first"),
    ],
)
async def test_the_roster_check_fails_on_a_store_that_breaks_a_promise(broken: str, message: str) -> None:
    users = InMemoryUserStore()
    with pytest.raises(AssertionError, match=message):
        await check_roster_store(_Broken(users.rows, broken), users, add_demo=users.add_demo)


async def test_the_invitation_check_fails_on_a_store_that_forgets_too_much() -> None:
    class Greedy(InMemoryInvitationStore):
        async def forget_accepted(self, user_id: int) -> Sequence[str]:
            gone = [row for row in self.rows.values() if row.accepted_at is not None]
            for row in gone:
                del self.rows[row.id]
            return [row.email for row in gone if row.accepted_by_user_id == user_id]

    with pytest.raises(AssertionError, match="another account's stay"):
        await check_accepted_invitations(Greedy(), inviter_id=1, user_id=2, other_user_id=3)


def _new(email: str, role: str) -> NewAccount:
    return NewAccount(email=email, password_hash="h", first_name="A", last_name="B", locale="en", role=role)


async def test_the_scrub_suite_reads_a_log_longer_than_one_page() -> None:
    """``_every_row`` pages through ``page`` 200 rows at a time: a log with more is read whole."""
    from datetime import UTC, datetime

    from eifi1_server_kit.user_admin import AdminAction
    from eifi1_server_kit.user_admin.audit import admin_action_record
    from eifi1_server_kit.user_admin.testing import _every_row

    log = InMemoryAdminActionLog()
    now = datetime(2026, 11, 14, tzinfo=UTC)
    for _ in range(201):
        await log.add(
            admin_action_record(AdminAction.DEACTIVATE, actor_id=1, target_user_id=2, target_email=None, now=now)
        )
    assert len(await _every_row(log, 2)) == 201
