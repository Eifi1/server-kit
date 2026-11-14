"""Teams (opt-in): named groups of accounts, their managers, and invitations into them —
Kurvenschmiede's teams, lifted into the kit so an app adopts them with no data migration and
no change on the wire.

The coded refusals (:class:`TeamError`, :class:`TeamErrorCode`), the wire shapes
(:class:`TeamOut`, :class:`AdminTeamOut`, :class:`AdminTeamsResponse`, the bodies), the
store's port (:class:`TeamStore`; a team invitation's ``scope``, :func:`team_scope`) and the
flows over it (:class:`TeamsService`, the app's :class:`TeamHooks`, the joins by invitation
:class:`TeamAccountHooks` and :func:`join_team`, a demo's :func:`add_system_member`) are here,
without FastAPI or SQLAlchemy. The routes are ``eifi1_server_kit.teams.router`` (the
``fastapi`` extra) and the tables ``eifi1_server_kit.teams.sql`` (the ``sql`` extra).
The in-memory store and its contract suite are ``eifi1_server_kit.teams.testing``, not
re-exported.

Nothing in the account layer or the admin's routes changes unless an app gives teams.
"""

from __future__ import annotations

from eifi1_server_kit.teams.errors import TEAM_ERROR_DETAIL, TEAM_ERROR_STATUS, TeamError, TeamErrorCode
from eifi1_server_kit.teams.ports import (
    Membership,
    NewTeam,
    TeamMemberRow,
    TeamRow,
    TeamStore,
    invitation_team,
    invitation_team_role,
    team_scope,
)
from eifi1_server_kit.teams.schemas import (
    MAX_TEAM_NAME_LENGTH,
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
from eifi1_server_kit.teams.service import (
    TeamAccountHooks,
    TeamGate,
    TeamHooks,
    TeamInvitationSent,
    TeamsService,
    TeamView,
    add_system_member,
    join_team,
)

__all__ = [
    "MAX_TEAM_NAME_LENGTH",
    "TEAM_ERROR_DETAIL",
    "TEAM_ERROR_STATUS",
    "AdminTeamOut",
    "AdminTeamsResponse",
    "Membership",
    "NewTeam",
    "TeamAccountHooks",
    "TeamError",
    "TeamErrorCode",
    "TeamGate",
    "TeamHooks",
    "TeamIn",
    "TeamInvitationOut",
    "TeamInvitationSent",
    "TeamInvited",
    "TeamMemberIn",
    "TeamMemberOut",
    "TeamMemberRoleIn",
    "TeamMemberRow",
    "TeamOut",
    "TeamRole",
    "TeamRow",
    "TeamStore",
    "TeamView",
    "TeamsService",
    "add_system_member",
    "invitation_team",
    "invitation_team_role",
    "join_team",
    "team_scope",
]
