"""0.32 (Kurvenschmiede's gap 1): an app's own routers are made when their module is imported,
before the app has its ``AuthDeps``; they depend on the kit's placeholders, and
``install_account_layer(app, deps)`` binds them when the app is built."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from eifi1_server_kit.auth.router import app_account_service, app_admin, app_current_user, install_account_layer
from tests.auth._world import BASE, World

# Made at import, as an app's own module does: no AuthDeps exists yet.
router = APIRouter()


@router.get("/mine")
async def mine(user: Annotated[Any, Depends(app_current_user)]) -> dict[str, int]:
    return {"id": user.id}


@router.get("/service")
async def service(accounts: Annotated[Any, Depends(app_account_service)]) -> dict[str, str]:
    return {"kind": type(accounts).__name__}


@router.get("/admin-only")
async def admin_only(user: Annotated[Any, Depends(app_admin)]) -> dict[str, int]:
    return {"id": user.id}


async def test_an_app_s_own_router_made_before_its_deps_reaches_the_account_layer() -> None:
    world = World()
    world.app.include_router(router, prefix=BASE)
    install_account_layer(world.app, world.deps)
    async with world.client:
        member = world.account()
        token = await world.token_of(member)
        assert (await world.call("GET", "/mine", token=token)).json() == {"id": member.id}
        assert (await world.call("GET", "/mine")).status_code == 401
        assert (await world.call("GET", "/service", token=token)).json() == {"kind": "AccountService"}
        refused = await world.call("GET", "/admin-only", token=token)
        assert refused.status_code == 403 and refused.json()["code"] == "admin_required"
        assert world.app.state.account_deps is world.deps


async def test_without_the_install_the_placeholder_names_what_is_missing() -> None:
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/mine", "/service", "/admin-only"):
            with pytest.raises(RuntimeError, match="install_account_layer"):
                await client.get(path)
