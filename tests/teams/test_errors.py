"""A team's coded refusals: Kurvenschmiede's five codes and the 404, each with its status, its
English detail and the fields the page's sentence names (Kurvenschmiede's ``RefusalCode``)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from eifi1_server_kit.errors import CONTRACT_ERRORS, install_contract_error_handlers
from eifi1_server_kit.teams import TEAM_ERROR_DETAIL, TEAM_ERROR_STATUS, TeamError, TeamErrorCode


def test_the_codes_are_kurvenschmiedes_and_every_state_refusal_is_a_409() -> None:
    assert [str(code) for code in TeamErrorCode] == [
        "team_not_found",
        "team_name_taken",
        "team_changed",
        "manager_named_by_admin",
        "manager_kept_by_admin",
        "manager_removed_by_admin",
    ]
    assert set(TEAM_ERROR_STATUS) == set(TeamErrorCode) == set(TEAM_ERROR_DETAIL)
    assert TEAM_ERROR_STATUS[TeamErrorCode.TEAM_NOT_FOUND] == 404
    assert {status for code, status in TEAM_ERROR_STATUS.items() if code is not TeamErrorCode.TEAM_NOT_FOUND} == {409}
    assert not any("@" in detail for detail in TEAM_ERROR_DETAIL.values()), "no default detail echoes an address"


def test_a_refusal_carries_its_code_status_detail_and_extra_fields() -> None:
    error = TeamError(TeamErrorCode.TEAM_CHANGED, "The team Rig was changed.", extra={"name": "Rig"})
    assert (error.code, error.status_code, str(error), dict(error.extra)) == (
        TeamErrorCode.TEAM_CHANGED,
        409,
        "The team Rig was changed.",
        {"name": "Rig"},
    )
    missing = TeamError(TeamErrorCode.TEAM_NOT_FOUND)
    assert (missing.status_code, str(missing), dict(missing.extra)) == (404, "Team not found", {})
    assert isinstance(missing, ValueError)


@pytest.mark.parametrize("key", ["detail", "code"])
def test_extra_never_names_what_the_answer_sets_itself(key: str) -> None:
    with pytest.raises(ValueError, match=f"sets {key} itself"):
        TeamError(TeamErrorCode.TEAM_NAME_TAKEN, extra={key: "x"})


def test_the_refusal_is_registered_beside_the_kits_others() -> None:
    assert TeamError in CONTRACT_ERRORS


async def test_an_uncaught_refusal_is_answered_with_its_code_and_fields_beside_a_value_error_handler() -> None:
    app = FastAPI()

    async def bad_request(_request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - the kit's wins
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.add_exception_handler(ValueError, bad_request)
    install_contract_error_handlers(app)

    @app.get("/taken")
    async def taken() -> None:
        raise TeamError(TeamErrorCode.TEAM_NAME_TAKEN, "There is already a team called Rig.", extra={"name": "Rig"})

    @app.get("/gone")
    async def gone() -> None:
        raise TeamError(TeamErrorCode.TEAM_NOT_FOUND)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        taken_answer = await client.get("/taken")
        gone_answer = await client.get("/gone")
    assert (taken_answer.status_code, taken_answer.json()) == (
        409,
        {"detail": "There is already a team called Rig.", "code": "team_name_taken", "name": "Rig"},
    )
    assert (gone_answer.status_code, gone_answer.json()) == (
        404,
        {"detail": "Team not found", "code": "team_not_found"},
    )
