"""One app with the admin router (0.30) beside the account routes: the account world of
``tests/auth/_world.py``, the roster over its accounts, and ``user_admin_router`` mounted at
``/api/v1`` as an app mounts it.

No ``from __future__ import annotations``: FastAPI reads the dependencies below.
"""

from collections.abc import Callable
from typing import Any

from eifi1_server_kit.user_admin.router import UserAdminDeps, user_admin_router
from eifi1_server_kit.user_admin.service import AdminPolicy, UserAdminHooks
from eifi1_server_kit.user_admin.testing import InMemoryRosterStore
from tests.auth._world import BASE, World
from tests.auth_fakes import MemoryUser

#: The app's review vocabulary: the reviewer route mounts with it.
REVIEW = AdminPolicy(review_locales=("de-CH", "en", "fr", "it"), review_areas=("auth", "feedback"))


class AdminWorld(World):
    """The account world, its roster, and the admin router. ``admin`` holds the
    ``UserAdminDeps`` options, and ``admin_of`` answers more of them from the built world (a
    scope or a guard on its stores and its ``AuthDeps``); the rest goes to ``AuthDeps`` as in
    :class:`World`."""

    def __init__(
        self,
        *,
        admin: dict[str, Any] | None = None,
        admin_of: Callable[[AdminWorld], dict[str, Any]] | None = None,
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self.roster = InMemoryRosterStore(self.users.rows)  # type: ignore[arg-type]
        self.admin_hooks: UserAdminHooks[Any] = UserAdminHooks()
        self.feedback: Any = None
        values: dict[str, Any] = {
            "auth": self.deps,
            "roster": lambda: self.roster,
            "hooks": self._admin_hooks,
            "feedback": self._feedback,
            "policy": REVIEW,
        }
        values.update(admin or {})
        values.update(admin_of(self) if admin_of is not None else {})
        self.admin_deps = UserAdminDeps(**values)
        self.admin_router = user_admin_router(self.admin_deps)
        self.app.include_router(self.admin_router, prefix=BASE)

    async def _admin_hooks(self) -> UserAdminHooks[Any]:
        return self.admin_hooks

    async def _feedback(self) -> Any:
        return self.feedback

    async def admin_token(self, **values: Any) -> tuple[MemoryUser, str]:
        """An admin of the install, signed in."""
        values.setdefault("email", "root@example.com")
        admin = self.account(role="ADMIN", **values)
        return admin, await self.token_of(admin)
