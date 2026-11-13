"""``auth.testing`` (auth §12.3): the stores and the fakes keep the ports' promises, every
check passes on the stores, and each fails on a store that breaks one."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from eifi1_server_kit.auth import absent_account_check, access_claims
from eifi1_server_kit.auth.ports import (
    ACCOUNT_FIELDS,
    AdminActionLog,
    AfterCommit,
    InvitationStore,
    NewAccount,
    NewInvitation,
    NewPasskey,
    NewSession,
    OneTimeTokenStore,
    PasskeyStore,
    PasswordHasher,
    SessionStore,
    TokenSigner,
    UserStore,
)
from eifi1_server_kit.auth.testing import (
    _ACCOUNT_CLEARS,
    FakePasswordHasher,
    HmacTokenSigner,
    InMemoryAdminActionLog,
    InMemoryAfterCommit,
    InMemoryInvitationStore,
    InMemoryOneTimeTokenStore,
    InMemoryPasskeyStore,
    InMemorySessionStore,
    InMemoryUserStore,
    MemoryAccountRow,
    MemoryAdminActionRow,
    MemoryCutoffAccountRow,
    MemoryInvitationRow,
    MemoryOneTimeTokenRow,
    MemoryPasskeyRow,
    MemorySessionRow,
    _account_changes,
    _b64,
    check_admin_action_log,
    check_after_commit,
    check_invitation_store,
    check_one_time_token_store,
    check_passkey_store,
    check_session_store,
    check_user_store,
)
from eifi1_server_kit.auth.tokens import OneTimeTokenKind
from eifi1_server_kit.clock import Clock

AT = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LATER = AT + timedelta(days=1)


def fixed(at: datetime = AT) -> Clock:
    """Every ``created_at`` the same instant, as one Postgres transaction's ``now()``."""
    return lambda: at


def new_account(email: str = "ada@example.test") -> NewAccount:
    return NewAccount(email=email, password_hash="h", first_name="Ada", last_name="", locale="de-CH", role="member")


def test_the_memory_stores_and_the_fakes_are_the_ports() -> None:
    """mypy checks each assignment against its protocol."""
    users: UserStore[MemoryAccountRow] = InMemoryUserStore()
    sessions: SessionStore[MemorySessionRow] = InMemorySessionStore()
    links: OneTimeTokenStore[MemoryOneTimeTokenRow] = InMemoryOneTimeTokenStore()
    invitations: InvitationStore[MemoryInvitationRow] = InMemoryInvitationStore()
    passkeys: PasskeyStore[MemoryPasskeyRow] = InMemoryPasskeyStore()
    log: AdminActionLog[MemoryAdminActionRow] = InMemoryAdminActionLog()
    after_commit: AfterCommit = InMemoryAfterCommit()
    signer: TokenSigner = HmacTokenSigner()
    hasher: PasswordHasher = FakePasswordHasher()
    assert None not in (users, sessions, links, invitations, passkeys, log, after_commit, signer, hasher)


# ── The account store ───────────────────────────────────────────────────────


async def test_check_user_store_passes_with_or_without_the_cutoff_a_demo_or_ties() -> None:
    for store in (InMemoryUserStore(), InMemoryUserStore(cutoff=True), InMemoryUserStore(now=fixed())):
        await check_user_store(store)
        # Twice on the same store, and it leaves nothing behind.
        await check_user_store(store, add_demo=store.add_demo, role="MEMBER", other_role="ADMIN")
        assert store.rows == {}


def test_the_checks_changes_cover_every_member_the_kit_may_change() -> None:
    """A member added to ``ACCOUNT_FIELDS`` must be added to the check's changes too."""
    assert set(_account_changes("token", "admin")) == ACCOUNT_FIELDS
    assert set(_ACCOUNT_CLEARS) <= ACCOUNT_FIELDS


async def test_an_account_without_the_cutoff_ignores_its_write() -> None:
    plain, cut = InMemoryUserStore(), InMemoryUserStore(cutoff=True)
    plain_row = await plain.apply(await plain.add(new_account()), {"sessions_invalid_before": AT})
    cut_row = await cut.apply(await cut.add(new_account()), {"sessions_invalid_before": AT})
    assert not hasattr(plain_row, "sessions_invalid_before")
    assert isinstance(cut_row, MemoryCutoffAccountRow) and cut_row.sessions_invalid_before == AT


async def test_apply_refuses_a_member_the_kit_may_not_change() -> None:
    store = InMemoryUserStore()
    row = await store.add(new_account())
    with pytest.raises(ValueError, match="id, is_demo"):
        await store.apply(row, {"is_demo": True, "id": 9})


async def test_apply_keeps_a_copy_of_the_review_scope() -> None:
    store = InMemoryUserStore()
    row = await store.add(new_account())
    locales = ["fr"]
    await store.apply(row, {"translation_review_locales": locales, "translation_review_areas": ("auth",)})
    locales.append("it")
    assert row.translation_review_locales == ["fr"] and row.translation_review_areas == ["auth"]


async def test_a_deleted_accounts_id_never_comes_back() -> None:
    store = InMemoryUserStore(now=fixed())
    first = await store.add(new_account("a@example.test"))
    await store.delete(first)
    second = await store.add(new_account("b@example.test"))
    assert second.id != first.id and second.created_at == AT


async def test_a_demo_account_is_no_person() -> None:
    store = InMemoryUserStore()
    await store.add(new_account("a@example.test"))
    demo = await store.add_demo(new_account("demo@example.test"))
    assert demo.is_demo and await store.count_people() == 1


async def test_check_user_store_fails_on_a_store_that_counts_demo_accounts() -> None:
    class CountsDemos(InMemoryUserStore):
        async def count_people(self) -> int:
            return len(self.rows)

    store = CountsDemos()
    with pytest.raises(AssertionError, match="count_people: a demo account is no person"):
        await check_user_store(store, add_demo=store.add_demo)


async def test_check_user_store_fails_on_a_store_that_counts_only_active_accounts() -> None:
    """A deactivated admin left out of the count would make the next sign-up the first account."""

    class ActiveOnly(InMemoryUserStore):
        async def count_people(self) -> int:
            return sum(row.is_active and not row.is_demo for row in self.rows.values())

    with pytest.raises(AssertionError, match="count_people: a deactivated account"):
        await check_user_store(ActiveOnly())


async def test_check_user_store_fails_on_a_store_that_skips_a_none() -> None:
    class SkipsNone(InMemoryUserStore):
        async def apply(self, row: MemoryAccountRow, changes: Mapping[str, Any]) -> MemoryAccountRow:
            return await super().apply(row, {name: value for name, value in changes.items() if value is not None})

    with pytest.raises(AssertionError, match="apply: email_verified_at reads back as None"):
        await check_user_store(SkipsNone())


async def test_check_user_store_fails_on_a_lookup_that_ignores_the_address_tag() -> None:
    class Untagged(InMemoryUserStore):
        async def by_email(self, email: str) -> MemoryAccountRow | None:
            local, _, domain = email.partition("@")
            return await super().by_email(f"{local.partition('+')[0]}@{domain}")

    with pytest.raises(AssertionError, match="by_email: exactly the address, not a tagged variant"):
        await check_user_store(Untagged())


async def test_check_user_store_fails_on_an_empty_lookup_that_answers_everyone() -> None:
    class EmptyIn(InMemoryUserStore):
        async def by_emails(self, emails: Sequence[str]) -> Sequence[MemoryAccountRow]:
            return await super().by_emails(emails) if emails else list(self.rows.values())

    with pytest.raises(AssertionError, match="by_emails: no addresses, no accounts"):
        await check_user_store(EmptyIn())


async def test_check_user_store_fails_on_an_address_index_an_address_change_leaves_behind() -> None:
    class StaleIndex(InMemoryUserStore):
        def __init__(self) -> None:
            super().__init__()
            self.index: dict[str, MemoryAccountRow] = {}

        async def add(self, new: NewAccount) -> MemoryAccountRow:
            row = await super().add(new)
            self.index[row.email] = row
            return row

        async def by_email(self, email: str) -> MemoryAccountRow | None:
            return self.index.get(email)

    with pytest.raises(AssertionError, match="apply: the old address finds the account no longer"):
        await check_user_store(StaleIndex())


async def test_check_user_store_fails_on_an_account_that_starts_inactive() -> None:
    class Inactive(InMemoryUserStore):
        async def add(self, new: NewAccount) -> MemoryAccountRow:
            row = await super().add(new)
            row.is_active = False
            return row

    with pytest.raises(AssertionError, match="add: a new account is active"):
        await check_user_store(Inactive())


async def test_check_user_store_fails_on_a_store_that_loses_the_account_it_changed() -> None:
    class Forgets(InMemoryUserStore):
        async def apply(self, row: MemoryAccountRow, changes: Mapping[str, Any]) -> MemoryAccountRow:
            await super().apply(row, changes)
            self.rows.pop(row.id)
            return row

    with pytest.raises(AssertionError, match="apply: get still finds the account"):
        await check_user_store(Forgets())


async def test_check_user_store_fails_on_a_delete_that_keeps_the_row() -> None:
    class KeepsRows(InMemoryUserStore):
        async def delete(self, row: MemoryAccountRow) -> None:
            return None

    with pytest.raises(AssertionError, match="delete: get finds the account no longer"):
        await check_user_store(KeepsRows())


# ── Sessions ────────────────────────────────────────────────────────────────


async def test_check_session_store_passes_on_the_memory_store_twice() -> None:
    store = InMemorySessionStore()
    await check_session_store(store, user_id=1, other_user_id=2)
    await check_session_store(store, user_id=1, other_user_id=2)
    assert not any(row.is_active for row in store.rows.values())


async def test_check_session_store_ends_and_counts_a_session_the_account_had_before() -> None:
    store = InMemorySessionStore()
    await store.start(NewSession(sid="before", user_id=1, started_at=AT, expires_at=LATER))
    await check_session_store(store, user_id=1, other_user_id=2)
    assert store.rows["before"].is_active is False


async def test_the_checks_need_two_different_accounts() -> None:
    with pytest.raises(ValueError, match="two different accounts"):
        await check_session_store(InMemorySessionStore(), user_id=1, other_user_id=1)


async def test_a_new_session_was_last_active_when_it_started_and_a_sid_is_taken_once() -> None:
    store = InMemorySessionStore()
    row = await store.start(NewSession(sid="s", user_id=1, started_at=AT, expires_at=LATER))
    assert row.last_active == AT and row.is_active
    with pytest.raises(ValueError, match="'s' exists"):
        await store.start(NewSession(sid="s", user_id=2, started_at=AT, expires_at=LATER))


async def test_check_session_store_fails_on_an_end_all_that_counts_ended_sessions() -> None:
    class CountsEnded(InMemorySessionStore):
        async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
            await super().end_all(user_id, keep=keep)
            return sum(row.user_id == user_id and row.sid != keep for row in self.rows.values())

    with pytest.raises(AssertionError, match="end_all: answers how many it ended"):
        await check_session_store(CountsEnded(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_an_end_all_that_ignores_keep() -> None:
    class IgnoresKeep(InMemorySessionStore):
        async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
            return await super().end_all(user_id)

    with pytest.raises(AssertionError, match="end_all: answers how many it ended"):
        await check_session_store(IgnoresKeep(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_an_end_all_that_ends_another_account() -> None:
    class EndsEveryone(InMemorySessionStore):
        async def end_all(self, user_id: int, *, keep: str | None = None) -> int:
            ended = await super().end_all(user_id, keep=keep)
            for row in self.rows.values():
                row.is_active = row.is_active and row.user_id == user_id
            return ended

    with pytest.raises(AssertionError, match="end_all: another account's session stays active"):
        await check_session_store(EndsEveryone(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_live_ordered_by_the_start() -> None:
    class ByStart(InMemorySessionStore):
        async def live(self, user_id: int) -> Sequence[MemorySessionRow]:
            return sorted(await super().live(user_id), key=lambda row: row.started_at, reverse=True)

    with pytest.raises(AssertionError, match="live: the latest activity first"):
        await check_session_store(ByStart(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_a_get_that_hides_an_ended_session() -> None:
    class HidesEnded(InMemorySessionStore):
        async def get(self, sid: str) -> MemorySessionRow | None:
            row = await super().get(sid)
            return row if row is not None and row.is_active else None

    with pytest.raises(AssertionError, match="end: get still finds an ended session"):
        await check_session_store(HidesEnded(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_a_touch_that_writes_nothing() -> None:
    class NoTouch(InMemorySessionStore):
        async def touch(self, session: MemorySessionRow, *, last_active: datetime, expires_at: datetime) -> None:
            return None

    with pytest.raises(AssertionError, match="touch: last_active and expires_at read back"):
        await check_session_store(NoTouch(), user_id=1, other_user_id=2)


# ── One-time links ──────────────────────────────────────────────────────────


async def test_check_one_time_token_store_passes_on_the_memory_store_twice() -> None:
    for store in (InMemoryOneTimeTokenStore(), InMemoryOneTimeTokenStore(now=fixed())):
        await check_one_time_token_store(store, user_id=1, other_user_id=2)
        await check_one_time_token_store(store, user_id=1, other_user_id=2)
        assert store.rows == {}


async def test_an_invitations_link_is_not_the_link_stores() -> None:
    with pytest.raises(ValueError, match="invitation store"):
        await InMemoryOneTimeTokenStore().put(
            OneTimeTokenKind.INVITATION, user_id=1, token_hash="h", email="a@example.test", expires_at=LATER
        )


async def test_check_one_time_token_store_fails_on_a_take_that_ignores_the_kind() -> None:
    class AnyKind(InMemoryOneTimeTokenStore):
        async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryOneTimeTokenRow | None:
            return next((row for row in self.rows.values() if row.token_hash == token_hash), None)

    with pytest.raises(AssertionError, match="take: a link of another kind is not this one"):
        await check_one_time_token_store(AnyKind(), user_id=1, other_user_id=2)


async def test_check_one_time_token_store_fails_on_a_take_that_hides_an_expired_link() -> None:
    """The kit decides on the expiry, so it can answer "expired" rather than "unknown"."""

    class HidesExpired(InMemoryOneTimeTokenStore):
        async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryOneTimeTokenRow | None:
            row = await super().take(kind, token_hash)
            return row if row is not None and row.expires_at > datetime.now(UTC) else None

    with pytest.raises(AssertionError, match="take: the link by its hash, expired or not"):
        await check_one_time_token_store(HidesExpired(), user_id=1, other_user_id=2)


async def test_check_one_time_token_store_fails_on_a_take_that_spends_the_link() -> None:
    class Spends(InMemoryOneTimeTokenStore):
        async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryOneTimeTokenRow | None:
            row = await super().take(kind, token_hash)
            if row is not None:
                await self.drop(kind, row.user_id)
            return row

    with pytest.raises(AssertionError, match="take: reading does not spend the link"):
        await check_one_time_token_store(Spends(), user_id=1, other_user_id=2)


async def test_check_one_time_token_store_fails_on_a_put_that_keeps_the_earlier_link() -> None:
    class KeepsEarlier(InMemoryOneTimeTokenStore):
        def __init__(self) -> None:
            super().__init__()
            self.earlier: list[MemoryOneTimeTokenRow] = []

        async def put(
            self, kind: OneTimeTokenKind, *, user_id: int, token_hash: str, email: str, expires_at: datetime
        ) -> MemoryOneTimeTokenRow:
            if (old := self.rows.get((kind, user_id))) is not None:
                self.earlier.append(old)
            return await super().put(kind, user_id=user_id, token_hash=token_hash, email=email, expires_at=expires_at)

        async def take(self, kind: OneTimeTokenKind, token_hash: str) -> MemoryOneTimeTokenRow | None:
            found = await super().take(kind, token_hash)
            return found or next((row for row in self.earlier if row.token_hash == token_hash), None)

    with pytest.raises(AssertionError, match="put: replaces the account's earlier link of this kind"):
        await check_one_time_token_store(KeepsEarlier(), user_id=1, other_user_id=2)


async def test_check_one_time_token_store_fails_on_a_drop_of_every_kind() -> None:
    class DropsAll(InMemoryOneTimeTokenStore):
        async def drop(self, kind: OneTimeTokenKind, user_id: int) -> None:
            for key in [key for key in self.rows if key[1] == user_id]:
                del self.rows[key]

    with pytest.raises(AssertionError, match="drop: the account's links of the other kinds stay"):
        await check_one_time_token_store(DropsAll(), user_id=1, other_user_id=2)


# ── Invitations ─────────────────────────────────────────────────────────────


async def test_check_invitation_store_passes_whether_a_revoke_keeps_the_row_or_not() -> None:
    for store in (
        InMemoryInvitationStore(),
        InMemoryInvitationStore(revoke_deletes=True),
        InMemoryInvitationStore(now=fixed()),
    ):
        await check_invitation_store(store, inviter_id=1, user_id=2)
        await check_invitation_store(store, inviter_id=1, user_id=1, role="ADMIN")


async def test_a_revoke_that_deletes_takes_the_invitation_away() -> None:
    store = InMemoryInvitationStore(revoke_deletes=True)
    row = await store.add(
        NewInvitation(email="a@example.test", token_hash="h", expires_at=LATER, locale="en", invited_by_user_id=None)
    )
    await store.revoke(row, at=AT)
    assert await store.get(row.id) is None and row.revoked_at is None


async def test_check_invitation_store_fails_on_an_open_for_that_hides_expired_ones() -> None:
    class HidesExpired(InMemoryInvitationStore):
        async def open_for(self, email: str) -> Sequence[MemoryInvitationRow]:
            now = datetime.now(UTC)
            return [row for row in await super().open_for(email) if row.expires_at is None or row.expires_at > now]

    with pytest.raises(AssertionError, match="open_for: an expired invitation is still open"):
        await check_invitation_store(HidesExpired(), inviter_id=1, user_id=2)


async def test_check_invitation_store_fails_on_an_open_for_that_keeps_revoked_ones() -> None:
    class KeepsRevoked(InMemoryInvitationStore):
        async def open_for(self, email: str) -> Sequence[MemoryInvitationRow]:
            return [row for row in self.rows.values() if row.email == email and row.accepted_at is None]

    with pytest.raises(AssertionError, match="open_for: not a revoked invitation"):
        await check_invitation_store(KeepsRevoked(), inviter_id=1, user_id=2)


async def test_check_invitation_store_fails_on_a_listing_with_accepted_ones() -> None:
    class ListsAll(InMemoryInvitationStore):
        async def listing(self) -> Sequence[MemoryInvitationRow]:
            return sorted(self.rows.values(), key=lambda row: (row.created_at, row.id), reverse=True)

    with pytest.raises(AssertionError, match="listing: not an accepted invitation"):
        await check_invitation_store(ListsAll(), inviter_id=1, user_id=2)


async def test_check_invitation_store_fails_on_a_listing_that_hides_a_kept_revoked_one() -> None:
    class HidesRevoked(InMemoryInvitationStore):
        async def listing(self) -> Sequence[MemoryInvitationRow]:
            return [row for row in await super().listing() if row.revoked_at is None]

    with pytest.raises(AssertionError, match="listing: a revoked invitation the store keeps is still listed"):
        await check_invitation_store(HidesRevoked(), inviter_id=1, user_id=2)


async def test_check_invitation_store_fails_on_a_listing_without_the_id_tie_break() -> None:
    class OldestFirst(InMemoryInvitationStore):
        async def listing(self) -> Sequence[MemoryInvitationRow]:
            return list(reversed(await super().listing()))

    with pytest.raises(AssertionError, match="listing: newest first, the higher id first on a tie"):
        await check_invitation_store(OldestFirst(now=fixed()), inviter_id=1, user_id=2)


async def test_check_invitation_store_fails_on_a_remint_that_keeps_the_old_link() -> None:
    class KeepsLink(InMemoryInvitationStore):
        async def remint(
            self, invitation: MemoryInvitationRow, *, token_hash: str, expires_at: datetime
        ) -> MemoryInvitationRow:
            invitation.expires_at = expires_at
            return invitation

    with pytest.raises(AssertionError, match="remint: the new token_hash and expires_at read back"):
        await check_invitation_store(KeepsLink(), inviter_id=1, user_id=2)


# ── Passkeys ────────────────────────────────────────────────────────────────


async def test_check_passkey_store_passes_on_the_memory_store_beside_other_passkeys() -> None:
    for store in (InMemoryPasskeyStore(), InMemoryPasskeyStore(now=fixed())):
        await store.add(NewPasskey(user_id=1, credential_id=b"theirs", public_key=b"k", sign_count=0, name="Own"))
        await check_passkey_store(store, user_id=1, other_user_id=2)
        await check_passkey_store(store, user_id=1, other_user_id=2)
        assert [row.credential_id for row in store.rows.values()] == [b"theirs"]
        assert len(store.spent) == 4


async def test_a_credential_id_is_registered_once() -> None:
    store = InMemoryPasskeyStore()
    await store.add(NewPasskey(user_id=1, credential_id=b"c", public_key=b"k", sign_count=0, name="Laptop"))
    with pytest.raises(ValueError, match="integrity error"):
        await store.add(NewPasskey(user_id=2, credential_id=b"c", public_key=b"k", sign_count=0, name="Phone"))


async def test_check_passkey_store_fails_on_a_challenge_that_can_be_spent_twice() -> None:
    class AlwaysSpends(InMemoryPasskeyStore):
        async def spend_challenge(self, challenge_hash: str, *, expires_at: datetime) -> bool:
            return True

    with pytest.raises(AssertionError, match="spend_challenge: a second spend of the same challenge is refused"):
        await check_passkey_store(AlwaysSpends(), user_id=1, other_user_id=2)


async def test_check_passkey_store_fails_on_passkeys_newest_first() -> None:
    class NewestFirst(InMemoryPasskeyStore):
        async def for_user(self, user_id: int) -> Sequence[MemoryPasskeyRow]:
            return list(reversed(await super().for_user(user_id)))

    with pytest.raises(AssertionError, match="for_user: oldest first, the lower id first on a tie"):
        await check_passkey_store(NewestFirst(), user_id=1, other_user_id=2)


async def test_check_passkey_store_fails_on_a_use_that_keeps_the_old_counter() -> None:
    class KeepsCounter(InMemoryPasskeyStore):
        async def used(self, passkey: MemoryPasskeyRow, *, sign_count: int, at: datetime) -> None:
            passkey.last_used_at = at

    with pytest.raises(AssertionError, match="used: the new counter and time read back"):
        await check_passkey_store(KeepsCounter(), user_id=1, other_user_id=2)


# ── The admin log ───────────────────────────────────────────────────────────


async def test_check_admin_action_log_passes_on_the_memory_log_beside_other_rows() -> None:
    log = InMemoryAdminActionLog()
    await log.add({"at": AT, "actor_id": 1, "action": "role", "target_user_id": 2, "target_email": "x@example.test"})
    await check_admin_action_log(log, actor_id=1, target_user_id=2)
    await check_admin_action_log(log, actor_id=2, target_user_id=2)
    assert len(log.rows) == 11


async def test_the_log_stamps_a_record_without_at_and_refuses_a_field_no_table_has() -> None:
    log = InMemoryAdminActionLog(now=fixed())
    row = await log.add({"action": "role"})
    assert row.at == AT and row.detail == {} and row.actor_id is None and row.target_email is None
    with pytest.raises(ValueError, match="secret"):
        await log.add({"action": "role", "secret": "x"})


async def test_check_admin_action_log_fails_on_a_page_ordered_by_id() -> None:
    class ById(InMemoryAdminActionLog):
        async def page(
            self, *, target_user_id: int | None, limit: int, offset: int
        ) -> tuple[Sequence[MemoryAdminActionRow], int]:
            rows, total = await super().page(target_user_id=target_user_id, limit=len(self.rows), offset=0)
            return sorted(rows, key=lambda row: row.id, reverse=True)[offset : offset + limit], total

    with pytest.raises(AssertionError, match="page: newest first, the higher id first on a tie"):
        await check_admin_action_log(ById(), actor_id=1, target_user_id=2)


async def test_check_admin_action_log_fails_on_a_total_of_the_page_alone() -> None:
    class PageTotal(InMemoryAdminActionLog):
        async def page(
            self, *, target_user_id: int | None, limit: int, offset: int
        ) -> tuple[Sequence[MemoryAdminActionRow], int]:
            rows, _ = await super().page(target_user_id=target_user_id, limit=limit, offset=offset)
            return rows, len(rows)

    with pytest.raises(AssertionError, match="page: at most limit rows, and always the total of all"):
        await check_admin_action_log(PageTotal(), actor_id=1, target_user_id=2)


async def test_check_admin_action_log_fails_on_a_page_that_ignores_the_offset() -> None:
    class NoOffset(InMemoryAdminActionLog):
        async def page(
            self, *, target_user_id: int | None, limit: int, offset: int
        ) -> tuple[Sequence[MemoryAdminActionRow], int]:
            return await super().page(target_user_id=target_user_id, limit=limit, offset=0)

    with pytest.raises(AssertionError, match="page: offset and limit walk the same order"):
        await check_admin_action_log(NoOffset(), actor_id=1, target_user_id=2)


async def test_check_admin_action_log_fails_when_no_target_means_the_rows_without_one() -> None:
    class NoneIsNone(InMemoryAdminActionLog):
        async def page(
            self, *, target_user_id: int | None, limit: int, offset: int
        ) -> tuple[Sequence[MemoryAdminActionRow], int]:
            rows = [row for row in self.rows.values() if row.target_user_id == target_user_id]
            ordered = sorted(rows, key=lambda row: (row.at, row.id), reverse=True)
            return ordered[offset : offset + limit], len(ordered)

    with pytest.raises(AssertionError, match=r"page\(target_user_id=None\): the total counts every row"):
        await check_admin_action_log(NoneIsNone(), actor_id=1, target_user_id=2)


async def test_check_admin_action_log_fails_on_a_scrub_of_every_address() -> None:
    class ScrubsAll(InMemoryAdminActionLog):
        async def scrub_email(self, email: str) -> None:
            for row in self.rows.values():
                row.target_email = None

    with pytest.raises(AssertionError, match="scrub_email: another address stays"):
        await check_admin_action_log(ScrubsAll(), actor_id=1, target_user_id=2)


async def test_check_admin_action_log_fails_on_a_scrub_that_drops_the_target() -> None:
    class DropsTarget(InMemoryAdminActionLog):
        async def scrub_email(self, email: str) -> None:
            for row in self.rows.values():
                if row.target_email == email:
                    row.target_email = row.target_user_id = None

    with pytest.raises(AssertionError, match="scrub_email: the rows stay, target_user_id with them"):
        await check_admin_action_log(DropsTarget(), actor_id=1, target_user_id=2)


# ── After the commit ────────────────────────────────────────────────────────


async def test_check_after_commit_passes_on_the_memory_queue_with_or_without_settle() -> None:
    after_commit = InMemoryAfterCommit()
    await check_after_commit(after_commit, commit=after_commit.run, rollback=after_commit.drop)
    assert after_commit.commits == 1 and len(after_commit.failures) == 1

    settled: list[int] = []

    async def settle() -> None:
        settled.append(1)

    again = InMemoryAfterCommit()
    await check_after_commit(again, commit=again.run, rollback=again.drop, settle=settle)
    assert settled


async def test_failing_work_is_logged_and_the_rest_runs(caplog: pytest.LogCaptureFixture) -> None:
    after_commit = InMemoryAfterCommit()
    ran: list[str] = []

    async def fails() -> None:
        raise LookupError("no such address")

    async def mails() -> None:
        ran.append("mail")
        after_commit.later(mails)  # queued while the queue runs: it waits for the next commit

    after_commit.later(fails)
    after_commit.later(mails)
    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.auth.testing"):
        await after_commit.commit_now()
    assert ran == ["mail"] and after_commit.commits == 1
    assert [type(error) for error in after_commit.failures] == [LookupError]
    assert "after the commit failed" in caplog.text
    assert after_commit.queue == [mails]
    await after_commit.drop()
    await after_commit.run()
    assert ran == ["mail"]


async def test_check_after_commit_fails_on_work_that_runs_when_it_is_queued() -> None:
    """Every app mailed before its commit (§12.2): a rollback then sent a dead link."""

    class Eager(InMemoryAfterCommit):
        def later(self, work: Callable[[], Awaitable[None]]) -> None:
            step = cast(Coroutine[Any, Any, None], work())
            with contextlib.suppress(StopIteration):
                step.send(None)

    eager = Eager()
    with pytest.raises(AssertionError, match="later: nothing runs before the commit"):
        await check_after_commit(eager, commit=eager.run, rollback=eager.drop)


async def test_check_after_commit_fails_on_a_rollback_that_keeps_the_queue() -> None:
    class KeepsQueue(InMemoryAfterCommit):
        async def drop(self) -> None:
            return None

    keeps = KeepsQueue()
    with pytest.raises(AssertionError, match="rollback: the queue is dropped"):
        await check_after_commit(keeps, commit=keeps.run, rollback=keeps.drop)


async def test_check_after_commit_fails_on_a_queue_that_stops_at_a_failure() -> None:
    class StopsAtFailure(InMemoryAfterCommit):
        async def run(self) -> None:
            queued, self.queue = self.queue, []
            for work in queued:
                try:
                    await work()
                except RuntimeError:
                    return

    stops = StopsAtFailure()
    with pytest.raises(AssertionError, match="commit: the queue runs, each item on its own"):
        await check_after_commit(stops, commit=stops.run, rollback=stops.drop)


async def test_check_after_commit_fails_on_a_queue_that_runs_twice() -> None:
    class RunsTwice(InMemoryAfterCommit):
        async def run(self) -> None:
            for work in list(self.queue):
                with contextlib.suppress(RuntimeError):
                    await work()

    twice = RunsTwice()
    with pytest.raises(AssertionError, match="commit: each item runs once"):
        await check_after_commit(twice, commit=twice.run, rollback=twice.drop)


async def test_check_after_commit_fails_on_a_commit_now_that_runs_nothing() -> None:
    class CountsOnly(InMemoryAfterCommit):
        async def commit_now(self) -> None:
            self.commits += 1

    counts = CountsOnly()
    with pytest.raises(AssertionError, match="commit_now: commits, so the queue runs"):
        await check_after_commit(counts, commit=counts.run, rollback=counts.drop)


# ── The fakes ───────────────────────────────────────────────────────────────


def test_the_fake_hasher_is_deterministic_and_never_raises_on_a_malformed_hash() -> None:
    hasher = FakePasswordHasher()
    hashed = hasher.hash("correct horse")
    assert hashed == hasher.hash("correct horse") != hasher.hash("correct horsf")
    assert hashed.startswith(FakePasswordHasher.PREFIX)
    assert hasher.verify("correct horse", hashed) and not hasher.verify("wrong", hashed)
    for malformed in ("", "garbage", "fake$", "€", "$2b$12$" + "x" * 53):
        assert hasher.verify("correct horse", malformed) is False
    assert hasher.hash("\ud800").startswith("fake$")  # a lone surrogate hashes all the same
    assert hasher.checks[:2] == [hashed, hashed] and len(hasher.checks) == 7


def test_the_fake_hasher_records_the_absent_accounts_check() -> None:
    hasher = FakePasswordHasher()
    check_absent = absent_account_check(hasher.hash, hasher.verify)
    assert check_absent("any password") is False
    assert len(hasher.checks) == 1 and hasher.checks[0].startswith("fake$")


def test_a_signed_token_verifies_to_its_claims_until_it_expires() -> None:
    claims = access_claims(7, now=AT, extra={"sid": "s"})
    exp = datetime.fromtimestamp(claims["exp"], UTC)
    token = HmacTokenSigner(b"secret", now=fixed(exp - timedelta(seconds=1))).sign(claims)
    assert HmacTokenSigner(b"secret", now=fixed(exp - timedelta(seconds=1))).verify(token) == claims
    assert HmacTokenSigner(b"secret", now=fixed(exp)).verify(token) is None, "expired at exp itself"
    assert HmacTokenSigner(now=fixed(AT)).verify(token) is None, "another secret: another app"


def test_a_token_of_any_other_shape_is_none() -> None:
    signer = HmacTokenSigner(now=fixed())
    token = signer.sign({"sub": "1", "exp": LATER.timestamp()})
    body, signature = token.split(".")
    forged = _b64(b'{"exp":9999999999,"sub":"2"}')
    for malformed in (
        "",
        "no-dot",
        f"{token}.extra",
        f"{forged}.{signature}",
        f"{body}.{signature[:-2]}",
        f"{body}=.{signature}",
        f"{body}.{signature}=",
        "é.é",
        "a.b",
    ):
        assert signer.verify(malformed) is None, malformed


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"\xff", b"[1]", b'{"sub":"1"}', b'{"exp":true}', b'{"exp":"9999999999"}', b'{"exp":NaN}'],
)
def test_a_signed_payload_without_a_finite_exp_in_an_object_is_none(payload: bytes) -> None:
    signer = HmacTokenSigner(now=fixed())
    assert signer.verify(signer._seal(payload)) is None


@pytest.mark.parametrize("exp", [None, "soon", True, float("nan"), float("inf")])
def test_signing_claims_without_a_finite_exp_is_refused(exp: object) -> None:
    claims: dict[str, object] = {"sub": "1"} if exp is None else {"sub": "1", "exp": exp}
    with pytest.raises(ValueError, match="finite exp"):
        HmacTokenSigner().sign(claims)


def test_the_software_authenticator_is_loaded_through_the_testing_module_on_first_use() -> None:
    from eifi1_server_kit.auth import soft_webauthn, testing

    assert testing.SoftAuthenticator is soft_webauthn.SoftAuthenticator
    assert testing.SoftCredential is soft_webauthn.SoftCredential
    with pytest.raises(AttributeError, match="has no attribute 'Nothing'"):
        testing.Nothing  # noqa: B018


async def test_check_user_store_fails_on_an_apply_that_drops_a_foreign_key() -> None:
    class DropsForeign(InMemoryUserStore):
        async def apply(self, row: MemoryAccountRow, changes: Mapping[str, Any]) -> MemoryAccountRow:
            return await super().apply(row, {name: value for name, value in changes.items() if name in ACCOUNT_FIELDS})

    with pytest.raises(AssertionError, match="apply: a key outside ACCOUNT_FIELDS raises ValueError"):
        await check_user_store(DropsForeign())


async def test_check_session_store_fails_on_live_without_the_sid_tie_break() -> None:
    class NoTieBreak(InMemorySessionStore):
        async def live(self, user_id: int) -> Sequence[MemorySessionRow]:
            rows = [row for row in self.rows.values() if row.user_id == user_id and row.is_active]
            return sorted(rows, key=lambda row: row.last_active, reverse=True)

    with pytest.raises(AssertionError, match="live: the higher sid first on a tie"):
        await check_session_store(NoTieBreak(), user_id=1, other_user_id=2)


async def test_check_session_store_fails_on_a_session_that_starts_idle() -> None:
    class StartsIdle(InMemorySessionStore):
        async def start(self, new: NewSession) -> MemorySessionRow:
            row = await super().start(new)
            row.last_active = new.started_at - timedelta(hours=1)
            return row

    with pytest.raises(AssertionError, match="start: last_active is started_at"):
        await check_session_store(StartsIdle(), user_id=1, other_user_id=2)
