"""The teams' flows: creating, renaming and deleting a team, who is in it and who manages it,
and invitations into it — Kurvenschmiede's ``team_service`` and its team invitations, written
once against :class:`~eifi1_server_kit.teams.ports.TeamStore` and the account layer.

Three audiences, and the policy for all of them is here (the routes are
:mod:`eifi1_server_kit.teams.router`):

* **an admin** creates, renames and deletes teams, adds and removes anybody, and decides who
  manages;
* **a manager** adds and removes the *members* of a team they manage — not its managers, which
  stay the admin's decision (``manager_named_by_admin``, ``manager_kept_by_admin``,
  ``manager_removed_by_admin``), and not of any other team: :meth:`TeamsService.managed` is
  the predicate, and a team they do not manage is the ``404`` a stranger gets;
* **a member** sees the teams they are in and who else is in them.

**The rules** (Kurvenschmiede's review of the plan, 2026-11-14):

* the version moves with a rename and every change of who is in the team — an add, a role
  change, a removal, an invitation into it, a join by one — and a stale ``If-Match`` is
  ``team_changed``, carrying the team's name; one not sent is never demanded;
* adding somebody already in the team is a role change (``membership_role``), and the role
  they have changes nothing; **a change that changes nothing writes no row**;
* an address is trimmed and lower-cased, refused by the app's ``refuse_address`` hook (a
  demo's, by its shape) before it is looked up, and an address with no account is INVITED into
  the team through the account layer (:meth:`~eifi1_server_kit.auth.service.AccountService.create_invitation`,
  its scope :func:`~eifi1_server_kit.teams.ports.team_scope`): in the language picked, else the
  inviter's; an open invitation of the address into the same team is sent again instead,
  in the role asked for;
* **the admin log** names the team as a column (``target_group_id``), never in ``detail``:
  ``group_create`` and ``group_rename`` (``{}``), ``group_delete`` (``{members, grants,
  invitations}`` and the name the team had, ``target_group_name``), ``membership_add``
  (``{role}``), ``membership_role`` (``{from, to}``), ``membership_remove`` (``{role}``); a join
  by invitation is a ``membership_add`` whose actor is the invitee;
* **the system team** (``is_system``, a demo's) is a ``404`` everywhere; a demo is put in it by
  :func:`add_system_member`, with no row and no notice.

**The hooks** (:class:`TeamHooks`, every one optional) are what is the app's: the grants a
team holds (``deleting``), the notice of a new membership (``member_added``), the billing gate
on a manager's add and re-send (``billing_gate``), and the refusal of a demo's address
(``refuse_address``).

**Joining by invitation** happens in the account layer's flows — a sign-up, ``POST
/auth/invitations/accept``, an address confirmation — through the app's ``AuthHooks``:
:class:`TeamAccountHooks` has the ready coroutines to wire there, and ``group_names`` for the
admin log's ``UserAdminHooks``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from eifi1_server_kit.auth.accounts import normalise_email
from eifi1_server_kit.auth.errors import AuthError, AuthErrorCode
from eifi1_server_kit.auth.ports import AccountRow, AdminActionLog, InvitationRow, InvitationStore, UserStore
from eifi1_server_kit.auth.schemas import RegisterRequest
from eifi1_server_kit.auth.service import AccountService
from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.teams.errors import TeamError, TeamErrorCode
from eifi1_server_kit.teams.ports import (
    Membership,
    NewTeam,
    TeamRow,
    TeamStore,
    invitation_team,
    invitation_team_role,
    team_scope,
)
from eifi1_server_kit.teams.schemas import TeamRole
from eifi1_server_kit.user_admin.actions import AdminAction, confirmation_level
from eifi1_server_kit.user_admin.audit import admin_action_record
from eifi1_server_kit.user_admin.ports import DeletedGroupNames
from eifi1_server_kit.user_admin.rules import require_confirmation
from eifi1_server_kit.user_admin.schemas import GroupRef, InvitationCreate

__all__ = [
    "TeamAccountHooks",
    "TeamGate",
    "TeamHooks",
    "TeamInvitationSent",
    "TeamView",
    "TeamsService",
    "add_system_member",
    "join_team",
]

#: What a manager does that the app's billing gate is asked about: an add, a re-send.
type TeamGate = Literal["add", "resend"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamHooks[UserT: AccountRow = Any]:
    """What is the app's around its teams, as optional coroutines its hook dependency answers
    per request, on the request's own session. Each may raise a kit refusal to refuse the step."""

    #: The grants these teams hold, by id (Kurvenschmiede's shares to a team), in one call:
    #: the admin list's ``grant_count`` and the ``grants`` a ``group_delete`` row counts. An id
    #: left out holds none; without the hook no team holds any.
    deleting: Callable[[Sequence[int]], Awaitable[Mapping[int, int]]] | None = None
    #: ``(actor, team, user)``: a NEW membership made by an add (Kurvenschmiede's
    #: ``team_added`` notice) — not a role change, not a join by invitation (which has its own
    #: mail and is the invitee's own doing), never the system team's.
    member_added: Callable[[UserT, TeamRow, UserT], Awaitable[None]] | None = None
    #: ``(manager, "add" | "resend")``: a manager's add or re-send, on the manager's own
    #: standing (Kurvenschmiede's billing contract §12.13). Never a removal, a withdrawal or an
    #: admin's.
    billing_gate: Callable[[UserT, TeamGate], Awaitable[None]] | None = None
    #: The address of an add, trimmed and lower-cased, before it is looked up: refuse one the
    #: app never puts in a team (Kurvenschmiede's demo addresses, by their shape, a ``400``).
    refuse_address: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class TeamView:
    """One team as a viewer sees it: the caller's role in it (``None`` from the admin page),
    everybody in it, the invitations waiting (for an admin and the team's managers; empty for
    everybody else), and on the admin's list the grants it holds."""

    team: TeamRow
    my_role: TeamRole | None
    members: Sequence[Membership[Any, Any]]
    invitations: Sequence[InvitationRow] = ()
    grants: int = 0


@dataclass(frozen=True, slots=True)
class TeamInvitationSent:
    """The answer of an add that invited (or re-sent an invitation): the address, whether the
    mail went, and the link only while mail goes to the console."""

    email: str
    sent: bool
    link: str | None


async def _record(
    log: AdminActionLog[Any],
    action: AdminAction,
    *,
    actor: AccountRow | None,
    team: TeamRow,
    target: AccountRow | None = None,
    detail: Mapping[str, object] | None = None,
    team_name: str | None = None,
    now: Clock,
) -> None:
    await log.add(
        admin_action_record(
            action,
            actor_id=None if actor is None else actor.id,
            target_user_id=None if target is None else target.id,
            target_email=None if target is None else target.email,
            detail=detail,
            target_group_id=team.id,
            target_group_name=team_name,
            now=now(),
        )
    )


def _not_found(detail: str = "Team not found") -> TeamError:
    return TeamError(TeamErrorCode.TEAM_NOT_FOUND, detail)


def _by_team(memberships: Sequence[Membership[Any, Any]]) -> dict[int, list[Membership[Any, Any]]]:
    found: dict[int, list[Membership[Any, Any]]] = {}
    for entry in memberships:
        found.setdefault(entry.member.team_id, []).append(entry)
    return found


async def join_team(
    teams: TeamStore[Any, Any],
    log: AdminActionLog[Any],
    invitation: InvitationRow,
    user: AccountRow,
    *,
    now: Clock = utc_now,
) -> bool:
    """The team an invitation names, in its role, for the account that took it up — at a
    sign-up, at ``POST /auth/invitations/accept`` and at an address confirmation alike (the
    account layer stamps the invitation itself). A ``membership_add`` whose actor is the
    invitee: whoever invited chose the team, and the invitee chose to come in. An invitation
    that names no team, a team that is gone, or an account already in it changes nothing and
    writes nothing. Answers whether it joined."""
    team_id = invitation_team(invitation)
    team = None if team_id is None else await teams.get(team_id)
    if team is None or await teams.membership(team.id, user.id) is not None:
        return False
    role = invitation_team_role(invitation)
    await teams.add_member(team.id, user.id, role)
    await teams.bump(team)
    await _record(
        log, AdminAction.MEMBERSHIP_ADD, actor=user, team=team, target=user, detail={"role": role.value}, now=now
    )
    return True


async def add_system_member(teams: TeamStore[Any, Any], team: TeamRow, user: AccountRow) -> bool:
    """Put a demo account in the app's system team (Kurvenschmiede's "Demo" team, through which
    every demo reads the worked examples), as a member: no row in the admin log, no notice, no
    new version — the team is the app's, hidden everywhere. Answers whether it was added; an
    account in it already is left as it is. A team that is no system team is a
    :class:`ValueError`."""
    if not team.is_system:
        raise ValueError("add_system_member puts a demo in the app's system team, and this is no system team")
    if await teams.membership(team.id, user.id) is not None:
        return False
    await teams.add_member(team.id, user.id, TeamRole.MEMBER)
    return True


class TeamAccountHooks[UserT: AccountRow = Any]:
    """The teams' ready coroutines for the account layer's hooks and the admin log, on one
    request's team store and log::

        teams = TeamAccountHooks(SqlTeamStore(session, Team, TeamMember, users=User,
                                              invitations=Invitation),
                                 SqlAdminActionLog(session, AdminActionRecord))
        AuthHooks(on_register=teams.on_register, on_invitation_redeemed=teams.on_invitation_redeemed,
                  invitation_group=teams.invitation_group, invitation_scope=teams.invitation_scope,
                  describe_invitation=teams.describe_invitation)
        UserAdminHooks(group_names=teams.group_names)

    An app whose own hook does more (Kurvenschmiede's sign-up starts a subscription) calls
    these from it."""

    def __init__(self, teams: TeamStore[Any, Any], log: AdminActionLog[Any], *, now: Clock = utc_now) -> None:
        self._teams = teams
        self._log = log
        self._now = now

    async def join(self, invitation: InvitationRow, user: UserT) -> bool:
        """:func:`join_team` on this request's stores."""
        return await join_team(self._teams, self._log, invitation, user, now=self._now)

    async def on_register(self, user: UserT, invitation: InvitationRow | None, _body: RegisterRequest) -> None:
        """``AuthHooks.on_register``: a sign-up through a team invitation joins its team."""
        if invitation is not None:
            await self.join(invitation, user)

    async def on_invitation_redeemed(self, user: UserT, invitation: InvitationRow) -> None:
        """``AuthHooks.on_invitation_redeemed``: an existing account took up a team invitation
        (``/auth/invitations/accept``), or confirmed the address one was sent to."""
        await self.join(invitation, user)

    async def _team(self, invitation: InvitationRow) -> TeamRow | None:
        team_id = invitation_team(invitation)
        return None if team_id is None else await self._teams.get(team_id)

    async def invitation_group(self, invitation: InvitationRow) -> GroupRef | None:
        """``AuthHooks.invitation_group``: the team an invitation is for — its id on the
        invitation's log rows, its name in the mail."""
        team = await self._team(invitation)
        return None if team is None else GroupRef(id=team.id, name=team.name)

    async def invitation_scope(self, _admin: UserT, scope: int | str) -> Mapping[str, Any]:
        """``AuthHooks.invitation_scope``: an admin's ``scope`` on ``POST /admin/invitations`` is
        a team to invite into, as a member; the system team is hidden here too. Any other is
        ``invitation_scope_invalid``."""
        try:
            team = await self._teams.get(int(scope))
        except ValueError as exc:
            raise AuthError(AuthErrorCode.INVITATION_SCOPE_INVALID) from exc
        if team is None or team.is_system:
            raise AuthError(AuthErrorCode.INVITATION_SCOPE_INVALID)
        return team_scope(team.id, TeamRole.MEMBER)

    async def describe_invitation(self, invitation: InvitationRow) -> dict[str, Any]:
        """``AuthHooks.describe_invitation``: the team an invitation brings the person into, on
        the check, the acceptance and the admin's rows — its id (the contract's ``scope``), its
        name and the role in it (Kurvenschmiede's three fields)."""
        team_id = invitation_team(invitation)
        if team_id is None:
            return {"scope": None, "team_name": None, "team_role": None}
        team = await self._teams.get(team_id)
        return {
            "scope": team_id,
            "team_name": None if team is None else team.name,
            "team_role": invitation_team_role(invitation).value,
        }

    async def group_names(self, group_ids: Sequence[int]) -> Mapping[int, str | None]:
        """``UserAdminHooks.group_names``: the teams the log names — the live team's name, else
        the one its ``group_delete`` row kept (with a log that reads it,
        :class:`~eifi1_server_kit.user_admin.ports.DeletedGroupNames`)."""
        wanted = sorted(set(group_ids))
        names: dict[int, str | None] = dict(await self._teams.names(wanted))
        gone = [group_id for group_id in wanted if group_id not in names]
        if gone and isinstance(self._log, DeletedGroupNames):
            names.update(await self._log.deleted_group_names(gone))
        return names


class TeamsService[UserT: AccountRow = Any]:
    """The teams' flows for one request, over the team store and the account layer's stores and
    service (the invitations and their mail are the account layer's).

    ``invitation_role`` is the account role an invitation into a team gives a new account (the
    policy's ``default_role``); ``show_link`` answers an invitation's link while mail goes to
    the console."""

    def __init__(
        self,
        *,
        teams: TeamStore[Any, Any],
        users: UserStore[Any],
        invitations: InvitationStore[Any],
        log: AdminActionLog[Any],
        accounts: AccountService[Any, Any, Any],
        invitation_role: str,
        hooks: TeamHooks[UserT] | None = None,
        show_link: bool = False,
        now: Clock = utc_now,
    ) -> None:
        self._teams = teams
        self._users = users
        self._invitations = invitations
        self._log = log
        self._accounts = accounts
        self._invitation_role = invitation_role
        self._hooks: TeamHooks[UserT] = hooks or TeamHooks()
        self._show_link = show_link
        self._now = now

    # --- reading -------------------------------------------------------------------------

    async def get(self, team_id: int) -> TeamRow:
        """A team the admin may act on: any but a system team, else ``team_not_found``."""
        team: TeamRow | None = await self._teams.get(team_id)
        if team is None or team.is_system:
            raise _not_found()
        return team

    async def managed(self, user: UserT, team_id: int) -> TeamRow:
        """A team ``user`` manages — or ``team_not_found``, for a team they are merely in as
        much as for one they are not in at all."""
        team: TeamRow | None = await self._teams.managed(team_id, user.id)
        if team is None:
            raise _not_found()
        return team

    async def _grants(self, team_ids: Sequence[int]) -> Mapping[int, int]:
        if self._hooks.deleting is None:
            return {}
        return await self._hooks.deleting(team_ids)

    async def teams_of(self, user: UserT) -> list[TeamView]:
        """The teams ``user`` is in, by name, with everybody in them; the invitations waiting
        only where they manage. Nobody but an admin is told which other teams exist."""
        rows = [(team, TeamRole(role)) for team, role in await self._teams.of_user(user.id)]
        if not rows:
            return []
        members = _by_team(await self._teams.members([team.id for team, _role in rows]))
        managing = [team.id for team, role in rows if role is TeamRole.MANAGER]
        waiting = await self._teams.open_invitations(managing) if managing else {}
        return [TeamView(team, role, members.get(team.id, []), tuple(waiting.get(team.id, ()))) for team, role in rows]

    async def all_teams(self) -> list[TeamView]:
        """Every team but the system teams, by name, with everybody in it, the invitations
        waiting and the grants it holds — the admin's view."""
        teams = list(await self._teams.listing())
        ids = [team.id for team in teams]
        members = _by_team(await self._teams.members(ids))
        waiting = await self._teams.open_invitations(ids)
        grants = await self._grants(ids)
        return [
            TeamView(team, None, members.get(team.id, []), tuple(waiting.get(team.id, ())), int(grants.get(team.id, 0)))
            for team in teams
        ]

    async def view(self, team: TeamRow, my_role: TeamRole | None = None) -> TeamView:
        """One team for its admin (``my_role`` ``None``) or one of its managers — the two who
        may invite into it, and so see who is waiting."""
        members = await self._teams.members([team.id])
        waiting = await self._teams.open_invitations([team.id])
        return TeamView(team, my_role, list(members), tuple(waiting.get(team.id, ())))

    def check_version(self, team: TeamRow, expected: int | None) -> None:
        """Refuse a change made against a version the team has moved on from
        (``team_changed``, with its name). Checked when the client sends one, never demanded:
        a membership is a set and each change is idempotent."""
        if expected is not None and expected != team.version:
            raise TeamError(
                TeamErrorCode.TEAM_CHANGED,
                f"The team {team.name} was changed after you opened it. Reload to see the change.",
                extra={"name": team.name},
            )

    async def gate(self, user: UserT, what: TeamGate) -> None:
        """The app's billing gate on a manager's add or re-send (``TeamHooks.billing_gate``)."""
        if self._hooks.billing_gate is not None:
            await self._hooks.billing_gate(user, what)

    # --- the team itself (an admin's) ----------------------------------------------------

    async def _refuse_taken(self, name: str, team_id: int | None = None) -> None:
        """Asked before the write: an integrity error mid-request poisons the unit of work, and
        the sentence is the useful half of the refusal. The unique index stays the backstop."""
        if await self._teams.name_taken(name, other_than=team_id):
            raise TeamError(
                TeamErrorCode.TEAM_NAME_TAKEN, f"There is already a team called {name}.", extra={"name": name}
            )

    async def create(self, actor: UserT, name: str) -> TeamRow:
        """A new team by its stripped name, logged ``group_create``."""
        stripped = name.strip()
        await self._refuse_taken(stripped)
        team: TeamRow = await self._teams.add(NewTeam(stripped))
        await _record(self._log, AdminAction.GROUP_CREATE, actor=actor, team=team, now=self._now)
        return team

    async def rename(self, actor: UserT, team: TeamRow, name: str) -> None:
        """A new name, and a ``group_rename`` row that says neither name: the log joins what the
        team is called when it is read. Its own name again changes nothing."""
        stripped = name.strip()
        if stripped == team.name:
            return
        await self._refuse_taken(stripped, team.id)
        await self._teams.rename(team, stripped)
        await self._teams.bump(team)
        await _record(self._log, AdminAction.GROUP_RENAME, actor=actor, team=team, now=self._now)

    async def delete(self, actor: UserT, team: TeamRow, *, acknowledged: bool) -> None:
        """Delete a team, and with it every membership and every invitation into it — and, the
        app's own cascade, every grant made to it — once acknowledged (§11.2 decision 5): a
        request without the tick is ``confirmation_required`` and deletes nothing. The
        ``group_delete`` row counts what goes (an accepted invitation is history, not counted)
        and keeps the name the team had."""
        require_confirmation(confirmation_level(AdminAction.GROUP_DELETE), target_email=None, acknowledged=acknowledged)
        members = await self._teams.members([team.id])
        waiting = await self._teams.open_invitations([team.id])
        grants = await self._grants([team.id])
        gone = {
            "members": len(members),
            "grants": int(grants.get(team.id, 0)),
            "invitations": len(waiting.get(team.id, ())),
        }
        await _record(
            self._log, AdminAction.GROUP_DELETE, actor=actor, team=team, detail=gone, team_name=team.name, now=self._now
        )
        await self._teams.delete(team)

    # --- who is in it --------------------------------------------------------------------

    async def add(
        self,
        actor: UserT,
        team: TeamRow,
        email: str,
        role: TeamRole = TeamRole.MEMBER,
        *,
        by_admin: bool,
        locale: str | None = None,
    ) -> UserT | TeamInvitationSent:
        """Put the account with this exact address in the team as ``role`` — or, for an
        address with no account, invite it into the team (in ``locale``, else the actor's).
        Answers the account, or the invitation sent.

        A manager (``by_admin`` false) may only add members, and never changes a manager's
        role. Adding somebody already in the team changes their role; the role they have
        changes nothing."""
        role = TeamRole(role)
        if role is TeamRole.MANAGER and not by_admin:
            raise TeamError(TeamErrorCode.MANAGER_NAMED_BY_ADMIN)
        address = normalise_email(email)
        if self._hooks.refuse_address is not None:
            await self._hooks.refuse_address(address)
        user: UserT | None = await self._users.by_email(address)
        if user is None:
            return await self._invite(actor, team, address, role, locale)
        existing = await self._teams.membership(team.id, user.id)
        if existing is None:
            await self._teams.add_member(team.id, user.id, role)
            await self._teams.bump(team)
            await _record(
                self._log,
                AdminAction.MEMBERSHIP_ADD,
                actor=actor,
                team=team,
                target=user,
                detail={"role": role.value},
                now=self._now,
            )
            if self._hooks.member_added is not None:
                await self._hooks.member_added(actor, team, user)
            return user
        before = TeamRole(existing.role)
        if before is TeamRole.MANAGER and not by_admin:
            raise TeamError(
                TeamErrorCode.MANAGER_KEPT_BY_ADMIN,
                f"{address} manages this team; only an admin changes that.",
                extra={"email": address},
            )
        if before is not role:
            await self._change_role(actor, team, user, existing, before, role)
        return user

    async def _change_role(
        self, actor: UserT, team: TeamRow, user: AccountRow | None, member: Any, before: TeamRole, after: TeamRole
    ) -> None:
        await _record(
            self._log,
            AdminAction.MEMBERSHIP_ROLE,
            actor=actor,
            team=team,
            target=user,
            detail={"from": before.value, "to": after.value},
            now=self._now,
        )
        await self._teams.set_member_role(member, after)
        await self._teams.bump(team)

    async def _invite(
        self, actor: UserT, team: TeamRow, address: str, role: TeamRole, locale: str | None
    ) -> TeamInvitationSent:
        """An invitation into the team, through the account layer's: the address's open one
        into this team sent again (in the role asked for), else a new one. The version moves
        first, so it commits with the invitation before the mail goes."""
        await self._teams.bump(team)
        open_ones = [row for row in await self._invitations.open_for(address) if invitation_team(row) == team.id]
        if open_ones:
            latest = max(open_ones, key=lambda row: (row.created_at, row.id))
            if invitation_team_role(latest) is not role:
                await self._teams.set_invitation_role(latest, role)
            answer = await self._accounts.resend_invitation(actor, latest.id, show_link=self._show_link)
        else:
            fields: dict[str, Any] = {"email": address, "role": self._invitation_role}
            if locale is not None:
                fields["locale"] = locale
            answer = await self._accounts.create_invitation(
                actor,
                InvitationCreate(**fields),
                show_link=self._show_link,
                scope=team_scope(team.id, role),
                detail={"team_role": role.value},
            )
        return TeamInvitationSent(email=answer["email"], sent=bool(answer["sent"]), link=answer["link"])

    async def _member(self, team: TeamRow, user_id: int) -> Any:
        member = await self._teams.membership(team.id, user_id)
        if member is None:
            raise _not_found("Not a member of this team")
        return member

    async def set_role(self, actor: UserT, team: TeamRow, user_id: int, role: TeamRole) -> None:
        """An admin names or un-names a manager; the role the member has changes nothing."""
        member = await self._member(team, user_id)
        before, after = TeamRole(member.role), TeamRole(role)
        if before is not after:
            await self._change_role(actor, team, await self._users.get(user_id), member, before, after)

    async def remove(self, actor: UserT, team: TeamRow, user_id: int, *, by_admin: bool) -> None:
        """Take somebody out of the team — and with them, on their next request, everything
        shared with it. A manager removes members only. The ``membership_remove`` row says the
        role they had."""
        member = await self._member(team, user_id)
        role = TeamRole(member.role)
        if role is TeamRole.MANAGER and not by_admin:
            raise TeamError(TeamErrorCode.MANAGER_REMOVED_BY_ADMIN)
        await _record(
            self._log,
            AdminAction.MEMBERSHIP_REMOVE,
            actor=actor,
            team=team,
            target=await self._users.get(user_id),
            detail={"role": role.value},
            now=self._now,
        )
        await self._teams.remove_member(member)
        await self._teams.bump(team)

    # --- its invitations -----------------------------------------------------------------

    async def invitation(self, team: TeamRow, invitation_id: int) -> InvitationRow:
        """One of THIS team's invitations, in any state, or ``team_not_found``: a manager's
        reach ends at the team they manage."""
        invitation: InvitationRow | None = await self._invitations.get(invitation_id)
        if invitation is None or invitation_team(invitation) != team.id:
            raise _not_found("Invitation not found")
        return invitation

    async def resend(self, actor: UserT, invitation: InvitationRow) -> TeamInvitationSent:
        """A team invitation (:meth:`invitation`) sent again with a new link, through the
        account layer's re-send (``invitation_closed`` once taken up or withdrawn)."""
        answer = await self._accounts.resend_invitation(actor, invitation.id, show_link=self._show_link)
        return TeamInvitationSent(email=answer["email"], sent=bool(answer["sent"]), link=answer["link"])

    async def withdraw(self, actor: UserT, invitation: InvitationRow) -> None:
        """A team invitation (:meth:`invitation`) withdrawn, through the account layer's:
        stamped and kept (``revoked_at``); one taken up is ``invitation_closed``."""
        await self._accounts.revoke_invitation(actor, invitation.id)
