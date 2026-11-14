"""One app with teams: the admin world of ``tests/user_admin/_admin_world.py`` (the account
routes, the admin's), the kit's in-memory admin log and team store over its accounts and
invitations, the teams' ready account hooks wired into its ``AuthHooks``, and — once mounted —
the two teams routers at ``/api/v1``.

No ``from __future__ import annotations``: FastAPI reads the dependencies below.
"""

from typing import Any

from eifi1_server_kit.auth.service import AuthHooks
from eifi1_server_kit.auth.testing import InMemoryAdminActionLog
from eifi1_server_kit.teams.service import TeamAccountHooks, TeamHooks, TeamsService
from eifi1_server_kit.teams.testing import InMemoryTeamStore
from eifi1_server_kit.user_admin.service import UserAdminHooks
from tests.auth_fakes import MemoryUser
from tests.user_admin._admin_world import AdminWorld


class TeamsWorld(AdminWorld):
    """The admin world with teams: :attr:`teams` (the store), :attr:`team_hooks` (the app's
    :class:`TeamHooks`, replaced by a test), and the teams' account hooks in :attr:`hooks`."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        #: The kit's in-memory log in place of the account world's: it keeps a deleted group's name.
        self.admin_log = InMemoryAdminActionLog(now=self.clock)
        self.log = self.admin_log  # type: ignore[assignment]
        self.teams = InMemoryTeamStore(self.users.rows, self.invitations.rows, now=self.clock)
        self.team_hooks: TeamHooks[Any] = TeamHooks()
        self.account_hooks: TeamAccountHooks[Any] = TeamAccountHooks(self.teams, self.admin_log, now=self.clock)
        self.hooks = AuthHooks(
            on_register=self.account_hooks.on_register,
            on_invitation_redeemed=self.account_hooks.on_invitation_redeemed,
            invitation_group=self.account_hooks.invitation_group,
            invitation_scope=self.account_hooks.invitation_scope,
            describe_invitation=self.account_hooks.describe_invitation,
        )
        self.admin_hooks = UserAdminHooks(group_names=self.account_hooks.group_names)

    def teams_service(self, **options: Any) -> TeamsService[Any]:
        """The request's teams service over this world's stores, without a router."""
        values: dict[str, Any] = {
            "teams": self.teams,
            "users": self.users,
            "invitations": self.invitations,
            "log": self.admin_log,
            "accounts": self.service(self.hooks),
            "invitation_role": self.deps.policy.default_role,
            "hooks": self.team_hooks,
            "now": self.clock,
        }
        values.update(options)
        return TeamsService(**values)

    def person(self, email: str, **values: Any) -> MemoryUser:
        return self.account(email=email, **values)

    def actions(self) -> list[str]:
        return [row.action for row in self.admin_log.rows.values()]

    def last(self) -> Any:
        return list(self.admin_log.rows.values())[-1]
