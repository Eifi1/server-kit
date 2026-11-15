"""A team's coded refusals: Kurvenschmiede's codes (``domain/refusals.py``), lifted with the teams.

The same shape as :class:`~eifi1_server_kit.user_admin.AccountError`: a ``code`` the page
words, never the English ``detail``, and
:func:`eifi1_server_kit.errors.install_contract_error_handlers` answers it as ``{"detail": …,
"code": …}`` plus the refusal's :attr:`TeamError.extra` — the team's ``name`` or the
manager's ``email`` the page's sentence fills in.

Every one the state refuses is a ``409``: the request is well formed, and it is the state of
the team that refuses it (Kurvenschmiede's reasoning for its ``RefusedError``). One is not:
``team_not_found`` (``404``) — no such team, none the caller manages, or none of its
invitations or members; one answer for all, so a manager of team A asking about team B learns
what a stranger does. Kurvenschmiede answered it uncoded (``{"detail"}``); the code beside it
is new and changes nothing a client of that answer reads.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

__all__ = ["TEAM_ERROR_DETAIL", "TEAM_ERROR_STATUS", "TeamError", "TeamErrorCode"]


class TeamErrorCode(enum.StrEnum):
    """The ``code`` of a team's refusal. A ``StrEnum``, so ``str(code)`` is the wire value. The
    field each one adds to the answer is named beside it."""

    #: No such team, a system team, one the caller does not manage, or no such member or
    #: invitation of it (404).
    TEAM_NOT_FOUND = "team_not_found"
    #: ``name``: another team is called that (the stripped name, asked before the write).
    TEAM_NAME_TAKEN = "team_name_taken"
    #: ``name``: the team moved on from the version the caller sent as ``If-Match``.
    TEAM_CHANGED = "team_changed"
    #: A manager naming a manager: who manages is an admin's decision.
    MANAGER_NAMED_BY_ADMIN = "manager_named_by_admin"
    #: ``email``: a manager changing another manager's role, by adding them again.
    MANAGER_KEPT_BY_ADMIN = "manager_kept_by_admin"
    #: A manager removing a manager.
    MANAGER_REMOVED_BY_ADMIN = "manager_removed_by_admin"


#: Each code's status: ``409``, but ``team_not_found``'s ``404``.
TEAM_ERROR_STATUS: Mapping[TeamErrorCode, int] = {
    **dict.fromkeys(TeamErrorCode, 409),
    TeamErrorCode.TEAM_NOT_FOUND: 404,
}

#: The English ``detail`` of each code when the raiser says nothing more; the service says the
#: team's name where Kurvenschmiede's sentence did. None of these echoes an address.
TEAM_ERROR_DETAIL: Mapping[TeamErrorCode, str] = {
    TeamErrorCode.TEAM_NOT_FOUND: "Team not found",
    TeamErrorCode.TEAM_NAME_TAKEN: "There is already a team with this name.",
    TeamErrorCode.TEAM_CHANGED: "The team was changed after you opened it. Reload to see the change.",
    TeamErrorCode.MANAGER_NAMED_BY_ADMIN: "Only an admin names a team's managers.",
    TeamErrorCode.MANAGER_KEPT_BY_ADMIN: "This person manages the team; only an admin changes that.",
    TeamErrorCode.MANAGER_REMOVED_BY_ADMIN: "Only an admin removes a team's manager.",
}

#: Keys the error body sets itself; :attr:`TeamError.extra` may not name them.
_RESERVED_KEYS = frozenset({"detail", "code"})


class TeamError(ValueError):
    """A team's coded refusal: ``status_code`` and ``code`` from :class:`TeamErrorCode`.

    ``TeamError(TeamErrorCode.TEAM_CHANGED, "The team Rig was changed…", extra={"name": "Rig"})``
    → ``409 {"detail": …, "code": "team_changed", "name": "Rig"}``. ``extra`` may not set
    ``detail`` or ``code``. A ``ValueError`` like every kit refusal, and registered in
    :data:`~eifi1_server_kit.errors.CONTRACT_ERRORS`, so an app-wide ``ValueError`` → 400
    handler does not swallow its status.
    """

    #: Replaced per instance from the code.
    status_code: int = 409
    code: TeamErrorCode
    extra: Mapping[str, object]

    def __init__(self, code: TeamErrorCode, detail: str | None = None, *, extra: Mapping[str, object] | None = None):
        self.code = TeamErrorCode(code)
        self.status_code = TEAM_ERROR_STATUS[self.code]
        fields = dict(extra or {})
        if clash := sorted(_RESERVED_KEYS & fields.keys()):
            raise ValueError(f"the error body sets {', '.join(clash)} itself")
        self.extra = fields
        super().__init__(detail or TEAM_ERROR_DETAIL[self.code])
