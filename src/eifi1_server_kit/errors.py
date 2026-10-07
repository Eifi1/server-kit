"""The kit's refusals as HTTP answers: one call, so no app maps them by hand — or forgets to.

Every refusal the kit raises is a plain exception carrying the contract's status as
``status_code`` (:class:`~eifi1_server_kit.feedback.FeedbackError` 422 / 403,
:class:`~eifi1_server_kit.uploads.UploadRejectedError` 400 / 413 / 415, the translation
review's 422 / 403, :class:`~eifi1_server_kit.auth.AuthError` 400 / 401 / 403 / 409,
user administration's :class:`~eifi1_server_kit.user_admin.AccountError` 409 and
:class:`~eifi1_server_kit.user_admin.RosterQueryError` 422, and the settings rule's
:class:`~eifi1_server_kit.settings.PatchNullError` 422).
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
``{"detail": …, "code": "last_admin", "companies": [...]}``).

kastlan routes its refusals through ``DomainError`` and may keep mapping kit errors
there; the installer is the same answer without the mapping.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from eifi1_server_kit.auth.errors import AuthError
from eifi1_server_kit.feedback.errors import FeedbackError
from eifi1_server_kit.settings import PatchNullError
from eifi1_server_kit.translation_review.scope import (
    TranslationAccessError,
    TranslationAreaError,
    TranslationLocaleError,
)
from eifi1_server_kit.uploads import UploadRejectedError
from eifi1_server_kit.user_admin.errors import AccountError
from eifi1_server_kit.user_admin.roster import RosterQueryError

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
)


async def contract_error_response(_request: Request, exc: Exception) -> Response:
    """``{"detail": str(exc)}`` at the refusal's own ``status_code`` — the handler
    :func:`install_contract_error_handlers` registers, for an app that registers it itself.

    A refusal with a ``code`` (:class:`~eifi1_server_kit.auth.AuthError`,
    :class:`~eifi1_server_kit.user_admin.AccountError`,
    :class:`~eifi1_server_kit.settings.PatchNullError`) adds it: ``{"detail": …, "code":
    …}``, and then its ``extra`` fields beside them — never in place of them. Those without
    a code answer exactly as before.
    """
    body: dict[str, object] = {"detail": str(exc)}
    code = getattr(exc, "code", None)
    if code is not None:
        body["code"] = str(code)
        extra = getattr(exc, "extra", None)
        if isinstance(extra, Mapping):
            body.update({key: value for key, value in extra.items() if key not in body})
    return JSONResponse(body, status_code=getattr(exc, "status_code", 500))


def install_contract_error_handlers(app: Starlette) -> None:
    """Answer every uncaught kit refusal at its contract status, beside any ``ValueError`` handler.

    Call it while building the app, before the first request (Starlette builds its
    middleware stack, handlers included, once). Works on a FastAPI app — it is a
    Starlette one.
    """
    for error in CONTRACT_ERRORS:
        app.add_exception_handler(error, contract_error_response)
