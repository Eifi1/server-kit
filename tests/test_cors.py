"""Extra-origin CORS — keksdose ``tests/api/test_translation_review_tokens.py:170-244``, ported
onto a bare FastAPI app with the same two layers keksdose's ``create_app`` builds."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from eifi1_server_kit.cors import (
    TRANSLATION_REVIEW_ROUTES,
    ExtraOriginCorsMiddleware,
    ExtraOriginRoute,
    assert_outside_cors_middleware,
    parse_extra_origins,
)

OWN = "https://keksdose.app"
SHOWCASE = "https://eifi1.github.io"
URL = "/api/v1/translations/reviews"
TOKENS = "/api/v1/translations/review-tokens"


def _app(*, extra: bool = True, wrong_order: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(URL)
    async def reviews() -> dict[str, list[str]]:
        return {"reviews": []}

    @app.put(URL)
    async def write() -> dict[str, list[str]]:
        return {"reviews": []}

    @app.post(f"{URL}/clear")
    async def clear() -> dict[str, int]:
        return {"cleared": 0}

    @app.post(TOKENS)
    async def mint() -> dict[str, str]:
        return {"token": "x"}

    @app.get("/api/v1/version")
    async def version() -> dict[str, str]:
        return {"version": "1"}

    def add_extra() -> None:
        app.add_middleware(ExtraOriginCorsMiddleware, origins=[SHOWCASE], routes=TRANSLATION_REVIEW_ROUTES)

    if extra and wrong_order:
        add_extra()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[OWN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Tile-Attribution"],
    )
    if extra and not wrong_order:
        add_extra()
    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as ac:
        yield ac


def _preflight(origin: str, method: str = "PUT") -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization, content-type",
    }


async def test_the_showcase_gets_narrow_cors_without_credentials(client: AsyncClient) -> None:
    """keksdose ``:211``."""
    for path, method in ((URL, "PUT"), (URL, "GET"), (f"{URL}/clear", "POST"), ("/api/v1/health", "GET")):
        r = await client.options(path, headers=_preflight(SHOWCASE, method))
        assert r.status_code == 204, (path, r.status_code)
        assert r.headers["access-control-allow-origin"] == SHOWCASE
        assert "authorization" in r.headers["access-control-allow-headers"].lower()
        assert method in r.headers["access-control-allow-methods"]
        assert r.headers["access-control-max-age"] == "600"
        # The whole point of a separate layer: never the refresh cookie.
        assert "access-control-allow-credentials" not in r.headers

    r = await client.get("/api/v1/health", headers={"Origin": SHOWCASE})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == SHOWCASE
    assert "origin" in r.headers["vary"].lower()
    assert "access-control-allow-credentials" not in r.headers
    assert "access-control-expose-headers" not in r.headers

    # Anywhere else: no CORS at all, so a browser on that origin reads nothing.
    r = await client.options("/api/v1/auth/me", headers=_preflight(SHOWCASE, "GET"))
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    r = await client.options(TOKENS, headers=_preflight(SHOWCASE, "POST"))
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    r = await client.options(URL, headers=_preflight(SHOWCASE, "DELETE"))
    assert r.status_code == 403
    r = await client.get("/api/v1/version", headers={"Origin": SHOWCASE})
    assert "access-control-allow-origin" not in r.headers


async def test_methods_are_per_route(client: AsyncClient) -> None:
    """keksdose allowed GET/PUT/POST on all three paths; each has its own now."""
    assert (await client.options("/api/v1/health", headers=_preflight(SHOWCASE, "PUT"))).status_code == 403
    assert (await client.options(f"{URL}/clear", headers=_preflight(SHOWCASE, "GET"))).status_code == 403
    r = await client.options(URL, headers=_preflight(SHOWCASE, "get"))
    assert r.status_code == 204 and r.headers["access-control-allow-methods"] == "GET, PUT"


async def test_the_apps_own_origin_keeps_its_credentialed_cors(client: AsyncClient) -> None:
    """keksdose ``:236``."""
    r = await client.options(URL, headers=_preflight(OWN))
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == OWN
    assert r.headers["access-control-allow-credentials"] == "true"
    r = await client.get(URL, headers={"Origin": OWN})
    assert r.headers["access-control-allow-credentials"] == "true"
    # An origin on neither list is still refused by the main layer.
    r = await client.options(URL, headers=_preflight("https://evil.example"))
    assert "access-control-allow-origin" not in r.headers
    # No Origin at all: untouched.
    assert (await client.get(URL)).status_code == 200


async def test_the_main_layer_really_leaks_credentials_and_this_one_strips_them() -> None:
    """Measured, not assumed (keksdose ``extra_origin_cors.py:84-91``): CORSMiddleware adds its
    simple headers — Allow-Credentials among them — to EVERY response that carries an Origin,
    allowed or not. Beside the extra layer's Allow-Origin that would expose a cookie-carrying
    response to the extra origin."""
    async with AsyncClient(transport=ASGITransport(app=_app(extra=False)), base_url="http://test") as bare:
        r = await bare.get("/api/v1/health", headers={"Origin": SHOWCASE})
        assert r.headers.get("access-control-allow-credentials") == "true", "the leak this layer exists for"
        assert "access-control-allow-origin" not in r.headers
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as narrowed:
        for path in ("/api/v1/health", "/api/v1/version"):
            r = await narrowed.get(path, headers={"Origin": SHOWCASE})
            assert "access-control-allow-credentials" not in r.headers, path


async def test_off_the_listed_paths_an_extra_origin_reads_nothing_even_if_misconfigured() -> None:
    """If the main layer ALSO allows the extra origin, this layer still keeps it to its paths."""
    app = FastAPI()

    @app.get("/api/v1/private")
    async def private() -> dict[str, str]:
        return {"secret": "x"}

    app.add_middleware(CORSMiddleware, allow_origins=[OWN, SHOWCASE], allow_credentials=True)
    app.add_middleware(ExtraOriginCorsMiddleware, origins=[SHOWCASE], routes=TRANSLATION_REVIEW_ROUTES)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/api/v1/private", headers={"Origin": SHOWCASE})
        assert "access-control-allow-origin" not in r.headers
        assert "access-control-allow-credentials" not in r.headers


def test_the_layer_must_sit_outside_cors_middleware() -> None:
    assert_outside_cors_middleware(_app())
    with pytest.raises(RuntimeError, match="OUTSIDE CORSMiddleware"):
        assert_outside_cors_middleware(_app(wrong_order=True))
    with pytest.raises(RuntimeError, match="not installed"):
        assert_outside_cors_middleware(_app(extra=False))


async def test_in_the_wrong_order_the_preflight_never_arrives() -> None:
    """Why the order matters: CORSMiddleware answers an unknown origin's preflight with 400."""
    async with AsyncClient(transport=ASGITransport(app=_app(wrong_order=True)), base_url="http://test") as ac:
        r = await ac.options(URL, headers=_preflight(SHOWCASE))
        assert r.status_code == 400


async def test_no_origins_and_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:  # pragma: no cover - never awaited
        return {}

    async def send(message: Message) -> None:  # pragma: no cover - never called
        pass

    off = ExtraOriginCorsMiddleware(inner, origins=[], routes=TRANSLATION_REVIEW_ROUTES)
    await off({"type": "http", "headers": [], "path": "/", "method": "GET"}, receive, send)
    on = ExtraOriginCorsMiddleware(inner, origins=[SHOWCASE], routes=TRANSLATION_REVIEW_ROUTES)
    await on({"type": "lifespan"}, receive, send)
    assert seen == ["http", "lifespan"]


def test_an_extra_origin_must_be_exact() -> None:
    for bad in ("*", "https://*.github.io", "https://eifi1.github.io/", "eifi1.github.io"):
        with pytest.raises(ValueError, match="exact"):
            ExtraOriginCorsMiddleware(_app(), origins=[bad], routes={})
    assert ExtraOriginRoute(methods=("get", "Put")).methods == ("GET", "PUT")


def test_the_extra_origins_are_exact_or_dropped() -> None:
    """keksdose ``:170`` (its ``settings.cors_extra_origin_list``)."""
    raw = (
        f" {SHOWCASE}/ , https://*.github.io, https://eifi1.github.io/ui-kit, ftp://x.y, "
        f"https://keksdose.app, http://localhost:4173, {SHOWCASE}"
        " https://a.example\thttps://b.example:8443"
        " https://user@c.example https://d.example?x=1 https://e.example#f"
    )
    assert parse_extra_origins(raw, own_origin="https://keksdose.app/") == [
        SHOWCASE,
        "http://localhost:4173",
        "https://a.example",
        "https://b.example:8443",
    ]
    assert parse_extra_origins("") == []
    assert parse_extra_origins("https://keksdose.app") == ["https://keksdose.app"]
