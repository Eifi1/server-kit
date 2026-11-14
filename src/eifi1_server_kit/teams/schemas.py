"""A team's wire shapes: Kurvenschmiede's (``domain/schemas/teams.py``), field for field, so the
app adopts them with no change on the wire.

* :class:`TeamOut` — one team as its viewer may see it: who is in it with their addresses, the
  viewer's own role, the invitations waiting (for an admin and the team's managers, who may
  invite), and, on the answer to an add that invited, :class:`TeamInvited`;
* :class:`AdminTeamOut` — the same on the admin's list, with the grants deleting it would take
  back (``grant_count``), and :class:`AdminTeamsResponse` — that list with the confirmation
  levels, ``{teams, levels}``;
* the bodies :class:`TeamIn` (``{name}``), :class:`TeamMemberIn` (``{email, locale?, role}``)
  and :class:`TeamMemberRoleIn` (``{role}``).

Two roles, :class:`TeamRole`: a ``member`` is in the team; a ``manager`` is in it and adds and
removes its members. Who manages is an admin's decision.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, ClassVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from eifi1_server_kit.auth.schemas import Email, LocaleTag, _offered_locale

__all__ = [
    "MAX_TEAM_NAME_LENGTH",
    "AdminTeamOut",
    "AdminTeamsResponse",
    "TeamIn",
    "TeamInvitationOut",
    "TeamInvited",
    "TeamMemberIn",
    "TeamMemberOut",
    "TeamMemberRoleIn",
    "TeamOut",
    "TeamRole",
]

#: The longest team name: Kurvenschmiede's ``teams.name`` is ``String(120)``.
MAX_TEAM_NAME_LENGTH = 120


class TeamRole(enum.StrEnum):
    """What a membership lets its holder do beyond being in the team. A ``StrEnum``: the stored
    and the wire value is ``str(role)``."""

    MEMBER = "member"
    MANAGER = "manager"


class TeamMemberOut(BaseModel):
    """One person in a team: the name's parts, for the page to write in its reader's order (the
    auth contract §3.2), and ``display_name`` already in the viewer's, for anything that only
    shows a name."""

    user_id: int
    first_name: str
    last_name: str
    display_name: str
    email: str
    role: TeamRole
    is_active: bool


class TeamInvitationOut(BaseModel):
    """Somebody invited into the team who has not come in yet — shown to the admin and the
    team's managers, who may invite (the auth contract §2.11)."""

    id: int
    email: str
    role: TeamRole
    #: ``open`` or ``expired`` — an accepted one is a member instead, a withdrawn one is gone.
    status: str
    expires_at: datetime | None


class TeamInvited(BaseModel):
    """The answer's note that the address had no account and was INVITED rather than added:
    whether the mail went, and — only while mail is the console backend — the link, shown this
    once."""

    email: str
    sent: bool
    link: str | None


class TeamOut(BaseModel):
    """One team as its viewer may see it."""

    id: int
    name: str
    version: int
    created_at: datetime
    #: The caller's own role in the team; ``None`` for an admin looking from the admin page.
    my_role: TeamRole | None
    members: list[TeamMemberOut]
    #: Waiting invitations — empty for anybody who may not invite into it.
    invitations: list[TeamInvitationOut] = []
    #: Only on the answer to an add that invited.
    invited: TeamInvited | None = None


class AdminTeamOut(TeamOut):
    """A team on the admin's list, with what deleting it would take back beyond the members and
    the waiting invitations it already lists: how many grants it holds (the app's
    ``TeamHooks.deleting``; ``0`` without one). The confirmation counts all three before the
    cascade does (the user-admin contract §11.2 decision 5)."""

    grant_count: int


class AdminTeamsResponse(BaseModel):
    """``GET /admin/teams``: every team, and the confirmation each action asks for —
    ``levels``, as the roster states them (§4.2), so the page asks before its first delete for
    exactly what the server refuses without: ``group_delete`` is ``acknowledge``."""

    teams: list[AdminTeamOut]
    levels: dict[str, str] = Field(default_factory=dict)


def _named(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise ValueError("a team's name is not blank")
    return stripped


class TeamIn(BaseModel):
    """``POST /admin/teams`` and ``PATCH /admin/teams/{id}``: ``{name}``, kept stripped. A name
    that is blank once stripped is a 422, as an empty one is."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=MAX_TEAM_NAME_LENGTH), AfterValidator(_named)]


class TeamMemberIn(BaseModel):
    """Put somebody in a team by their exact address — or invite them into it, if the address
    has no account. A manager may only add ``member``\\ s; naming a manager is an admin's call.
    The address is trimmed and lower-cased (:func:`~eifi1_server_kit.auth.normalise_email`)."""

    model_config = ConfigDict(extra="forbid")

    #: The app's languages (``AuthDeps.offered_locales``, which the teams' routers set on this
    #: body as the account routes set it on theirs); ``None`` checks the tag's shape only.
    offered_locales: ClassVar[Sequence[str] | None] = None

    email: Email
    #: The invitation mail's language, for an address with no account: the invitee's, picked
    #: in the dialog. Left out, the inviter's own.
    locale: LocaleTag | None = None
    role: TeamRole = TeamRole.MEMBER

    @field_validator("locale", mode="before")
    @classmethod
    def _offered_locale(cls, value: object) -> object:
        return _offered_locale(value, cls.offered_locales)


class TeamMemberRoleIn(BaseModel):
    """``PATCH /admin/teams/{id}/members/{user_id}``: ``{role}`` — an admin names or un-names a
    manager."""

    model_config = ConfigDict(extra="forbid")

    role: TeamRole
