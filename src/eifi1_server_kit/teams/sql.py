"""The teams' tables as SQLAlchemy declarative mixins, and the store over them — the ``sql`` extra.

Kurvenschmiede's tables column for column (its migrations 0032 and 0046), so the app swaps its
models for these with no migration: Alembic's comparison of its database with the mixins finds
nothing to change.

* :class:`TeamColumns` — ``teams``: ``id``; ``name`` ``String(120)``, unique and indexed (one
  unique index, ``ix_teams_name``); ``created_at``; ``version`` (``1``); ``is_system``
  (``False``, no server default).
* :class:`TeamMemberColumns` — ``team_members``: a surrogate ``id``; ``team_id`` and
  ``user_id``, each a ``CASCADE`` foreign key with its index (unnamed, so the app's naming
  convention names them); ``role``, a :class:`~eifi1_server_kit.teams.TeamRole` as
  ``VARCHAR(16)`` with no ``CHECK``; ``created_at``; unique per pair,
  ``uq_team_members_team_user``.
* :class:`TeamInvitationColumns` — beside :class:`~eifi1_server_kit.auth.sql.InvitationColumns`
  on the app's invitation model: ``team_id`` (``CASCADE`` foreign key, indexed, nullable) and
  ``team_role``, the team a ``scope`` names written into columns of its own, so a team's
  invitations go with it and are found by an index. :class:`SqlTeamInvitationStore` writes them
  from the scope (Kurvenschmiede's ``TeamInvitations``, lifted).

**The store**: :class:`SqlTeamStore` implements :class:`~eifi1_server_kit.teams.ports.TeamStore`
over the app's team, membership, account and invitation models, on the request's
``AsyncSession``. Every write flushes and none commits.

**The tables' names.** A foreign key to the accounts is built from ``__accounts_table__``
(default ``"users"``), one to the teams from ``__teams_table__`` (default ``"teams"``); an app
whose tables are called otherwise sets them on each subclass, or once on a mixin of its own
placed before these.

Needs the ``sql`` extra (``eifi1-server-kit[sql]``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar

try:
    from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint, delete, select
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Mapped, declared_attr, mapped_column
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.teams.sql needs SQLAlchemy: install the kit with its sql extra, eifi1-server-kit[sql]."
    ) from exc

from eifi1_server_kit.auth.ports import InvitationRow, NewInvitation
from eifi1_server_kit.auth.sql import InvitationColumns, SqlInvitationStore, UtcDateTime, _new, _PointsAtAccounts
from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.teams.ports import Membership, NewTeam, invitation_team, invitation_team_role
from eifi1_server_kit.teams.schemas import MAX_TEAM_NAME_LENGTH, TeamRole

__all__ = [
    "TEAMS_TABLE",
    "SqlTeamInvitationStore",
    "SqlTeamStore",
    "TeamColumns",
    "TeamInvitationColumns",
    "TeamMemberColumns",
    "team_role_type",
]

#: The team table every foreign key to a team points at unless a subclass says otherwise.
TEAMS_TABLE = "teams"
#: A role's stored width: Kurvenschmiede's ``VARCHAR(16)``.
_ROLE_WIDTH = 16


def _role_values(roles: type[TeamRole]) -> list[str]:
    return [role.value for role in roles]


def team_role_type() -> Enum:
    """A :class:`~eifi1_server_kit.teams.TeamRole` column's type: Kurvenschmiede's
    ``Enum(TeamRole, name="team_role", native_enum=False, length=16, validate_strings=True)``
    by value — ``VARCHAR(16)`` holding ``member`` or ``manager``, no ``CHECK``, read back as
    the enum."""
    return Enum(
        TeamRole,
        name="team_role",
        native_enum=False,
        length=_ROLE_WIDTH,
        validate_strings=True,
        values_callable=_role_values,
    )


class _PointsAtTeams:
    """The team table's name for the foreign keys below."""

    __teams_table__: ClassVar[str] = TEAMS_TABLE

    @classmethod
    def _team_id(cls) -> str:
        return f"{cls.__teams_table__}.id"


class TeamColumns:
    """A team: its unique name, when it was made, its version and whether it is the app's own
    (a system team, hidden everywhere)."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Plaintext, unique and indexed: two teams of one name are two places a grant could go.
    name: Mapped[str] = mapped_column(String(MAX_TEAM_NAME_LENGTH), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)
    #: Bumped by a rename and every change of who is in it; checked when a client sends it.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: The app's own team (Kurvenschmiede's "Demo"), not a group of people.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TeamMemberColumns(_PointsAtAccounts, _PointsAtTeams):
    """One person in one team, and whether they manage it. ``CASCADE`` both ways: a membership
    of a team that is gone, or of an account that is gone, is a membership of nothing; a
    deactivated account keeps its rows and comes back into its teams when it is reactivated."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    @declared_attr
    def team_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._team_id(), ondelete="CASCADE"), index=True, nullable=False)

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey(cls._account_id(), ondelete="CASCADE"), index=True, nullable=False)

    role: Mapped[TeamRole] = mapped_column(team_role_type(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now, nullable=False)

    @classmethod
    def table_args(cls) -> tuple[Any, ...]:
        """One membership per team and account: Kurvenschmiede's named constraint."""
        return (UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:
        return cls.table_args()


class TeamInvitationColumns(_PointsAtTeams):
    """The team an invitation brings its invitee into, beside
    :class:`~eifi1_server_kit.auth.sql.InvitationColumns`' ``scope``: ``None`` for an invitation
    to the instance alone. The ``CASCADE`` takes a team's invitations with it."""

    @declared_attr
    def team_id(cls) -> Mapped[int | None]:
        return mapped_column(ForeignKey(cls._team_id(), ondelete="CASCADE"), index=True, nullable=True)

    team_role: Mapped[TeamRole | None] = mapped_column(team_role_type(), nullable=True)


class SqlTeamInvitationStore[InvT: InvitationColumns](SqlInvitationStore[InvT]):
    """The account layer's :class:`~eifi1_server_kit.auth.sql.SqlInvitationStore` over a model
    that has :class:`TeamInvitationColumns` too: an invitation whose ``scope`` names a team of
    ``teams`` (the app's team model) gets that team and the role in it written into
    ``team_id`` and ``team_role`` as well. A scope naming no team that exists is kept as the
    kit keeps every scope, its columns left empty (the account layer's contract suite writes
    one). Kurvenschmiede's ``TeamInvitations``, lifted."""

    def __init__(self, session: AsyncSession, model: type[InvT], *, teams: type[Any]) -> None:
        super().__init__(session, model)
        self._teams = teams

    async def add(self, new: NewInvitation) -> InvT:
        invitation = await super().add(new)
        team_id = invitation_team(invitation)
        if team_id is not None and await self._session.get(self._teams, team_id) is not None:
            row: Any = invitation
            row.team_id = team_id
            row.team_role = invitation_team_role(invitation)
            await self._session.flush()
        return invitation


class SqlTeamStore[TeamT: TeamColumns, MemberT: TeamMemberColumns]:
    """:class:`~eifi1_server_kit.teams.ports.TeamStore` over the app's team model
    (:class:`TeamColumns`) and membership model (:class:`TeamMemberColumns`), reading its
    account model (``users``, :class:`~eifi1_server_kit.auth.sql.AccountColumns`) for the member
    lists and its invitation model (``invitations``, with :class:`TeamInvitationColumns`) for a
    team's open invitations. Deleting a team deletes its memberships and invitations first, so
    the result is the cascade's whether or not the database enforces it."""

    def __init__(
        self,
        session: AsyncSession,
        model: type[TeamT],
        members: type[MemberT],
        *,
        users: type[Any],
        invitations: type[Any],
    ) -> None:
        self._session = session
        self._model = model
        self._members = members
        self._users = users
        self._invitations = invitations

    async def get(self, team_id: int) -> TeamT | None:
        return await self._session.get(self._model, team_id)

    async def names(self, team_ids: Sequence[int]) -> Mapping[int, str]:
        if not team_ids:
            return {}
        model = self._model
        rows = await self._session.execute(select(model.id, model.name).where(model.id.in_(list(team_ids))))
        return dict(rows.all())

    async def name_taken(self, name: str, *, other_than: int | None = None) -> bool:
        model = self._model
        query = select(model.id).where(model.name == name)
        if other_than is not None:
            query = query.where(model.id != other_than)
        return await self._session.scalar(query.limit(1)) is not None

    async def listing(self) -> Sequence[TeamT]:
        model = self._model
        rows = await self._session.scalars(select(model).where(model.is_system.is_(False)).order_by(model.name))
        return list(rows)

    async def of_user(self, user_id: int) -> Sequence[tuple[TeamT, str]]:
        model, members = self._model, self._members
        rows = await self._session.execute(
            select(model, members.role)
            .join(members, members.team_id == model.id)
            .where(members.user_id == user_id, model.is_system.is_(False))
            .order_by(model.name)
        )
        return [(team, role) for team, role in rows.all()]

    async def managed(self, team_id: int, user_id: int) -> TeamT | None:
        model, members = self._model, self._members
        rows = await self._session.scalars(
            select(model)
            .join(members, members.team_id == model.id)
            .where(
                model.id == team_id,
                model.is_system.is_(False),
                members.user_id == user_id,
                members.role == TeamRole.MANAGER,
            )
        )
        return rows.one_or_none()

    async def add(self, new: NewTeam) -> TeamT:
        team = _new(self._model, {"name": new.name, "is_system": new.is_system, "version": 1})
        self._session.add(team)
        await self._session.flush()
        return team

    async def rename(self, team: TeamT, name: str) -> None:
        team.name = name
        await self._session.flush()

    async def bump(self, team: TeamT) -> None:
        team.version = team.version + 1
        await self._session.flush()

    async def delete(self, team: TeamT) -> None:
        members, invitations = self._members, self._invitations
        await self._session.execute(delete(members).where(members.team_id == team.id))
        await self._session.execute(delete(invitations).where(invitations.team_id == team.id))
        await self._session.delete(team)
        await self._session.flush()

    async def members(self, team_ids: Sequence[int]) -> Sequence[Membership[Any, Any]]:
        if not team_ids:
            return []
        members, users = self._members, self._users
        rows = await self._session.execute(
            select(members, users)
            .join(users, users.id == members.user_id)
            .where(members.team_id.in_(list(team_ids)))
            .order_by(members.team_id, users.last_name, users.first_name, users.id)
        )
        return [Membership(member, user) for member, user in rows.all()]

    async def membership(self, team_id: int, user_id: int) -> MemberT | None:
        members = self._members
        rows = await self._session.scalars(
            select(members).where(members.team_id == team_id, members.user_id == user_id)
        )
        return rows.one_or_none()

    async def add_member(self, team_id: int, user_id: int, role: TeamRole) -> MemberT:
        member = _new(self._members, {"team_id": team_id, "user_id": user_id, "role": TeamRole(role)})
        self._session.add(member)
        await self._session.flush()
        return member

    async def set_member_role(self, member: MemberT, role: TeamRole) -> None:
        member.role = TeamRole(role)
        await self._session.flush()

    async def remove_member(self, member: MemberT) -> None:
        await self._session.delete(member)
        await self._session.flush()

    async def open_invitations(self, team_ids: Sequence[int]) -> Mapping[int, Sequence[InvitationRow]]:
        if not team_ids:
            return {}
        model = self._invitations
        rows = await self._session.scalars(
            select(model)
            .where(model.team_id.in_(list(team_ids)), model.accepted_at.is_(None), model.revoked_at.is_(None))
            .order_by(model.created_at, model.id)
        )
        found: dict[int, list[InvitationRow]] = {}
        for row in rows:
            found.setdefault(row.team_id, []).append(row)
        return found

    async def set_invitation_role(self, invitation: InvitationRow, role: TeamRole) -> None:
        row: Any = invitation
        row.team_role = TeamRole(role)
        row.scope = {**(row.scope or {}), "team_role": TeamRole(role).value}
        await self._session.flush()
