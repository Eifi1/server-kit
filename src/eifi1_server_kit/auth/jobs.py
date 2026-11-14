"""The account layer's daily jobs as the scheduler's routes (0.31), behind the ``fastapi``
extra.

The account tables keep rows nobody reads again, some with personal data (a session's IP
address and browser), and an ``after_days`` deletion promises its day in a mail. Both are
jobs, not requests: :func:`~eifi1_server_kit.auth.sql.prune_account_rows` and
:func:`~eifi1_server_kit.user_admin.service.erase_due_accounts`. This router is the one way
every app runs them — a Cloud Scheduler HTTP job, once a day, with the app's jobs token
(:mod:`eifi1_server_kit.jobs`, ``X-Jobs-Token``), as kastlan's and Kurvenschmiede's daily
jobs already run::

    app.include_router(
        account_jobs_router(AccountJobsDeps(token=lambda: settings.jobs_token, prune=prune, erase_due=erase_due)),
        prefix="/api/v1",
    )

* ``POST /ops/accounts/daily`` — the scheduler's one call: the due erasures first (an erased
  account's rows go with it), then the prune; answers both counts.
* ``POST /ops/accounts/prune`` and ``POST /ops/accounts/erase-due`` — each alone (a manual
  run, a second schedule). ``erase-due`` is mounted only with ``erase_due``: an app whose
  operator erases (``DeletionMode.OPERATOR``) has nothing due.

``prune`` and ``erase_due`` are the app's, because the transaction is: ``prune(before)``
opens a session, calls ``prune_account_rows(before=before, sessions=…, passkeys=…, links=…,
invitations=…)`` and commits; ``erase_due()`` reads the candidates and runs
``erase_due_accounts(ids, erase_one)`` (its docstring has the five lines). The kit decides
the retention (:data:`ACCOUNT_ROWS_RETENTION`, the privacy policy's number), the token, the
order and the answer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, SecretStr

from eifi1_server_kit.clock import Clock, utc_now
from eifi1_server_kit.jobs import require_jobs_token
from eifi1_server_kit.user_admin.service import ErasureRun

try:
    from fastapi import APIRouter, Depends
except ImportError as exc:
    raise ImportError(
        "eifi1_server_kit.auth.jobs needs FastAPI: install the kit with its fastapi extra, eifi1-server-kit[fastapi]."
    ) from exc

__all__ = [
    "ACCOUNT_ROWS_RETENTION",
    "AccountJobsDeps",
    "DailyAccountJobs",
    "ErasureAnswer",
    "PruneAnswer",
    "PruneCounts",
    "account_jobs_router",
]

logger = logging.getLogger("eifi1_server_kit.auth.jobs")

#: How long the account tables keep a row that is done with — an ended session with its IP
#: address and browser, a spent challenge, an expired link or invitation — before the daily
#: prune deletes it. The number an app's privacy policy names for them.
ACCOUNT_ROWS_RETENTION = timedelta(days=30)


class PruneCounts(Protocol):
    """What ``prune`` answers: :class:`~eifi1_server_kit.auth.sql.AccountPrune` is one."""

    @property
    def sessions(self) -> int: ...
    @property
    def challenges(self) -> int: ...
    @property
    def links(self) -> int: ...
    @property
    def invitations(self) -> int: ...


class PruneAnswer(BaseModel):
    """``POST /ops/accounts/prune``: the cut-off and how many rows went, per table."""

    before: datetime
    sessions: int
    challenges: int
    links: int
    invitations: int
    total: int


class ErasureAnswer(BaseModel):
    """``POST /ops/accounts/erase-due``: the accounts erased, skipped (not due any more) and
    failed (logged, asked again on the next run)."""

    erased: list[int]
    skipped: list[int]
    failed: list[int]


class DailyAccountJobs(BaseModel):
    """``POST /ops/accounts/daily``: both; ``erasure`` is ``None`` without ``erase_due``."""

    erasure: ErasureAnswer | None
    prune: PruneAnswer


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountJobsDeps:
    """What :func:`account_jobs_router` needs from the app."""

    #: The app's jobs token, read at every call (``lambda: settings.jobs_token``).
    token: Callable[[], SecretStr | str | None]
    #: Delete the rows done with before the cut-off, in the app's own transaction.
    prune: Callable[[datetime], Awaitable[PruneCounts]]
    #: The ``after_days`` erasures that are due; ``None`` in operator mode.
    erase_due: Callable[[], Awaitable[ErasureRun]] | None = None
    retention: timedelta = ACCOUNT_ROWS_RETENTION
    now: Clock = utc_now

    def __post_init__(self) -> None:
        if self.retention <= timedelta(0):
            raise ValueError("the retention is more than nothing: a row still in use would go")


def account_jobs_router(
    deps: AccountJobsDeps, *, prefix: str = "/ops/accounts", tags: Sequence[str] = ("ops",)
) -> APIRouter:
    """The scheduler's routes for the account layer's daily jobs (module docstring)."""
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=[Depends(require_jobs_token(deps.token))])
    erase_due = deps.erase_due

    async def prune() -> PruneAnswer:
        before = deps.now() - deps.retention
        counts = await deps.prune(before)
        total = counts.sessions + counts.challenges + counts.links + counts.invitations
        logger.info("account prune: %s row(s) done with before %s", total, before.isoformat())
        return PruneAnswer(
            before=before,
            sessions=counts.sessions,
            challenges=counts.challenges,
            links=counts.links,
            invitations=counts.invitations,
            total=total,
        )

    async def erasure(job: Callable[[], Awaitable[ErasureRun]]) -> ErasureAnswer:
        run = await job()
        logger.info(
            "account erasure: %s erased, %s skipped, %s failed", len(run.erased), len(run.skipped), len(run.failed)
        )
        return ErasureAnswer(erased=list(run.erased), skipped=list(run.skipped), failed=list(run.failed))

    @router.post("/daily", response_model=DailyAccountJobs)
    async def daily() -> DailyAccountJobs:
        """The scheduler's daily call: the due erasures, then the prune."""
        erased = None if erase_due is None else await erasure(erase_due)
        return DailyAccountJobs(erasure=erased, prune=await prune())

    @router.post("/prune", response_model=PruneAnswer)
    async def prune_route() -> PruneAnswer:
        """Delete the account rows done with longer than the retention."""
        return await prune()

    if erase_due is not None:
        job = erase_due

        @router.post("/erase-due", response_model=ErasureAnswer)
        async def erase_due_route() -> ErasureAnswer:
            """Erase the accounts whose deletion is due (``after_days``)."""
            return await erasure(job)

    return router
