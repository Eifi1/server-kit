"""The teams' routes as router factories: ``teams_router(TeamsDeps(...))`` and
``teams_admin_router(...)`` — Kurvenschmiede's paths, bodies and answers.

Behind the ``fastapi`` extra, beside :func:`~eifi1_server_kit.auth.router.auth_router` and on its
``AuthDeps``: the same account, the same :func:`~eifi1_server_kit.auth.router.current_user`, the
same invitation store and mail, and, unless the app passes its own, the same
:func:`~eifi1_server_kit.auth.router.admin_guard` on the admin's router. An app mounts them as::

    teams = TeamsDeps(auth=deps, teams=team_store, hooks=team_hooks)
    app.include_router(teams_router(teams), prefix="/api/v1")
    app.include_router(teams_admin_router(teams), prefix="/api/v1")

**``/teams``** (:func:`teams_router`) is everybody's: ``GET ""`` the teams the caller is in (no
demo guard), and a manager's door into a team they manage — ``POST /{id}/members``, ``DELETE
/{id}/members/{user_id}``, ``POST /{id}/invitations/{inv}/resend``, ``DELETE
/{id}/invitations/{inv}``, each behind the app's ``demo_guard("teams")``. A team the caller does
not manage is ``404 team_not_found``, from the one query that answers a manager's team.

**``/admin/teams``** (:func:`teams_admin_router`) is the admin's: ``GET ""`` every team with its
grants and the confirmation levels; ``POST ""`` (``201``); ``PATCH /{id}``; ``DELETE /{id}``
(``204``, its optional body the acknowledgement); ``POST``, ``PATCH`` and ``DELETE`` on
``/{id}/members``.

**Every answer to a change is the whole team again**, as Kurvenschmiede's: who is in it is what
the page draws next. A version is read off ``If-Match`` (``3``, ``"3"`` or ``W/"3"``) where it
is checked — a manager's add and removal, the admin's rename — and never demanded. The router
raises no ``HTTPException``: every refusal is a coded kit refusal, answered by
:func:`~eifi1_server_kit.errors.install_contract_error_handlers`.

No ``from __future__ import annotations`` here, on purpose: the endpoints' parameter types are
closed over in the factories.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from eifi1_server_kit.auth.accounts import full_name
from eifi1_server_kit.auth.router import (
    AuthDeps,
    _guards,
    _offering,
    account_request_window,
    account_service,
    admin_guard,
    current_user,
)
from eifi1_server_kit.auth.service import AccountService
from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.teams.ports import invitation_team_role
from eifi1_server_kit.teams.schemas import (
    AdminTeamOut,
    AdminTeamsResponse,
    TeamIn,
    TeamInvitationOut,
    TeamInvited,
    TeamMemberIn,
    TeamMemberOut,
    TeamMemberRoleIn,
    TeamOut,
    TeamRole,
)
from eifi1_server_kit.teams.service import TeamHooks, TeamInvitationSent, TeamsService, TeamView
from eifi1_server_kit.user_admin.actions import CONFIRMATION_LEVELS
from eifi1_server_kit.user_admin.schemas import MAIL_BACKEND_CONSOLE, ActionConfirmation

try:
    from fastapi import APIRouter, Depends, Header, Response, status
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.teams.router needs FastAPI: install the kit with its fastapi extra, "
        "eifi1-server-kit[fastapi]."
    ) from exc

__all__ = ["TeamsDeps", "if_match_version", "no_team_hooks", "team_out", "teams_admin_router", "teams_router"]


async def no_team_hooks() -> TeamHooks[Any]:
    """``TeamsDeps.hooks``' default: no hook at all."""
    return TeamHooks()


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class TeamsDeps:
    """What an app hands the teams' routers: its ``AuthDeps`` (the account, the stores, the
    mail, the demo guard, the clock), its team store and its hooks."""

    #: The account layer's: ``current_user``, ``admin_guard``, the account service (the
    #: invitations and their mail), the users, invitations and log stores, ``demo_guard``,
    #: ``offered_locales``, ``mail_backend`` and ``now``.
    auth: AuthDeps
    #: A dependency answering the :class:`~eifi1_server_kit.teams.ports.TeamStore` on the
    #: request's session (``SqlTeamStore`` on ``[sql]``).
    teams: Callable[..., Any]
    #: A dependency answering the request's :class:`~eifi1_server_kit.teams.service.TeamHooks`.
    hooks: Callable[..., Any] = no_team_hooks
    #: The admin router's guard; ``None``: ``admin_guard(auth)``.
    guard: Callable[..., Any] | None = None


def if_match_version(if_match: str | None) -> int | None:
    """The version an ``If-Match`` header names — ``3``, ``"3"`` or ``W/"3"`` — or ``None`` when
    none was sent. One that names no number names no version the team has (``0``): the change
    is ``team_changed``, and the page reloads."""
    if if_match is None:
        return None
    text = if_match.strip()
    text = text.removeprefix("W/")
    try:
        return int(text.strip('"'))
    except ValueError:
        return 0


def _status(expires_at: datetime | None, now: datetime) -> str:
    return "open" if expires_at is not None and _aware(expires_at) > now else "expired"


def team_out(view: TeamView, viewer: Any, invited: TeamInvitationSent | None = None, *, now: datetime) -> TeamOut:
    """One team as ``viewer`` may see it: every member with their address, ``display_name`` in
    the viewer's language (the auth contract §3.2), the waiting invitations ``open`` or
    ``expired`` at ``now``, and the note of an add that invited."""
    return TeamOut(
        id=view.team.id,
        name=view.team.name,
        version=view.team.version,
        created_at=view.team.created_at,
        my_role=view.my_role,
        members=[
            TeamMemberOut(
                user_id=entry.user.id,
                first_name=entry.user.first_name,
                last_name=entry.user.last_name,
                display_name=full_name(entry.user.first_name, entry.user.last_name, viewer.locale),
                email=entry.user.email,
                role=TeamRole(entry.member.role),
                is_active=entry.user.is_active,
            )
            for entry in view.members
        ],
        invitations=[
            TeamInvitationOut(
                id=invitation.id,
                email=invitation.email,
                role=invitation_team_role(invitation),
                status=_status(invitation.expires_at, now),
                expires_at=invitation.expires_at,
            )
            for invitation in view.invitations
        ],
        invited=None if invited is None else TeamInvited(email=invited.email, sent=invited.sent, link=invited.link),
    )


def _service(deps: TeamsDeps) -> Callable[..., Any]:
    auth = deps.auth

    async def service(
        accounts: Annotated[AccountService[Any, Any, Any], Depends(account_service(auth))],
        teams: Annotated[Any, Depends(deps.teams)],
        users: Annotated[Any, Depends(auth.users)],
        invitations: Annotated[Any, Depends(auth.invitations)],
        log: Annotated[Any, Depends(auth.log)],
        hooks: Annotated[Any, Depends(deps.hooks)],
    ) -> TeamsService[Any]:
        return TeamsService(
            teams=teams,
            users=users,
            invitations=invitations,
            log=log,
            accounts=accounts,
            invitation_role=auth.policy.default_role,
            hooks=hooks,
            show_link=auth.mail_backend == MAIL_BACKEND_CONSOLE,
            now=auth.now,
        )

    return service


def _invited(added: object) -> TeamInvitationSent | None:
    return added if isinstance(added, TeamInvitationSent) else None


def teams_router(deps: TeamsDeps, *, prefix: str = "/teams", tags: Sequence[str] = ("teams",)) -> APIRouter:
    """A member's teams and a manager's door into theirs (see the module docstring), on
    ``APIRouter(prefix=prefix, tags=tags)``. A manager adds only members, removes only
    members, and is asked through the app's ``billing_gate`` before an add or a re-send."""
    auth = deps.auth
    Service = Annotated[TeamsService[Any], Depends(_service(deps))]  # noqa: N806
    User = Annotated[Any, Depends(current_user(auth))]  # noqa: N806
    IfMatch = Annotated[str | None, Header()]  # noqa: N806
    guards = _guards(auth, "teams")
    member_model = _offering(TeamMemberIn, auth.offered_locales)
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(account_request_window(auth))])

    async def answer(service: TeamsService[Any], team: Any, user: Any, invited: object = None) -> TeamOut:
        return team_out(await service.view(team, TeamRole.MANAGER), user, _invited(invited), now=auth.now())

    @router.get("", response_model=list[TeamOut])
    async def my_teams(user: User, service: Service) -> list[TeamOut]:
        """The teams the caller is in. Not a team list: nobody but an admin is told which teams
        exist beyond their own."""
        now = auth.now()
        return [team_out(view, user, now=now) for view in await service.teams_of(user)]

    @router.post("/{team_id}/members", response_model=TeamOut, dependencies=guards)
    async def add_member(
        team_id: int,
        payload: member_model,  # type: ignore[valid-type]
        user: User,
        service: Service,
        if_match: IfMatch = None,
    ) -> TeamOut:
        """A manager puts somebody in their team, by exact address, as a member — or invites
        them into it, if the address has no account."""
        body: TeamMemberIn = payload
        team = await service.managed(user, team_id)
        await service.gate(user, "add")
        service.check_version(team, if_match_version(if_match))
        added = await service.add(user, team, body.email, body.role, by_admin=False, locale=body.locale)
        return await answer(service, team, user, added)

    @router.post("/{team_id}/invitations/{invitation_id}/resend", response_model=TeamOut, dependencies=guards)
    async def resend_invitation(team_id: int, invitation_id: int, user: User, service: Service) -> TeamOut:
        """A manager sends one of the team's invitations again, with a new link."""
        team = await service.managed(user, team_id)
        await service.gate(user, "resend")
        sent = await service.resend(user, await service.invitation(team, invitation_id))
        return await answer(service, team, user, sent)

    @router.delete("/{team_id}/invitations/{invitation_id}", response_model=TeamOut, dependencies=guards)
    async def withdraw_invitation(team_id: int, invitation_id: int, user: User, service: Service) -> TeamOut:
        """A manager withdraws one of the team's invitations nobody has taken up."""
        team = await service.managed(user, team_id)
        await service.withdraw(user, await service.invitation(team, invitation_id))
        return await answer(service, team, user)

    @router.delete("/{team_id}/members/{user_id}", response_model=TeamOut, dependencies=guards)
    async def remove_member(
        team_id: int, user_id: int, user: User, service: Service, if_match: IfMatch = None
    ) -> TeamOut:
        """A manager takes a member out of their team — and with them, on their next request,
        everything shared with it."""
        team = await service.managed(user, team_id)
        service.check_version(team, if_match_version(if_match))
        await service.remove(user, team, user_id, by_admin=False)
        return await answer(service, team, user)

    return router


def teams_admin_router(deps: TeamsDeps, *, prefix: str = "/admin/teams", tags: Sequence[str] = ("admin",)) -> APIRouter:
    """The admin's teams (see the module docstring), every route behind the guard, on
    ``APIRouter(prefix=prefix, tags=tags)``. An admin adds anybody in either role, names and
    un-names managers, and is never asked through the billing gate."""
    auth = deps.auth
    Service = Annotated[TeamsService[Any], Depends(_service(deps))]  # noqa: N806
    Admin = Annotated[Any, Depends(current_user(auth))]  # noqa: N806
    IfMatch = Annotated[str | None, Header()]  # noqa: N806
    member_model = _offering(TeamMemberIn, auth.offered_locales)
    router = APIRouter(
        prefix=prefix,
        tags=list(tags),
        dependencies=[Depends(account_request_window(auth)), Depends(deps.guard or admin_guard(auth))],
    )
    levels = {action.value: level.value for action, level in CONFIRMATION_LEVELS.items()}

    async def answer(service: TeamsService[Any], team: Any, admin: Any, invited: object = None) -> TeamOut:
        return team_out(await service.view(team), admin, _invited(invited), now=auth.now())

    @router.get("", response_model=AdminTeamsResponse)
    async def list_teams(admin: Admin, service: Service) -> AdminTeamsResponse:
        """Every team and everybody in it, addresses included, with the grants each holds, and
        the confirmation levels: what the delete's question counts, and how it is asked."""
        now = auth.now()
        teams = [
            AdminTeamOut(**team_out(view, admin, now=now).model_dump(), grant_count=view.grants)
            for view in await service.all_teams()
        ]
        return AdminTeamsResponse(teams=teams, levels=levels)

    @router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
    async def create_team(payload: TeamIn, admin: Admin, service: Service) -> TeamOut:
        """A new team, by its name."""
        return await answer(service, await service.create(admin, payload.name), admin)

    @router.patch("/{team_id}", response_model=TeamOut)
    async def rename_team(
        team_id: int, payload: TeamIn, admin: Admin, service: Service, if_match: IfMatch = None
    ) -> TeamOut:
        """A new name, against the version read when one is sent."""
        team = await service.get(team_id)
        service.check_version(team, if_match_version(if_match))
        await service.rename(admin, team, payload.name)
        return await answer(service, team, admin)

    @router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_team(
        team_id: int, admin: Admin, service: Service, payload: ActionConfirmation | None = None
    ) -> Response:
        """Delete a team — its memberships, its waiting invitations and every grant made to it
        go with it — once acknowledged in the JSON body. The body is optional so that one a
        proxy dropped is ``confirmation_required`` rather than a ``422``: it fails closed."""
        team = await service.get(team_id)
        await service.delete(admin, team, acknowledged=payload is not None and payload.acknowledged)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/{team_id}/members", response_model=TeamOut)
    async def add_team_member(
        team_id: int,
        payload: member_model,  # type: ignore[valid-type]
        admin: Admin,
        service: Service,
    ) -> TeamOut:
        """Put anybody in any team, as a member or as its manager — or invite them into it, if
        the address has no account."""
        body: TeamMemberIn = payload
        team = await service.get(team_id)
        added = await service.add(admin, team, body.email, body.role, by_admin=True, locale=body.locale)
        return await answer(service, team, admin, added)

    @router.patch("/{team_id}/members/{user_id}", response_model=TeamOut)
    async def set_team_role(
        team_id: int, user_id: int, payload: TeamMemberRoleIn, admin: Admin, service: Service
    ) -> TeamOut:
        """Name or un-name a manager."""
        team = await service.get(team_id)
        await service.set_role(admin, team, user_id, payload.role)
        return await answer(service, team, admin)

    @router.delete("/{team_id}/members/{user_id}", response_model=TeamOut)
    async def remove_team_member(team_id: int, user_id: int, admin: Admin, service: Service) -> TeamOut:
        """Take anybody out of a team, a manager too."""
        team = await service.get(team_id)
        await service.remove(admin, team, user_id, by_admin=True)
        return await answer(service, team, admin)

    return router
