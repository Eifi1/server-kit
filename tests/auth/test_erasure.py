"""The account layer's pieces the admin router and the erasure job share (0.30): ending
every session for a reason, the admin's mails, and the erasure (user-admin §6.4)."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from eifi1_server_kit.auth import OneTimeTokenKind, mint
from eifi1_server_kit.auth.service import AuthHooks, SessionsEndedReason
from eifi1_server_kit.limiter import ThrottledError
from eifi1_server_kit.user_admin import DeletionMode, MailKind, admin_action_record
from tests.auth._world import POLICY, World
from tests.auth_fakes import MemoryUser


class FakeFeedback:
    """``FeedbackService.erase_author``'s shape: what it was asked, and its answer."""

    def __init__(self, keys: tuple[str, ...] = ("feedback/a.png",)) -> None:
        self.asked: list[tuple[int, list[str | None]]] = []
        self.keys = keys

    async def erase_author(self, user_id: int, identifiers: Any) -> Any:
        self.asked.append((user_id, list(identifiers)))
        return SimpleNamespace(reports=2, keys=self.keys)


# ── every session, for a reason ─────────────────────────────────────────────


async def test_ending_every_session_ends_the_rows_the_pending_address_and_tells_the_app(cutoff_world: World) -> None:
    world = cutoff_world
    user = world.account()
    await world.sign_in(user)
    await world.sign_in(user)
    await world.tokens.put(
        OneTimeTokenKind.EMAIL_CHANGE,
        user_id=user.id,
        token_hash="h",
        email="new@example.com",
        expires_at=world.clock(),
    )
    told: list[tuple[int, str]] = []

    async def sessions_ended(account: MemoryUser, reason: SessionsEndedReason) -> None:
        told.append((account.id, reason))

    ended = await world.service(AuthHooks(sessions_ended=sessions_ended)).end_sessions(
        user, SessionsEndedReason.DEACTIVATED
    )
    assert ended == 2 and not any(row.is_active for row in world.sessions.of(user.id))
    assert user.sessions_invalid_before == world.clock()  # type: ignore[attr-defined]
    assert await world.tokens.pending(OneTimeTokenKind.EMAIL_CHANGE, user.id) is None
    assert told == [(user.id, "deactivated")]


async def test_a_new_app_ends_sessions_by_the_rows_alone(world: World) -> None:
    user = world.account()
    await world.sign_in(user)
    assert await world.service().end_sessions(user, SessionsEndedReason.PASSWORD_CHANGE_REQUIRED) == 1
    assert not hasattr(user, "sessions_invalid_before")
    assert str(SessionsEndedReason.PASSWORD_CHANGE_REQUIRED) == "password_change_required"


# ── the admin's mails ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "mail_kind", "link_path", "row_kind"),
    [
        (MailKind.VERIFICATION, "verification", "/verify-email?token=", OneTimeTokenKind.VERIFICATION),
        (MailKind.PASSWORD_RESET, "password_reset", "/reset-password?token=", OneTimeTokenKind.PASSWORD_RESET),
    ],
)
async def test_an_admin_s_mail_mints_its_link_commits_then_sends(
    world: World, kind: MailKind, mail_kind: str, link_path: str, row_kind: OneTimeTokenKind
) -> None:
    user = world.account(locale="fr")
    commits = world.after.commits
    sent, link = await world.service().send_account_mail(user, kind)
    assert sent is True and link_path in link
    (mail,) = world.mailer.of(mail_kind)
    assert mail.to == user.email and mail.after_commits == commits + 1, "the link is committed before it is mailed"
    assert link in mail.message.text
    pending = await world.tokens.pending(row_kind, user.id)
    assert pending is not None and pending.email == user.email
    world.mailer.works = False
    assert (await world.service().send_account_mail(user, kind))[0] is False


@pytest.mark.parametrize(("kind", "budget"), [(MailKind.VERIFICATION, 10), (MailKind.PASSWORD_RESET, 3)])
async def test_an_admin_s_mail_is_charged_to_the_recipient_s_window(world: World, kind: MailKind, budget: int) -> None:
    """The public flows' per-address windows (Kurvenschmiede): an admin's button is no faster
    way to fill somebody's inbox."""
    user = world.account()
    service = world.service()
    for _ in range(budget):
        await service.send_account_mail(user, kind)
    with pytest.raises(ThrottledError):
        await service.send_account_mail(user, kind)


# ── the erasure ─────────────────────────────────────────────────────────────


async def test_an_erasure_hands_over_anonymises_scrubs_and_deletes_the_account(world: World) -> None:
    admin = world.account(email="root@example.com", role="ADMIN")
    user = world.account(email="ada@example.com", is_active=False, deletion_requested_at=world.clock())
    await world.sign_in(admin)
    world.sessions.rows["s"] = replace(next(iter(world.sessions.rows.values())), sid="s", user_id=user.id)
    _raw, digest = mint()
    await world.tokens.put(
        OneTimeTokenKind.PASSWORD_RESET, user_id=user.id, token_hash=digest, email=user.email, expires_at=world.clock()
    )
    accepted = world.invitations.seed(
        email="ada+old@example.com", accepted_at=world.clock(), accepted_by_user_id=user.id
    )
    kept = world.invitations.seed(email="bob@example.com", accepted_at=world.clock(), accepted_by_user_id=admin.id)
    for address in (user.email, accepted.email, "bob@example.com"):
        await world.log.add(
            admin_action_record(
                "invite", actor_id=admin.id, target_user_id=None, target_email=address, now=world.clock()
            )
        )
    events: list[str] = []
    purged: list[tuple[str, ...]] = []

    async def erase(account: MemoryUser, *, actor_id: int | None) -> dict[str, Any]:
        events.append(f"erase:{account.id} by {actor_id}")
        return {"handed_over": {"projects": 3}}

    async def purge_files(keys: Any) -> None:
        purged.append(tuple(keys))

    feedback = FakeFeedback()
    detail = await world.service(AuthHooks(erase=erase, purge_files=purge_files)).erase(
        user,
        actor_id=admin.id,
        feedback=feedback,  # type: ignore[arg-type]
    )
    assert detail == {"feedback_anonymised": 2, "handed_over": {"projects": 3}}
    assert events == [f"erase:{user.id} by {admin.id}"], "the hook knows the admin who erased"
    (asked_id, identifiers) = feedback.asked[0]
    assert asked_id == user.id and "ada@example.com" in identifiers and "Ada Lovelace" in identifiers
    assert purged == [], "the files go after the commit"
    await world.after.finish()
    assert purged == [("feedback/a.png",)]
    assert await world.users.get(user.id) is None
    assert world.sessions.rows["s"].is_active is False
    assert await world.tokens.pending(OneTimeTokenKind.PASSWORD_RESET, user.id) is None
    assert accepted.id not in world.invitations.rows and kept.id in world.invitations.rows
    assert [row.target_email for row in world.log.rows[:3]] == [None, None, "bob@example.com"]
    erased = world.log.rows[-1]
    assert (erased.action, erased.actor_id, erased.target_user_id, erased.target_email) == (
        "erase",
        admin.id,
        None,
        None,
    )
    assert erased.detail == detail


async def test_an_erasure_without_feedback_files_or_hooks_still_erases(world: World) -> None:
    user = world.account(is_active=False, deletion_requested_at=world.clock())
    assert await world.service().erase(user, actor_id=None) == {"feedback_anonymised": 0}
    assert await world.users.get(user.id) is None
    assert world.log.rows[-1].actor_id is None, "the job erases with no actor"
    await world.service().erase(world.account(email="b@example.com"), actor_id=None, feedback=FakeFeedback())  # type: ignore[arg-type]
    assert world.after.queue == [], "files with nowhere to go are not queued"
    await world.service().erase(world.account(email="c@example.com"), actor_id=None, feedback=FakeFeedback(keys=()))  # type: ignore[arg-type]


async def test_the_job_s_erasure_tells_the_hook_it_has_no_actor(world: World) -> None:
    actors: list[int | None] = []

    async def erase(account: MemoryUser, *, actor_id: int | None) -> None:
        actors.append(actor_id)

    user = world.account(is_active=False, deletion_requested_at=world.clock() - timedelta(days=31))
    assert await world.service(AuthHooks(erase=erase)).erase_if_due(user.id) is True
    assert actors == [None]


async def test_the_job_erases_only_an_account_whose_deletion_is_due(world: World) -> None:
    asked = world.clock() - timedelta(days=30)
    due = world.account(email="due@example.com", is_active=False, deletion_requested_at=asked)
    soon = world.account(email="soon@example.com", is_active=False, deletion_requested_at=asked + timedelta(hours=1))
    later = world.account(
        email="later@example.com",
        is_active=False,
        deletion_requested_at=asked,
        deletion_scheduled_at=world.clock() + timedelta(days=1),
    )
    back = world.account(email="back@example.com", is_active=True, deletion_requested_at=asked)
    never = world.account(email="never@example.com", is_active=False)
    service = world.service()
    assert await service.erase_if_due(due.id) is True
    assert await world.users.get(due.id) is None
    for account in (soon, later, back, never):
        assert await service.erase_if_due(account.id) is False, account.email
    assert await service.erase_if_due(due.id) is False, "gone: nothing to do, no error"
    operator = world.service(policy=replace(POLICY, deletion_mode=DeletionMode.OPERATOR))
    world.clock.advance(days=2)
    assert await operator.erase_if_due(soon.id) is False, "an operator erases, never the job"
    assert await service.erase_if_due(soon.id, feedback=FakeFeedback()) is True  # type: ignore[arg-type]


async def test_an_invitation_store_without_forget_accepted_leaves_the_invitations_to_the_app(world: World) -> None:
    """An app's own store adds the method or forgets them in its ``erase`` hook; the kit
    scrubs the account's own address all the same."""

    class Plain:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "forget_accepted":
                raise AttributeError(name)
            return getattr(self.inner, name)

    user = world.account(is_active=False, deletion_requested_at=world.clock())
    accepted = world.invitations.seed(email="old@example.com", accepted_at=world.clock(), accepted_by_user_id=user.id)
    await world.log.add(
        admin_action_record("invite", actor_id=None, target_user_id=None, target_email=user.email, now=world.clock())
    )
    await world.service(invitations=Plain(world.invitations)).erase(user, actor_id=None)
    assert accepted.id in world.invitations.rows and world.log.rows[0].target_email is None
