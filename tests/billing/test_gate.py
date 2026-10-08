"""The read-only gate (billing contract §3.3, §12.2, §12.5, §12.13), through a real app."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from eifi1_server_kit.billing import (
    BillingError,
    BillingErrorCode,
    billing_write_allowed,
    refuse_billing_read_only,
)
from eifi1_server_kit.errors import install_contract_error_handlers

#: A keksdose-like allow-list (§12.13–§12.14).
BILLING_WRITES = frozenset(
    {
        "POST /api/v1/sync",
        "POST /api/v1/feedback",
        "DELETE /api/v1/budgets/{id}/shares/{share_id}",
        "PATCH /api/v1/auth/me",
    }
)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_reading_always_passes(method: str) -> None:
    assert billing_write_allowed(method, "/api/v1/budgets", standing=False)


def test_a_payer_in_good_standing_always_passes() -> None:
    assert billing_write_allowed("POST", "/api/v1/budgets", standing=True)
    assert billing_write_allowed("DELETE", "/api/v1/budgets/7", standing=True, allow=BILLING_WRITES)


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("POST", "/api/v1/sync", True),  # §12.5: receiving sync
        ("POST", "/api/v1/feedback", True),
        ("DELETE", "/api/v1/budgets/7/shares/3", True),  # removing access
        ("PATCH", "/api/v1/auth/me", True),
        ("POST", "/api/v1/budgets", False),
        ("POST", "/api/v1/invitations", False),  # adding guests stays blocked (§12.13)
        ("DELETE", "/api/v1/budgets/7/shares", False),
        ("PUT", "/api/v1/sync", False),
    ],
)
def test_a_lapsed_payer_passes_only_the_allow_list(method: str, path: str, allowed: bool) -> None:
    assert billing_write_allowed(method, path, standing=False, allow=BILLING_WRITES) is allowed
    assert not billing_write_allowed("POST", "/api/v1/sync", standing=False)  # the list is empty by default


def test_a_malformed_entry_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="METHOD /path"):
        billing_write_allowed("POST", "/api/v1/sync", standing=False, allow={"/api/v1/sync"})


def test_refuse_billing_read_only_at_an_apps_choke_point() -> None:
    refuse_billing_read_only(True, "a new budget")
    with pytest.raises(BillingError) as refused:
        refuse_billing_read_only(False, "a new budget")
    assert (refused.value.status_code, refused.value.code) == (402, BillingErrorCode.BILLING_READ_ONLY)
    assert str(refused.value) == "Read-only for now: changes need an active plan (a new budget)"


async def test_the_gate_answers_402_with_its_code() -> None:
    """The auth dependency's shape, answered by the contract handler."""
    lapsed = {"ada": False, "bob": True}

    async def write(request: Request) -> JSONResponse:
        standing = lapsed[request.headers["x-payer"]]
        if not billing_write_allowed(request.method, request.url.path, standing=standing, allow=BILLING_WRITES):
            raise BillingError(BillingErrorCode.BILLING_READ_ONLY)
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/api/v1/budgets", write, methods=["GET", "POST"]),
            Route("/api/v1/sync", write, methods=["POST"]),
        ]
    )
    install_contract_error_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post("/api/v1/budgets", headers={"x-payer": "ada"})
        assert (refused.status_code, refused.json()) == (
            402,
            {"detail": "Read-only for now: changes need an active plan", "code": "billing_read_only"},
        )
        assert (await client.get("/api/v1/budgets", headers={"x-payer": "ada"})).status_code == 200
        assert (await client.post("/api/v1/sync", headers={"x-payer": "ada"})).status_code == 200
        assert (await client.post("/api/v1/budgets", headers={"x-payer": "bob"})).status_code == 200
