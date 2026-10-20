"""The kit's refusals as HTTP answers: one call, so no app maps them by hand — or forgets to.

Every refusal the kit raises is a plain exception carrying the contract's status as
``status_code`` (:class:`~eifi1_server_kit.feedback.FeedbackError` 422 / 403 / 404 / 429,
:class:`~eifi1_server_kit.uploads.UploadRejectedError` 400 / 413 / 415, the translation
review's 422 / 403, :class:`~eifi1_server_kit.auth.AuthError` 400 / 401 / 403 / 409,
user administration's :class:`~eifi1_server_kit.user_admin.AccountError` 409 and
:class:`~eifi1_server_kit.user_admin.RosterQueryError` 422, the settings rule's
:class:`~eifi1_server_kit.settings.PatchNullError` 422, the demo's
:class:`~eifi1_server_kit.demo.DemoError` 403 / 404 / 429 / 503, the dev switch's
:class:`~eifi1_server_kit.dev_switch.DevSwitchError` 404 / 409 / 422, and billing's
:class:`~eifi1_server_kit.billing.BillingError` 400 / 402 / 404 / 409 / 422 / 502 / 503 and
:class:`~eifi1_server_kit.billing.PlanLimitError` 402, a token route's
:class:`~eifi1_server_kit.jobs.JobsTokenError` 401, and the notifications'
:class:`~eifi1_server_kit.notifications.NotificationError` 400 / 409 / 422, and tour
progress's :class:`~eifi1_server_kit.tours.TourProgressError` 422).
Most of them are :class:`ValueError` subclasses — on purpose, so one
raised inside a Pydantic validator is still a 422 there — and that is the trap this module
closes: keksdose (``main.py:166``) and Kurvenschmiede (``main.py:193``) each answer every
uncaught ``ValueError`` with a 400, so a kit refusal that slipped past an endpoint's own
``except`` came out as 400 instead of 403, 415 or 422, silently.

:func:`install_contract_error_handlers` registers one handler for each kit base class.
Starlette picks the handler by walking the exception's MRO
(``starlette/_exception_handler.py`` ``_lookup_exception_handler``) and takes the first
class it has one for, so ``FeedbackForbiddenError`` reaches the ``FeedbackError`` handler
before ``ValueError``'s — whichever was registered first. An endpoint's own ``except``
still wins over both; this answers only what nobody caught. The body is FastAPI's own
``HTTPException`` shape, ``{"detail": str(exc)}``, so no client can tell the difference —
plus ``"code"`` for a refusal that carries one (:class:`~eifi1_server_kit.auth.AuthError`:
``{"detail": "Invalid credentials", "code": "invalid_credentials"}``), which the kit's
pages switch on instead of the English detail — and the refusal's ``extra`` fields, for
one that names something (:class:`~eifi1_server_kit.user_admin.AccountError`: kastlan's
``{"detail": …, "code": "last_admin", "companies": [...]}``; a plan limit's ``dimension``,
``plan``, ``limit`` and ``used``) — and the refusal's
``headers``, for one that carries a ``Retry-After``
(:class:`~eifi1_server_kit.demo.DemoError`'s ``demo_rate_limited``).

kastlan routes its refusals through ``DomainError`` and may keep mapping kit errors
there; the installer is the same answer without the mapping.

**A 422 never echoes the request** (0.17.0). FastAPI's own ``RequestValidationError``
answer puts each error's ``input`` (and ``ctx``, and pydantic's ``url``) in the body — so
a too-long password in ``LoginRequest``, ``EmailChangeRequest`` or ``DeletionRequest``, a
malformed ``EmailChangeConfirm`` token, any body's values came back in the 422 and landed
in request and response logs. On a FastAPI app the installer also registers
:func:`validation_error_response`: FastAPI's same shape, ``{"detail": [{"type", "loc",
"msg"}, ...]}``, without ``input``, ``ctx`` or ``url``, for every route — no app has to
take bodies untyped and parse them by hand. An app's own ``RequestValidationError``
handler is kept: registered before the call, the kit leaves it alone; registered after,
it replaces the kit's as any later registration does. ``redact_validation_errors=False``
keeps FastAPI's echoing answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from eifi1_server_kit.auth.errors import AuthError
from eifi1_server_kit.billing.errors import BillingError, PlanLimitError
from eifi1_server_kit.demo import DemoError
from eifi1_server_kit.dev_switch import DevSwitchError
from eifi1_server_kit.feedback.errors import FeedbackError
from eifi1_server_kit.jobs import JobsTokenError
from eifi1_server_kit.notifications.errors import NotificationError
from eifi1_server_kit.settings import PatchNullError
from eifi1_server_kit.tours.errors import TourProgressError
from eifi1_server_kit.translation_review.scope import (
    TranslationAccessError,
    TranslationAreaError,
    TranslationLocaleError,
)
from eifi1_server_kit.uploads import UploadRejectedError
from eifi1_server_kit.user_admin.errors import AccountError
from eifi1_server_kit.user_admin.roster import RosterQueryError

if TYPE_CHECKING:
    from fastapi.exceptions import RequestValidationError

__all__ = [
    "CONTRACT_ERRORS",
    "contract_error_response",
    "install_contract_error_handlers",
    "validation_error_response",
]

#: Every kit exception base that carries a ``status_code``; subclasses are covered by MRO.
CONTRACT_ERRORS: tuple[type[Exception], ...] = (
    FeedbackError,
    UploadRejectedError,
    TranslationLocaleError,
    TranslationAreaError,
    TranslationAccessError,
    AuthError,
    AccountError,
    RosterQueryError,
    PatchNullError,
    DemoError,
    DevSwitchError,
    BillingError,
    PlanLimitError,
    JobsTokenError,
    NotificationError,
    TourProgressError,
)


async def contract_error_response(_request: Request, exc: Exception) -> Response:
    """``{"detail": str(exc)}`` at the refusal's own ``status_code`` — the handler
    :func:`install_contract_error_handlers` registers, for an app that registers it itself.

    A refusal with a ``code`` (:class:`~eifi1_server_kit.auth.AuthError`,
    :class:`~eifi1_server_kit.user_admin.AccountError`,
    :class:`~eifi1_server_kit.settings.PatchNullError`) adds it: ``{"detail": …, "code":
    …}``, and then its ``extra`` fields beside them — never in place of them. Those without
    a code answer exactly as before. A refusal's ``headers`` mapping goes on the answer
    (:class:`~eifi1_server_kit.demo.DemoError`'s ``Retry-After``), as an ``HTTPException``'s
    would.
    """
    body: dict[str, object] = {"detail": str(exc)}
    code = getattr(exc, "code", None)
    if code is not None:
        body["code"] = str(code)
        extra = getattr(exc, "extra", None)
        if isinstance(extra, Mapping):
            body.update({key: value for key, value in extra.items() if key not in body})
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        body,
        status_code=getattr(exc, "status_code", 500),
        headers=dict(headers) if isinstance(headers, Mapping) and headers else None,
    )


async def validation_error_response(_request: Request, exc: Exception) -> Response:
    """FastAPI's 422, ``{"detail": [{"type", "loc", "msg"}, ...]}``, with no error's
    ``input``, ``ctx`` or ``url`` — the ``RequestValidationError`` handler
    :func:`install_contract_error_handlers` registers on a FastAPI app (0.17.0), for an app
    that registers it itself.

    ``loc`` names the field (``["body", "password"]``) and ``msg`` is pydantic's or the
    validator's own words — so a validator of the app's that writes the value into its
    message still echoes it there; the kit's never do.
    """
    errors = cast("RequestValidationError", exc).errors()
    detail = [{"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in errors]
    return JSONResponse({"detail": detail}, status_code=422)


def _is_fastapi(app: Starlette) -> bool:
    return any(cls.__module__.split(".")[0] == "fastapi" for cls in type(app).__mro__)


def install_contract_error_handlers(app: Starlette, *, redact_validation_errors: bool = True) -> None:
    """Answer every uncaught kit refusal at its contract status, beside any ``ValueError`` handler.

    Call it while building the app, before the first request (Starlette builds its
    middleware stack, handlers included, once). Works on a FastAPI app — it is a
    Starlette one.

    On a FastAPI app it also answers every request validation error with
    :func:`validation_error_response` — FastAPI's 422 without the request's values
    (0.17.0) — unless the app registered its own ``RequestValidationError`` handler
    before the call (kept) or after it (replaces the kit's), or passes
    ``redact_validation_errors=False``.
    """
    for error in CONTRACT_ERRORS:
        app.add_exception_handler(error, contract_error_response)
    if redact_validation_errors and _is_fastapi(app):
        from fastapi.exception_handlers import request_validation_exception_handler
        from fastapi.exceptions import RequestValidationError

        if app.exception_handlers.get(RequestValidationError) in (None, request_validation_exception_handler):
            app.add_exception_handler(RequestValidationError, validation_error_response)
