"""0.31: the account layer's daily jobs as the scheduler's routes (``auth.jobs``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from eifi1_server_kit.auth.jobs import ACCOUNT_ROWS_RETENTION, AccountJobsDeps, account_jobs_router
from eifi1_server_kit.auth.sql import AccountPrune
from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.user_admin.service import ErasureRun

NOW = datetime(2026, 11, 14, 3, 30, tzinfo=UTC)
TOKEN = {"X-Jobs-Token": "s3cret"}


def _client(deps: AccountJobsDeps) -> AsyncClient:
    app = FastAPI()
    install_contract_error_handlers(app)
    app.include_router(account_jobs_router(deps), prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class Jobs:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def prune(self, before: datetime) -> AccountPrune:
        self.calls.append(("prune", before))
        return AccountPrune(sessions=3, challenges=2, links=1, invitations=0)

    async def erase_due(self) -> ErasureRun:
        self.calls.append("erase")
        return ErasureRun(erased=(7,), skipped=(8,), failed=())


async def test_the_daily_job_erases_what_is_due_then_prunes_what_is_30_days_done() -> None:
    jobs = Jobs()
    deps = AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, erase_due=jobs.erase_due, now=lambda: NOW)
    async with _client(deps) as client:
        refused = await client.post("/api/v1/ops/accounts/daily")
        assert refused.status_code == 401 and jobs.calls == []
        answer = await client.post("/api/v1/ops/accounts/daily", headers=TOKEN)
    assert answer.status_code == 200, answer.text
    assert timedelta(days=30) == ACCOUNT_ROWS_RETENTION
    assert jobs.calls == ["erase", ("prune", NOW - timedelta(days=30))]
    assert answer.json() == {
        "uploads": None,
        "erasure": {"erased": [7], "skipped": [8], "failed": []},
        "prune": {
            "before": "2026-10-15T03:30:00Z",
            "sessions": 3,
            "challenges": 2,
            "links": 1,
            "invitations": 0,
            "total": 6,
        },
    }


async def test_each_job_has_its_own_route_and_the_retention_is_the_app_s() -> None:
    jobs = Jobs()
    deps = AccountJobsDeps(
        token=lambda: "s3cret", prune=jobs.prune, erase_due=jobs.erase_due, retention=timedelta(days=7), now=lambda: NOW
    )
    async with _client(deps) as client:
        pruned = (await client.post("/api/v1/ops/accounts/prune", headers=TOKEN)).json()
        erased = (await client.post("/api/v1/ops/accounts/erase-due", headers=TOKEN)).json()
    assert pruned["before"] == "2026-11-07T03:30:00Z" and pruned["total"] == 6
    assert erased == {"erased": [7], "skipped": [8], "failed": []}
    assert jobs.calls == [("prune", NOW - timedelta(days=7)), "erase"]


async def test_without_an_erasure_job_only_the_prune_runs() -> None:
    """An app whose operator erases (Kurvenschmiede's operator mode) has no due erasures."""
    jobs = Jobs()
    async with _client(AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, now=lambda: NOW)) as client:
        assert (await client.post("/api/v1/ops/accounts/erase-due", headers=TOKEN)).status_code == 404
        daily = (await client.post("/api/v1/ops/accounts/daily", headers=TOKEN)).json()
    assert daily["erasure"] is None and daily["prune"]["total"] == 6


def test_a_retention_of_nothing_or_less_is_refused() -> None:
    jobs = Jobs()
    for retention in (timedelta(0), timedelta(days=-1)):
        with pytest.raises(ValueError, match="retention"):
            AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, retention=retention)
    with pytest.raises(ValueError, match="upload wait"):
        AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, upload_wait=timedelta(0))


async def test_the_daily_job_sweeps_the_uploads_nothing_names_after_a_day() -> None:
    """0.31: the orphan sweep (``SqlAttachmentStorage.prune``) in the same daily call, so a
    file the purge spared does not stay: an upload waits a day for what names it."""
    from eifi1_server_kit.auth.jobs import UPLOAD_WAIT

    jobs = Jobs()
    swept: list[datetime] = []

    async def sweep_uploads(before: datetime) -> int:
        swept.append(before)
        return 4

    deps = AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, sweep_uploads=sweep_uploads, now=lambda: NOW)
    async with _client(deps) as client:
        daily = (await client.post("/api/v1/ops/accounts/daily", headers=TOKEN)).json()
        alone = (await client.post("/api/v1/ops/accounts/sweep-uploads", headers=TOKEN)).json()
    assert timedelta(days=1) == UPLOAD_WAIT
    assert daily["uploads"] == 4 and alone == {"before": "2026-11-13T03:30:00Z", "uploads": 4}
    assert swept == [NOW - timedelta(days=1)] * 2
    async with _client(AccountJobsDeps(token=lambda: "s3cret", prune=jobs.prune, now=lambda: NOW)) as client:
        assert (await client.post("/api/v1/ops/accounts/daily", headers=TOKEN)).json()["uploads"] is None
        assert (await client.post("/api/v1/ops/accounts/sweep-uploads", headers=TOKEN)).status_code == 404
