"""One app on the account layer, for the router tests: the fakes, the router mounted at
``/api/v1`` as an app mounts it, the limiters on ``app.state``, and a client.

No ``from __future__ import annotations``: FastAPI reads the dependencies below.
"""

from typing import Any

import httpx
from fastapi import FastAPI, Request

from eifi1_server_kit.auth import AuthLimiters
from eifi1_server_kit.auth.passkeys import RelyingParty
from eifi1_server_kit.auth.router import AuthDeps, auth_router, invitations_admin_router
from eifi1_server_kit.auth.service import AccountLinks, AccountPolicy, AuthHooks
from eifi1_server_kit.errors import install_contract_error_handlers
from tests.auth_fakes import (
    Clock,
    FakeSigner,
    MemoryActionLog,
    MemoryInvitationStore,
    MemoryPasskeyStore,
    MemorySessionStore,
    MemoryTokenStore,
    MemoryUser,
    MemoryUserStore,
    PlainHasher,
    RecordingAfterCommit,
    RecordingMailer,
)

BASE = "/api/v1"
PASSWORD = "correct horse"
ORIGIN = "https://staunlabor.example"
POLICY = AccountPolicy(
    app="staunlabor",
    app_name="Staunlabor",
    contact="support@staunlabor.example",
    links=AccountLinks(base_url=ORIGIN),
)
RP = RelyingParty(rp_id="staunlabor.example", rp_name="Staunlabor", origins=(ORIGIN,))


class World:
    """One app: its stores, its objects, the app and a client."""

    def __init__(self, *, cutoff: bool = False, passkeys: bool = True, **options: Any) -> None:
        self.clock = Clock()
        self.users = MemoryUserStore(self.clock, cutoff=cutoff)
        self.sessions = MemorySessionStore()
        self.tokens = MemoryTokenStore(self.clock)
        self.invitations = MemoryInvitationStore(self.clock)
        self.passkey_store = MemoryPasskeyStore(self.clock)
        self.log = MemoryActionLog()
        self.after = RecordingAfterCommit()
        self.signer = FakeSigner(self.clock)
        self.hasher = PlainHasher()
        self.mailer = RecordingMailer(self.after)
        self.hooks: AuthHooks[Any, Any] = AuthHooks()
        self.waits: list[float] = []
        values: dict[str, Any] = {
            "users": lambda: self.users,
            "sessions": lambda: self.sessions,
            "tokens": lambda: self.tokens,
            "invitations": lambda: self.invitations,
            "log": lambda: self.log,
            "after_commit": lambda: self.after,
            "hooks": self._hooks,
            "signer": self.signer,
            "hasher": self.hasher,
            "mailer": self.mailer,
            "mail_backend": "resend",
            "policy": POLICY,
            "client_address": _test_ip,
            "now": self.clock,
            "sleep": self._sleep,
        }
        if passkeys:
            values.update(passkeys=lambda: self.passkey_store, relying_party=RP)
        values.update(options)
        self.deps = AuthDeps(**values)
        self.app = FastAPI()
        install_contract_error_handlers(self.app)
        self.app.include_router(auth_router(self.deps), prefix=BASE)
        self.app.include_router(invitations_admin_router(self.deps), prefix=BASE)
        self.app.state.auth_limiters = AuthLimiters()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def _hooks(self) -> AuthHooks[Any, Any]:
        return self.hooks

    async def _sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    async def call(self, method: str, path: str, *, token: str | None = None, **options: Any) -> httpx.Response:
        """One request; its queued work runs after it as the app's commit would, or is
        dropped with a refusal as a rollback would."""
        headers = dict(options.pop("headers", {}))
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        response = await self.client.request(method, BASE + path, headers=headers, **options)
        if response.status_code < 400:
            await self.after.finish()
        else:
            self.after.drop()
        return response

    def account(self, **values: Any) -> MemoryUser:
        values.setdefault("email", "ada@example.com")
        values.setdefault("password_hash", f"hashed:{PASSWORD}")
        return self.users.seed(**values)

    async def sign_in(self, user: MemoryUser, *, password: str = PASSWORD, **body: Any) -> dict[str, Any]:
        response = await self.call("POST", "/auth/login", json={"email": user.email, "password": password, **body})
        assert response.status_code == 200, response.text
        answer: dict[str, Any] = response.json()
        return answer

    async def token_of(self, user: MemoryUser) -> str:
        token: str = (await self.sign_in(user))["access_token"]
        return token


def _test_ip(request: Request) -> str:
    return request.headers.get("x-test-ip", "198.51.100.1")
