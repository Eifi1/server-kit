"""The scheduler's token routes (billing §14.7): ``X-Jobs-Token``, compared in constant
time, 401 for a missing or wrong token and for none set — through a real FastAPI app, as
kastlan and Kurvenschmiede mount it."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.jobs import (
    JOBS_TOKEN_HEADER,
    JobsSettings,
    JobsTokenError,
    check_jobs_token,
    require_jobs_token,
)

TOKEN = "jobs-token-example-0123456789"


class _Settings(BaseSettings, JobsSettings):
    """An app's settings as the README shows them."""

    model_config = SettingsConfigDict(env_prefix="GARDEN_")


def _app(settings: JobsSettings) -> FastAPI:
    app = FastAPI()
    install_contract_error_handlers(app)
    router = APIRouter(prefix="/ops", dependencies=[Depends(require_jobs_token(lambda: settings.jobs_token))])

    @router.post("/jobs/{name}")
    async def run_job(name: str) -> dict[str, object]:
        return {"job": name, "done": 0}

    app.include_router(router)
    return app


async def _post(settings: JobsSettings, headers: dict[str, str] | None = None) -> tuple[int, object]:
    async with AsyncClient(transport=ASGITransport(app=_app(settings)), base_url="http://test") as client:
        response = await client.post("/ops/jobs/billing-notices", headers=headers)
    return response.status_code, response.json()


async def test_the_scheduler_with_the_token_is_let_through() -> None:
    settings = JobsSettings(jobs_token=TOKEN)
    assert await _post(settings, {JOBS_TOKEN_HEADER: TOKEN}) == (200, {"job": "billing-notices", "done": 0})
    assert await _post(settings, {"x-jobs-token": TOKEN}) == (200, {"job": "billing-notices", "done": 0})


@pytest.mark.parametrize(
    "headers",
    [None, {JOBS_TOKEN_HEADER: ""}, {JOBS_TOKEN_HEADER: TOKEN[:-1]}, {JOBS_TOKEN_HEADER: f"{TOKEN} "}],
)
async def test_a_missing_or_wrong_token_is_401(headers: dict[str, str] | None) -> None:
    assert await _post(JobsSettings(jobs_token=TOKEN), headers) == (401, {"detail": "A valid X-Jobs-Token is required"})


async def test_no_token_set_is_401_for_everyone_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Kurvenschmiede's rule: one answer, and the log says it is the operator's to fix."""
    for unset in (JobsSettings(), JobsSettings(jobs_token=""), JobsSettings(jobs_token="  ")):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="eifi1_server_kit.jobs"):
            assert (await _post(unset, {JOBS_TOKEN_HEADER: ""}))[0] == 401
            assert (await _post(unset, {JOBS_TOKEN_HEADER: "  "}))[0] == 401
        assert "no jobs token is set" in caplog.text


async def test_the_setting_is_read_at_every_call() -> None:
    settings = JobsSettings()
    app = _app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/ops/jobs/rent", headers={JOBS_TOKEN_HEADER: TOKEN})).status_code == 401
        settings.jobs_token = SecretStr(TOKEN)
        assert (await client.post("/ops/jobs/rent", headers={JOBS_TOKEN_HEADER: TOKEN})).status_code == 200


def test_the_check_takes_the_setting_as_each_app_holds_it() -> None:
    """kastlan's ``SecretStr | None``, Kurvenschmiede's ``str = ""``."""
    check_jobs_token(TOKEN, SecretStr(TOKEN))
    check_jobs_token(TOKEN, TOKEN)
    for presented, expected in [(None, TOKEN), ("wrong", TOKEN), (TOKEN, None), (TOKEN, ""), ("", "")]:
        with pytest.raises(JobsTokenError) as refused:
            check_jobs_token(presented, expected)
        assert refused.value.status_code == 401 and isinstance(refused.value, ValueError)


def test_a_non_ascii_token_is_refused_never_a_type_error() -> None:
    """``compare_digest`` on two ``str`` with a non-ASCII character raises ``TypeError``."""
    with pytest.raises(JobsTokenError):
        check_jobs_token("jöbs", TOKEN)
    check_jobs_token("jöbs-ü", "jöbs-ü")


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARDEN_JOBS_TOKEN", TOKEN)
    settings = _Settings()
    assert isinstance(settings.jobs_token, SecretStr) and settings.jobs_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(settings)  # SecretStr: never in a log line
    monkeypatch.delenv("GARDEN_JOBS_TOKEN")
    assert _Settings().jobs_token is None
