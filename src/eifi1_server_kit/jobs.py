"""The scheduler's calls: a route no account is behind, opened by ``X-Jobs-Token``.

``docs/billing-harmonization.md`` §14.7 in ``Eifi1/ui-kit``: a daily job — the trial and
grant end notices, and kastlan's every daily job — is ``POST /api/v1/ops/…``, called by a
Cloud Scheduler HTTP job. No account is behind the call, so there is no session and no
role: the caller proves it is the job with **a header, never a query parameter** (Cloud
Run's request log records the URL with its query), compared **in constant time** with the
app's ``<APP>_JOBS_TOKEN`` secret. Missing, wrong, or no token set: **401**, one answer for
all three; the last is logged too, because then the operator has something to do.

kastlan (``adapters/api/ops_router.py:35``) and Kurvenschmiede
(``adapters/api/ops_router.py:39``) each wrote that dependency; this is it once (round
0.34 S5). keksdose runs its jobs in-process and has no token route.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, SecretStr
from starlette.requests import Request

__all__ = ["JOBS_TOKEN_HEADER", "JobsSettings", "JobsTokenError", "check_jobs_token", "require_jobs_token"]

logger = logging.getLogger("eifi1_server_kit.jobs")

#: The header the scheduler sends the token in (Cloud Scheduler's ``--headers``).
JOBS_TOKEN_HEADER = "X-Jobs-Token"


class JobsSettings(BaseModel):
    """The token setting, for the app's settings to INHERIT, so each reads its own
    variable (``KASTLAN_JOBS_TOKEN``, ``KURVENSCHMIEDE_JOBS_TOKEN``)::

        class Settings(BaseSettings, DemoSettings, BillingSettings, JobsSettings):
            model_config = SettingsConfigDict(env_prefix="KASTLAN_")
    """

    model_config = ConfigDict(from_attributes=True)

    #: The scheduler's token: Secret Manager in production, never a deploy literal. Unset —
    #: or blank, as an ``.env`` template leaves it — every token route answers 401.
    jobs_token: SecretStr | None = None


class JobsTokenError(ValueError):
    """The call isn't the scheduler's (→ 401): ``X-Jobs-Token`` missing or wrong, or no
    token set. One answer for the three, as kastlan's ``AuthError`` and Kurvenschmiede's
    ``HTTPException`` gave it; answered through
    :func:`~eifi1_server_kit.errors.install_contract_error_handlers`."""

    status_code: int = 401

    def __init__(self, detail: str = f"A valid {JOBS_TOKEN_HEADER} is required") -> None:
        super().__init__(detail)


def check_jobs_token(presented: str | None, expected: SecretStr | str | None) -> None:
    """Refuse unless ``presented`` — the request's ``X-Jobs-Token`` — is the app's token:
    :class:`JobsTokenError` (401) for a missing or wrong one, and for no token set (logged
    at WARNING: the operator's to fix). ``expected`` is the setting as the app holds it,
    a ``SecretStr`` (kastlan) or text (Kurvenschmiede's ``str = ""``); blank is unset.

    :func:`hmac.compare_digest` on BYTES: two ``str`` with a non-ASCII character raise
    ``TypeError`` there, which an unauthenticated caller could turn into a 500 at will.
    """
    secret = expected.get_secret_value() if isinstance(expected, SecretStr) else expected
    if secret is None or not secret.strip():
        logger.warning("a jobs route was called and no jobs token is set: refused")
        raise JobsTokenError
    if presented is None or not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        raise JobsTokenError


def require_jobs_token(token: Callable[[], SecretStr | str | None]) -> Callable[[Request], None]:
    """The FastAPI dependency for a token route, reading the app's token through ``token``
    at every call — so a test that sets the setting is heard::

        @router.post(
            "/ops/jobs/{name}",
            dependencies=[Depends(require_jobs_token(lambda: settings.jobs_token))],
        )

    It reads ``X-Jobs-Token`` from the request (Starlette's ``Request``, which FastAPI
    hands a dependency; the kit doesn't import FastAPI) and calls
    :func:`check_jobs_token`. So the header is not a parameter in the app's OpenAPI
    schema — nobody but the scheduler calls these routes.
    """

    def jobs_token_dependency(request: Request) -> None:
        check_jobs_token(request.headers.get(JOBS_TOKEN_HEADER), token())

    return jobs_token_dependency
