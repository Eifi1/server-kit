"""The kit's refusals answered at their contract status — even beside a ``ValueError`` → 400
handler, which is what keksdose (``main.py:166``) and Kurvenschmiede (``main.py:193``) have."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from eifi1_server_kit.auth import AuthError, AuthErrorCode
from eifi1_server_kit.errors import CONTRACT_ERRORS, contract_error_response, install_contract_error_handlers
from eifi1_server_kit.feedback import (
    CrashCategoryNotAssignableError,
    FeedbackError,
    FeedbackForbiddenError,
    UnknownAttachmentUrlError,
)
from eifi1_server_kit.translation_review import TranslationAccessError, TranslationAreaError, TranslationLocaleError
from eifi1_server_kit.uploads import (
    EmptyUploadError,
    NotAnImageError,
    UnsupportedUploadTypeError,
    UploadRejectedError,
    UploadTooLargeError,
)
from eifi1_server_kit.user_admin import AccountError, AccountErrorCode, RosterQueryError

RAISED: dict[str, tuple[Exception, int]] = {
    "forbidden": (FeedbackForbiddenError("Not your report"), 403),
    "crash": (CrashCategoryNotAssignableError("CRASH is filed by the error boundary"), 422),
    "url": (UnknownAttachmentUrlError("not an attachment of this app"), 422),
    "empty": (EmptyUploadError("The file is empty."), 400),
    "huge": (UploadTooLargeError("The file is larger than 10 MB."), 413),
    "lying": (NotAnImageError("can't be read as one"), 400),
    "type": (UnsupportedUploadTypeError("Only images, PDF or text files are allowed."), 415),
    "locale": (TranslationLocaleError("Unknown locale 'de'"), 422),
    "area": (TranslationAreaError("Unknown area 'x'"), 422),
    "access": (TranslationAccessError("Not granted for locale 'fr'"), 403),
    "roster": (RosterQueryError("Unknown sort key 'plan'"), 422),
    "plain": (ValueError("an app's own invalid input"), 400),
}

#: The coded refusals answer ``{"detail", "code"}`` (§5.2), and the user-admin ones too.
CODED: dict[str, tuple[AuthError | AccountError, int]] = {
    "credentials": (AuthError(AuthErrorCode.INVALID_CREDENTIALS), 401),
    "closed": (AuthError(AuthErrorCode.REGISTRATION_CLOSED), 403),
    "taken": (AuthError(AuthErrorCode.EMAIL_TAKEN, "That address has an account"), 409),
    "token": (AuthError(AuthErrorCode.TOKEN_INVALID), 400),
    "expired": (AuthError(AuthErrorCode.TOKEN_EXPIRED), 400),
    "last_admin": (AccountError(AccountErrorCode.LAST_ADMIN), 409),
    "mismatch": (AccountError(AccountErrorCode.CONFIRMATION_MISMATCH), 409),
}


def _app(*, value_error_first: bool) -> FastAPI:
    app = FastAPI()

    async def value_error_to_400(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    if value_error_first:
        app.add_exception_handler(ValueError, value_error_to_400)
    install_contract_error_handlers(app)
    if not value_error_first:
        app.add_exception_handler(ValueError, value_error_to_400)

    @app.get("/raise/{name}")
    async def raising(name: str) -> None:
        raise RAISED[name][0]

    @app.get("/coded/{name}")
    async def coded(name: str) -> None:
        raise CODED[name][0]

    @app.get("/companies")
    async def companies() -> None:
        raise AccountError(AccountErrorCode.LAST_ADMIN, extra={"companies": ["Example AG", "Muster GmbH"]})

    @app.get("/caught")
    async def caught() -> None:
        try:
            raise FeedbackForbiddenError("Not your report")
        except FeedbackError as exc:
            raise HTTPException(404, "Feedback not found") from exc

    return app


@pytest.mark.parametrize("value_error_first", [True, False])
async def test_every_kit_refusal_keeps_its_status_beside_a_value_error_handler(value_error_first: bool) -> None:
    """Starlette resolves a handler along the exception's MRO, so the kit's classes win over
    ``ValueError`` whichever was registered first."""
    transport = ASGITransport(app=_app(value_error_first=value_error_first))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for name, (exc, status_code) in RAISED.items():
            response = await client.get(f"/raise/{name}")
            assert (response.status_code, response.json()) == (status_code, {"detail": str(exc)}), name
        for name, (auth_exc, status_code) in CODED.items():
            response = await client.get(f"/coded/{name}")
            expected = {"detail": str(auth_exc), "code": auth_exc.code.value}
            assert (response.status_code, response.json()) == (status_code, expected), name
        # A refusal that names something carries it beside the code (kastlan's companies, §6.4).
        named = await client.get("/companies")
        assert (named.status_code, named.json()) == (
            409,
            {
                "detail": "This would leave no active admin",
                "code": "last_admin",
                "companies": ["Example AG", "Muster GmbH"],
            },
        )
        # An endpoint's own mapping still comes first (Kurvenschmiede's foreign row is a 404).
        caught = await client.get("/caught")
        assert (caught.status_code, caught.json()) == (404, {"detail": "Feedback not found"})


async def test_a_bare_starlette_app_takes_it_too() -> None:
    async def refuse(_request: Request) -> None:
        raise UnsupportedUploadTypeError("Only images, PDF or text files are allowed.")

    app = Starlette(routes=[Route("/upload", refuse, methods=["POST"])])
    install_contract_error_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/upload")
    assert response.status_code == 415
    assert response.json() == {"detail": "Only images, PDF or text files are allowed."}


def test_the_registered_classes_are_every_kit_refusal_with_a_status() -> None:
    assert set(CONTRACT_ERRORS) == {
        FeedbackError,
        UploadRejectedError,
        TranslationLocaleError,
        TranslationAreaError,
        TranslationAccessError,
        AuthError,
        AccountError,
        RosterQueryError,
    }
    for error in CONTRACT_ERRORS:
        assert isinstance(getattr(error, "status_code", None), int), error


async def test_extra_fields_never_replace_the_detail_or_the_code() -> None:
    """Set on the instance after construction, past ``AccountError``'s own check."""
    error = AccountError(AccountErrorCode.OTHER_COMPANIES)
    error.extra = {"code": "spoofed", "detail": "spoofed", "companies": ["Example AG"]}
    response = await contract_error_response(None, error)  # type: ignore[arg-type]
    assert response.body == (
        b'{"detail":"This account belongs to other companies too","code":"other_companies","companies":["Example AG"]}'
    )


async def test_the_handler_answers_a_stray_exception_as_a_server_error() -> None:
    """Registered by hand on something that is not a kit refusal, it does not invent a 4xx."""
    response = await contract_error_response(None, RuntimeError("boom"))  # type: ignore[arg-type]
    assert response.status_code == 500 and response.body == b'{"detail":"boom"}'
