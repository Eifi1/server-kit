"""``GlobalScope``: one role per account (auth §12.4)."""

import pytest

from eifi1_server_kit.auth.ports import NewAccount
from eifi1_server_kit.auth.testing import InMemoryUserStore
from eifi1_server_kit.user_admin.scope import GlobalScope
from eifi1_server_kit.user_admin.testing import InMemoryRosterStore


async def test_the_global_scope_gives_one_role_and_takes_the_review_scope_with_it() -> None:
    users = InMemoryUserStore()
    scope = GlobalScope(
        users=users,
        roster=InMemoryRosterStore(users.rows),
        admin_role="ADMIN",
        reviewer_role="REVIEWER",
        grantable=frozenset({"ADMIN", "MEMBER"}),
    )
    user = await users.add(
        NewAccount(
            email="a@example.org", password_hash="h", first_name="A", last_name="B", locale="en", role="REVIEWER"
        )
    )
    await users.apply(user, {"translation_review_locales": ["fr"]})
    with pytest.raises(ValueError, match="exactly one role"):
        await scope.set_roles(user, frozenset({"ADMIN", "MEMBER"}))
    await scope.set_roles(user, frozenset({"MEMBER"}))
    assert (user.role, user.translation_review_locales) == ("MEMBER", None)
    assert await scope.roles_of(user) == {"MEMBER"} and scope.record_fields() == {}
