"""The seam holds a company scope (auth §12.4): kastlan's company admin, sketched as a fake and
run through the same ``user_admin_router`` — the acting company's members only, roles as a set
(``PUT /users/{id}/roles``), ``other_companies`` for an account-wide action, ``last_admin``
naming the company, the company on every log row, and seats through the hooks. kastlan
writes its own when it moves; the kit ships the global scope only.

No ``from __future__ import annotations``: FastAPI reads the dependencies below.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any

import pytest
from fastapi import Depends
from pydantic import Field

from eifi1_server_kit.auth import AuthError, AuthErrorCode
from eifi1_server_kit.auth.router import current_user
from eifi1_server_kit.auth.testing import InMemoryAdminActionLog
from eifi1_server_kit.billing import PlanLimitError
from eifi1_server_kit.user_admin import (
    AccountError,
    AccountErrorCode,
    AdminAction,
    AdminUserRow,
    UserListQuery,
)
from eifi1_server_kit.user_admin.ports import RosterCounts
from eifi1_server_kit.user_admin.service import AdminPolicy, UserAdminHooks
from eifi1_server_kit.user_admin.testing import InMemoryRosterStore
from tests.auth_fakes import MemoryUser
from tests.user_admin._admin_world import AdminWorld

ACME, OTHER = 1, 2
STAFF = frozenset({"ADMIN", "manager", "accountant"})
NAMES = {ACME: "Acme AG", OTHER: "Other GmbH"}


class Memberships:
    """kastlan's ``user_role_assignments``: an account's roles per company."""

    def __init__(self) -> None:
        self.roles: dict[int, dict[int, set[str]]] = {}
        self.seats = 10

    def of(self, user_id: int, company: int) -> set[str]:
        return self.roles.setdefault(user_id, {}).setdefault(company, set())

    def companies(self, user_id: int) -> set[int]:
        return {company for company, held in self.roles.get(user_id, {}).items() if held}


class CompanyScope:
    """An :class:`~eifi1_server_kit.user_admin.scope.AdminScope` over one company."""

    def __init__(self, world: AdminWorld, memberships: Memberships, company: int) -> None:
        self.world, self.memberships, self.company = world, memberships, company

    def _members(self) -> Mapping[int, Any]:
        return {
            user_id: row
            for user_id, row in self.world.users.rows.items()
            if self.company in self.memberships.companies(user_id)
        }

    async def find(self, user_id: int) -> MemoryUser | None:
        return self._members().get(user_id)

    async def roster(self, query: UserListQuery) -> tuple[Sequence[Any], int]:
        return await InMemoryRosterStore(self._members()).page(query, admin_role="ADMIN", reviewer_role=None)

    async def counts(self, *, since: datetime) -> RosterCounts:
        return await InMemoryRosterStore(self._members()).counts(since=since)

    async def roles_of(self, user: MemoryUser) -> frozenset[str]:
        return frozenset(self.memberships.of(user.id, self.company))

    async def grantable_roles(self) -> frozenset[str]:
        return STAFF  # never "tenant": it has no portal yet

    async def set_roles(self, user: MemoryUser, roles: frozenset[str]) -> None:
        held = self.memberships.of(user.id, self.company)
        held.clear()
        held.update(roles)

    async def active_admins(self) -> int:
        return sum(
            row.is_active and "ADMIN" in self.memberships.of(user_id, self.company)
            for user_id, row in self._members().items()
        )

    async def refuse_last_admin(self, user: MemoryUser) -> None:
        held = self.memberships.of(user.id, self.company)
        if user.is_active and "ADMIN" in held and await self.active_admins() <= 1:
            raise AccountError(AccountErrorCode.LAST_ADMIN, extra={"companies": [NAMES[self.company]]})

    async def refuse_account_wide(self, user: MemoryUser, action: AdminAction) -> None:
        if self.memberships.companies(user.id) - {self.company}:
            raise AccountError(AccountErrorCode.OTHER_COMPANIES)

    def record_fields(self) -> Mapping[str, Any]:
        return {"company_id": self.company}


class Row(AdminUserRow):
    roles: list[str] = Field(default_factory=list)


@pytest.fixture
async def kastlan() -> AsyncIterator[tuple[AdminWorld, Memberships]]:
    memberships = Memberships()

    def options(world: AdminWorld) -> dict[str, Any]:
        async def scope() -> CompanyScope:
            return CompanyScope(world, memberships, ACME)

        async def company_staff(user: Annotated[Any, Depends(current_user(world.deps))]) -> None:
            """The fake's guard: the company's admins and managers."""
            if not memberships.of(user.id, ACME) & {"ADMIN", "manager"}:
                raise AuthError(AuthErrorCode.ADMIN_REQUIRED)

        return {
            "scope": scope,
            "guard": company_staff,
            "role_sets": True,
            "row_model": Row,
            "policy": AdminPolicy(
                roles=("ADMIN", "MEMBER", *sorted(STAFF - {"ADMIN"}), "tenant", "REVIEWER"),
                review_locales=("de-CH", "en", "fr"),
            ),
        }

    world = AdminWorld(admin_of=options)
    world.log = InMemoryAdminActionLog(now=world.clock)  # type: ignore[assignment]

    async def to_row(user: MemoryUser) -> Row:
        row = Row.model_validate(user, from_attributes=True)
        return row.model_copy(update={"roles": sorted(memberships.of(user.id, ACME))})

    async def role_changing(admin: Any, target: Any, before: frozenset[str], after: frozenset[str]) -> None:
        if not before & STAFF and after & STAFF:
            _seat(memberships)

    async def activating(admin: Any, target: Any) -> None:
        if memberships.of(target.id, ACME) & STAFF:
            _seat(memberships)

    world.admin_hooks = UserAdminHooks(to_row=to_row, role_changing=role_changing, activating=activating)
    async with world.client:
        yield world, memberships


def _seat(memberships: Memberships) -> None:
    used = sum(bool(held.get(ACME, set()) & STAFF) for held in memberships.roles.values())
    if used >= memberships.seats:
        raise PlanLimitError(dimension="seats", plan="team", limit=memberships.seats, used=used)


async def _company_admin(world: AdminWorld, memberships: Memberships) -> tuple[MemoryUser, str]:
    admin = world.account(email="boss@acme.example", role="ADMIN")
    memberships.of(admin.id, ACME).add("ADMIN")
    return admin, await world.token_of(admin)


def code(response: Any) -> str:
    found: str = response.json()["code"]
    return found


async def test_the_roster_is_the_company_s_and_its_rows_carry_the_roles(
    kastlan: tuple[AdminWorld, Memberships],
) -> None:
    world, memberships = kastlan
    _, token = await _company_admin(world, memberships)
    member = world.account(email="mia@acme.example")
    memberships.of(member.id, ACME).update({"manager", "accountant"})
    stranger = world.account(email="sam@other.example")
    memberships.of(stranger.id, OTHER).add("ADMIN")
    answer = (await world.call("GET", "/admin/users?sort=email", token=token)).json()
    assert [row["email"] for row in answer["users"]] == ["boss@acme.example", "mia@acme.example"]
    assert answer["users"][1]["roles"] == ["accountant", "manager"]
    assert answer["summary"] == {"active_admins": 1}
    hidden = await world.call("POST", f"/admin/users/{stranger.id}/active", token=token, json={"active": True})
    assert (hidden.status_code, code(hidden)) == (404, "user_not_found"), "another company's account is not found"


async def test_roles_are_a_set_given_with_put_and_logged_as_added_and_removed(
    kastlan: tuple[AdminWorld, Memberships],
) -> None:
    world, memberships = kastlan
    admin, token = await _company_admin(world, memberships)
    member = world.account(email="mia@acme.example")
    memberships.of(member.id, ACME).add("manager")
    path = f"/admin/users/{member.id}/roles"
    assert (await world.call("POST", f"/admin/users/{member.id}/role", token=token, json={})).status_code == 404
    refused = await world.call("PUT", path, token=token, json={"roles": ["tenant"], "acknowledged": True})
    assert (refused.status_code, code(refused)) == (422, "role_not_grantable")
    answer = await world.call("PUT", path, token=token, json={"roles": ["accountant", "manager"], "acknowledged": True})
    assert answer.status_code == 200 and answer.json()["roles"] == ["accountant", "manager"]
    (row,) = world.log.rows.values()  # type: ignore[attr-defined]
    assert (row.action, row.detail, row.company_id) == ("role", {"added": ["accountant"], "removed": []}, ACME)
    assert row.actor_id == admin.id


async def test_the_last_admin_of_the_company_is_named(kastlan: tuple[AdminWorld, Memberships]) -> None:
    world, memberships = kastlan
    admin, token = await _company_admin(world, memberships)
    own = await world.call(
        "PUT", f"/admin/users/{admin.id}/roles", token=token, json={"roles": ["manager"], "acknowledged": True}
    )
    assert code(own) == "self_action"
    manager = world.account(email="mia@acme.example")
    memberships.of(manager.id, ACME).add("manager")
    response = await world.call(
        "PUT",
        f"/admin/users/{admin.id}/roles",
        token=await world.token_of(manager),
        json={"roles": ["manager"], "acknowledged": True},
    )
    assert (response.status_code, code(response)) == (409, "last_admin")
    assert response.json()["companies"] == ["Acme AG"]
    assert memberships.of(admin.id, ACME) == {"ADMIN"}


async def test_an_account_in_other_companies_is_not_switched_off_by_one(
    kastlan: tuple[AdminWorld, Memberships],
) -> None:
    world, memberships = kastlan
    _, token = await _company_admin(world, memberships)
    shared = world.account(email="pat@acme.example")
    memberships.of(shared.id, ACME).add("manager")
    memberships.of(shared.id, OTHER).add("accountant")
    for method, path, body in (
        ("POST", "active", {"active": False, "confirm_email": shared.email}),
        ("POST", "password-change", {"acknowledged": True}),
    ):
        refused = await world.call(method, f"/admin/users/{shared.id}/{path}", token=token, json=body)
        assert (refused.status_code, code(refused)) == (409, "other_companies"), path
    assert shared.is_active and shared.password_change_required_at is None


async def test_a_seat_is_taken_by_a_staff_role_and_by_switching_staff_on_again(
    kastlan: tuple[AdminWorld, Memberships],
) -> None:
    world, memberships = kastlan
    _, token = await _company_admin(world, memberships)
    memberships.seats = 1
    tenant = world.account(email="ten@acme.example")
    memberships.of(tenant.id, ACME).add("tenant")
    full = await world.call(
        "PUT", f"/admin/users/{tenant.id}/roles", token=token, json={"roles": ["manager"], "acknowledged": True}
    )
    assert (full.status_code, code(full)) == (402, "plan_limit")
    asleep = world.account(email="zz@acme.example", is_active=False)
    memberships.of(asleep.id, ACME).add("accountant")
    memberships.seats = 2
    refused = await world.call("POST", f"/admin/users/{asleep.id}/active", token=token, json={"active": True})
    assert code(refused) == "plan_limit" and not asleep.is_active


async def test_the_company_s_log_rows_carry_the_company(kastlan: tuple[AdminWorld, Memberships]) -> None:
    world, memberships = kastlan
    _admin, token = await _company_admin(world, memberships)
    member = world.account(email="mia@acme.example")
    memberships.of(member.id, ACME).add("manager")
    response = await world.call(
        "POST", f"/admin/users/{member.id}/active", token=token, json={"active": False, "confirm_email": member.email}
    )
    assert response.status_code == 200
    page = (await world.call("GET", "/admin/actions", token=token)).json()
    assert [(row["action"], row["company_id"]) for row in page["actions"]] == [("deactivate", ACME)]
    assert page["actions"][0]["target"]["email"] == member.email
    assert (await world.call("GET", "/admin/attention", token=token)).json()["users_total"] == 2


async def test_only_the_company_s_staff_pass_its_guard(kastlan: tuple[AdminWorld, Memberships]) -> None:
    world, _ = kastlan
    stranger = world.account(email="x@other.example", role="ADMIN")
    refused = await world.call("GET", "/admin/users", token=await world.token_of(stranger))
    assert (refused.status_code, code(refused)) == (403, "admin_required")


async def test_with_role_sets_the_reviewer_role_joins_and_leaves_the_others(
    kastlan: tuple[AdminWorld, Memberships],
) -> None:
    world, memberships = kastlan
    _, token = await _company_admin(world, memberships)
    manager = world.account(email="mia@acme.example")
    memberships.of(manager.id, ACME).add("manager")
    path = f"/admin/users/{manager.id}/reviewer"
    given = await world.call("PUT", path, token=token, json={"locales": ["fr"]})
    assert given.status_code == 200, given.text
    assert memberships.of(manager.id, ACME) == {"manager", "REVIEWER"}
    await world.call("PUT", path, token=token, json={"locales": []})
    assert memberships.of(manager.id, ACME) == {"manager"}
    only = world.account(email="rev@acme.example")
    memberships.of(only.id, ACME).add("REVIEWER")
    await world.call("PUT", f"/admin/users/{only.id}/reviewer", token=token, json={"locales": []})
    assert memberships.of(only.id, ACME) == {"MEMBER"}, "never no role at all: the default one"
