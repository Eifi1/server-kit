"""Teams (opt-in): named groups of accounts, their managers, and invitations into them —
Kurvenschmiede's teams, lifted into the kit so an app adopts them with no data migration and
no change on the wire.

The coded refusals (:class:`TeamError`, :class:`TeamErrorCode`) and the wire shapes
(:class:`TeamOut`, :class:`AdminTeamOut`, :class:`AdminTeamsResponse`, the bodies) are here.
Nothing in the account layer or the admin's routes changes unless an app gives teams.
"""

from __future__ import annotations

from eifi1_server_kit.teams.errors import TEAM_ERROR_DETAIL, TEAM_ERROR_STATUS, TeamError, TeamErrorCode
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

__all__ = [
    "MAX_TEAM_NAME_LENGTH",
    "TEAM_ERROR_DETAIL",
    "TEAM_ERROR_STATUS",
    "AdminTeamOut",
    "AdminTeamsResponse",
    "TeamError",
    "TeamErrorCode",
    "TeamIn",
    "TeamInvitationOut",
    "TeamInvited",
    "TeamMemberIn",
    "TeamMemberOut",
    "TeamMemberRoleIn",
    "TeamOut",
    "TeamRole",
]
