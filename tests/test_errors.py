"""The kit's refusals answered at their contract status — even beside a ``ValueError`` → 400
handler, which is what keksdose (``main.py:166``) and Kurvenschmiede (``main.py:193``) have."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from eifi1_server_kit.auth import AuthError, AuthErrorCode, LoginRequest
from eifi1_server_kit.billing import BillingError, BillingErrorCode, PaddleError, PlanLimitError
from eifi1_server_kit.demo import DemoError
from eifi1_server_kit.dev_switch import DevSwitchError
from eifi1_server_kit.errors import (
    CONTRACT_ERRORS,
    contract_error_response,
    install_contract_error_handlers,
    validation_error_response,
)
from eifi1_server_kit.feedback import (
    CrashCategoryNotAssignableError,
    FeedbackError,
    FeedbackForbiddenError,
    UnknownAttachmentUrlError,
)
from eifi1_server_kit.jobs import JobsTokenError
from eifi1_server_kit.notifications import NotificationError
from eifi1_server_kit.settings import PatchNullError
from eifi1_server_kit.tours import TourProgressError
from eifi1_server_kit.translation_review import TranslationAccessError, TranslationAreaError, TranslationLocaleError
from eifi1_server_kit.uploads import (
    EmptyUploadError,
    NotAnImageError,
    UnsupportedUploadTypeError,
    UploadRejectedError,
    UploadTooLargeError,
)
from eifi1_server_kit.user_admin import (
    AccountError,
    AccountErrorCode,
    DeletionRequest,
    EmailChangeConfirm,
    EmailChangeRequest,
    RosterQueryError,
)

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
    "jobs": (JobsTokenError(), 401),
    "plain": (ValueError("an app's own invalid input"), 400),
}

#: The coded refusals answer ``{"detail", "code"}`` (§5.2), and the user-admin and billing ones too.
CODED: dict[str, tuple[AuthError | AccountError | BillingError, int]] = {
    "credentials": (AuthError(AuthErrorCode.INVALID_CREDENTIALS), 401),
    "closed": (AuthError(AuthErrorCode.REGISTRATION_CLOSED), 403),
    "taken": (AuthError(AuthErrorCode.EMAIL_TAKEN, "That address has an account"), 409),
    "token": (AuthError(AuthErrorCode.TOKEN_INVALID), 400),
    "expired": (AuthError(AuthErrorCode.TOKEN_EXPIRED), 400),
    "session_ended": (AuthError(AuthErrorCode.SESSION_ENDED), 401),
    "last_admin": (AccountError(AccountErrorCode.LAST_ADMIN), 409),
    "mismatch": (AccountError(AccountErrorCode.CONFIRMATION_MISMATCH), 409),
    "billing_off": (BillingError(BillingErrorCode.BILLING_DISABLED), 404),
    "read_only": (BillingError(BillingErrorCode.BILLING_READ_ONLY), 402),
    "provider_down": (BillingError(BillingErrorCode.BILLING_PROVIDER_UNAVAILABLE), 502),
    "not_at_provider": (BillingError(BillingErrorCode.BILLING_NOT_AT_PROVIDER), 409),
    "subscribed": (BillingError(BillingErrorCode.BILLING_ALREADY_SUBSCRIBED), 409),
    "not_sold": (BillingError(BillingErrorCode.BILLING_PLAN_NOT_SOLD), 422),
    # Paddle's own words stay on the exception; the body says the kit's (§14.2).
    "paddle": (PaddleError(404, paddle_code="not_found", paddle_detail="Entity ctm_x not found"), 502),
    "paddle_key": (PaddleError(403, paddle_code="forbidden"), 503),
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

    @app.get("/plan-limit")
    async def plan_limit() -> None:
        raise PlanLimitError(dimension="budgets", plan="free", limit=1, used=1)

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
        # A plan limit names the dimension, the plan, the limit and the count (billing §3.4).
        limited = await client.get("/plan-limit")
        assert (limited.status_code, limited.json()) == (
            402,
            {
                "detail": "The free plan's limit on budgets is 1",
                "code": "plan_limit",
                "dimension": "budgets",
                "plan": "free",
                "limit": 1,
                "used": 1,
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
        PatchNullError,
        DemoError,
        DevSwitchError,
        BillingError,
        PlanLimitError,
        JobsTokenError,
        NotificationError,
        TourProgressError,
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


# --- 0.16.1: a 422 never echoes the body ------------------------------------------------

SECRET = "hunter2-" + "x" * 200  # past bcrypt's 72 bytes: a too-long password, still a secret


def _validating_app(*, own_handler: str | None = None, **install: bool) -> FastAPI:
    """The four bodies that carry a password or a token, behind ``install_contract_error_handlers``.

    ``own_handler`` is ``"before"`` / ``"after"`` for an app that registers its own
    ``RequestValidationError`` handler before or after the kit's call."""
    app = FastAPI()

    async def own(_request: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": "the app's own"})

    if own_handler == "before":
        app.add_exception_handler(RequestValidationError, own)
    install_contract_error_handlers(app, **install)
    if own_handler == "after":
        app.add_exception_handler(RequestValidationError, own)

    @app.post("/auth/login")
    async def login(body: LoginRequest) -> None: ...

    @app.post("/auth/me/email")
    async def email(body: EmailChangeRequest) -> None: ...

    @app.post("/auth/me/email/confirm")
    async def confirm(body: EmailChangeConfirm) -> None: ...

    @app.post("/auth/me/deletion")
    async def deletion(body: DeletionRequest) -> None: ...

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> None: ...

    return app


async def _post(app: FastAPI, path: str, **kwargs: object) -> tuple[int, str, object]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request("POST" if path.startswith("/auth") else "GET", path, **kwargs)  # type: ignore[arg-type]
    return response.status_code, response.text, response.json()


async def test_a_too_long_password_is_refused_without_echoing_it() -> None:
    status, text, body = await _post(
        _validating_app(), "/auth/login", json={"email": "ada@example.com", "password": SECRET}
    )
    assert status == 422
    assert SECRET not in text and "hunter2" not in text and "ada@example.com" not in text
    [error] = body["detail"]  # type: ignore[index]
    assert set(error) == {"type", "loc", "msg"}
    assert (error["type"], error["loc"]) == ("value_error", ["body", "password"])
    assert error["msg"].startswith("Value error, Password must be at most 72 bytes")


async def test_a_malformed_body_echoes_none_of_its_values() -> None:
    app = _validating_app()
    # A too-long password, an unknown field beside it (extra="forbid"), and a missing one.
    status, text, body = await _post(
        app, "/auth/me/email", json={"password": SECRET, "new_emial": "ada@example.com", "note": "sk-live-123"}
    )
    assert status == 422
    for value in (SECRET, "hunter2", "ada@example.com", "sk-live-123"):
        assert value not in text
    assert sorted((error["type"], tuple(error["loc"])) for error in body["detail"]) == [  # type: ignore[index]
        ("extra_forbidden", ("body", "new_emial")),
        ("extra_forbidden", ("body", "note")),
        ("missing", ("body", "new_email")),
        ("value_error", ("body", "password")),
    ]
    for error in body["detail"]:  # type: ignore[index]
        assert set(error) == {"type", "loc", "msg"} and error["msg"]
    # A token past its 256 characters, a wrong-typed password, a body that is no object.
    token = "tok_" + "y" * 300
    status, text, _ = await _post(app, "/auth/me/email/confirm", json={"token": token})
    assert status == 422 and token not in text and "url" not in text and "ctx" not in text
    status, text, _ = await _post(app, "/auth/me/deletion", json={"password": ["hunter2"], "confirm_email": SECRET})
    assert status == 422 and "hunter2" not in text
    status, text, _ = await _post(app, "/auth/login", json=["hunter2", SECRET])
    assert status == 422 and "hunter2" not in text
    # JSON that does not parse: FastAPI's json_invalid, its position kept.
    status, text, body = await _post(
        app, "/auth/login", content=b'{"password": "hunter2"', headers={"content-type": "application/json"}
    )
    assert status == 422 and "hunter2" not in text
    assert body["detail"][0]["type"] == "json_invalid"  # type: ignore[index]
    # Every route, not only bodies: a path parameter is answered the same way.
    status, text, body = await _post(app, "/items/secret-path-value")
    assert status == 422 and "secret-path-value" not in text
    assert body["detail"][0]["loc"] == ["path", "item_id"]  # type: ignore[index]


async def test_an_apps_own_validation_handler_is_kept_whichever_comes_first() -> None:
    for order in ("before", "after"):
        status, _, body = await _post(_validating_app(own_handler=order), "/auth/login", json={})
        assert (status, body) == (400, {"detail": "the app's own"}), order


async def test_redaction_can_be_switched_off() -> None:
    status, text, _ = await _post(
        _validating_app(redact_validation_errors=False),
        "/auth/login",
        json={"email": "ada@example.com", "password": SECRET},
    )
    assert status == 422 and SECRET in text  # FastAPI's own answer, input and all


async def test_a_bare_starlette_app_gets_no_validation_handler() -> None:
    app = Starlette()
    install_contract_error_handlers(app)
    assert RequestValidationError not in app.exception_handlers


async def test_the_redacting_handler_drops_input_ctx_and_url() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "token"),
                "msg": "String should have at most 256 characters",
                "input": "tok_secret",
                "ctx": {"max_length": 256},
                "url": "https://errors.pydantic.dev/2/v/string_too_long",
            }
        ]
    )
    response = await validation_error_response(None, exc)  # type: ignore[arg-type]
    assert response.status_code == 422
    assert response.body == (
        b'{"detail":[{"type":"string_too_long","loc":["body","token"],'
        b'"msg":"String should have at most 256 characters"}]}'
    )
