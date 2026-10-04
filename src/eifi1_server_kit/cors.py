"""CORS for a few EXTRA origins — exact origins, a few paths, and never with credentials.

keksdose ``backend/keksdose/infrastructure/extra_origin_cors.py`` (its showcase CORS,
Marcel 2026-10-03), with the paths and the per-path methods / headers as parameters.

An app's own origin is served by Starlette's ``CORSMiddleware`` with
``allow_credentials=True`` — the SPA's refresh cookie rides on it. That middleware has ONE
policy for every origin it allows, so adding another origin there would hand that origin
the cookie too (and ``https://eifi1.github.io`` is every GitHub Pages site of the account).
So the extra origins get their own, smaller answer:

* **no credentials** — never ``Access-Control-Allow-Credentials``: they authenticate with
  a Bearer token (keksdose's review token) and nothing else;
* **listed paths only** — :class:`ExtraOriginRoute` per path, each with its own methods
  and request headers. Everywhere else such an origin gets no CORS headers at all, so the
  browser refuses to hand the response over;
* **preflight answered here** — 204 for an allowed path and method, 403 otherwise.
  ``CORSMiddleware`` answers a preflight from an origin it does not know with 400, so this
  layer MUST sit OUTSIDE it: add it AFTER ``CORSMiddleware`` (Starlette's
  ``add_middleware`` wraps outward), then call :func:`assert_outside_cors_middleware`;
* **the credentials leak stripped** — ``CORSMiddleware`` adds its "simple" headers,
  ``Allow-Credentials: true`` among them, to EVERY response that carries an ``Origin``,
  allowed or not; it only withholds ``Allow-Origin``. Beside this layer's
  ``Allow-Origin`` that would let a page on the extra origin read a cookie-carrying
  response. Measured in the test that pins it; removed from every response to an extra
  origin.

The endpoints still authorise every call themselves; this only decides what a browser on
that origin may read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Ten minutes (keksdose): a showcase calls the same few paths over and over.
DEFAULT_MAX_AGE = 600

#: What an extra origin must never see — on any response, on any path.
_NEVER = ("Access-Control-Allow-Credentials", "Access-Control-Expose-Headers")


@dataclass(frozen=True, slots=True)
class ExtraOriginRoute:
    """What an extra origin may send to one path."""

    methods: tuple[str, ...] = ("GET",)
    headers: tuple[str, ...] = ("Authorization", "Content-Type")

    def __post_init__(self) -> None:
        object.__setattr__(self, "methods", tuple(m.upper() for m in self.methods))


#: The kit showcase's routes (keksdose ``EXTRA_ORIGIN_PATHS``, ``extra_origin_cors.py:29-37``):
#: the public health ping it wakes a scaled-to-zero backend with, and the translation
#: review list / write / clear. keksdose allowed GET, PUT and POST on all three; here each
#: path has the methods the showcase actually sends to it.
TRANSLATION_REVIEW_ROUTES: Mapping[str, ExtraOriginRoute] = MappingProxyType(
    {
        "/api/v1/health": ExtraOriginRoute(methods=("GET",)),
        "/api/v1/translations/reviews": ExtraOriginRoute(methods=("GET", "PUT")),
        "/api/v1/translations/reviews/clear": ExtraOriginRoute(methods=("POST",)),
    }
)


class ExtraOriginCorsMiddleware:
    """Narrow, credential-free CORS for ``origins`` on ``routes`` (see the module docstring)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        origins: Iterable[str],
        routes: Mapping[str, ExtraOriginRoute],
        max_age: int = DEFAULT_MAX_AGE,
    ) -> None:
        self.app = app
        self.origins = frozenset(origins)
        for origin in self.origins:
            if "*" in origin or origin != origin.rstrip("/") or not urlsplit(origin).hostname:
                raise ValueError(f"an extra origin must be exact (scheme://host[:port]): {origin!r}")
        self.routes = dict(routes)
        self.max_age = str(max_age)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.origins:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None or origin not in self.origins:
            await self.app(scope, receive, send)
            return
        route = self.routes.get(scope["path"])

        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            response: Response
            if route is not None and headers["access-control-request-method"].upper() in route.methods:
                response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": ", ".join(route.methods),
                        "Access-Control-Allow-Headers": ", ".join(route.headers),
                        "Access-Control-Max-Age": self.max_age,
                        "Vary": "Origin",
                    },
                )
            else:
                # No CORS headers: the browser refuses the real request.
                response = PlainTextResponse("Disallowed CORS request", status_code=403)
            await response(scope, receive, send)
            return

        async def send_narrowed(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                for name in _NEVER:
                    if name in out:
                        del out[name]
                if route is not None:
                    out["Access-Control-Allow-Origin"] = origin
                    out.add_vary_header("Origin")
                elif "Access-Control-Allow-Origin" in out:
                    # Off the listed paths an extra origin reads nothing, even if the main
                    # layer was (mis)configured to allow it too.
                    del out["Access-Control-Allow-Origin"]
            await send(message)

        await self.app(scope, receive, send_narrowed)


def assert_outside_cors_middleware(app: Starlette) -> None:
    """Raise :class:`RuntimeError` unless :class:`ExtraOriginCorsMiddleware` wraps every
    ``CORSMiddleware`` of ``app`` — i.e. was added AFTER it.

    Starlette's ``add_middleware`` inserts at the front of ``app.user_middleware`` and the
    front is the outermost layer, so this layer's index must be lower than every
    ``CORSMiddleware``'s. Call it once at the end of ``create_app`` (or in a test).
    """
    classes: list[object] = [entry.cls for entry in app.user_middleware]
    if ExtraOriginCorsMiddleware not in classes:
        raise RuntimeError("ExtraOriginCorsMiddleware is not installed on this app")
    ours = classes.index(ExtraOriginCorsMiddleware)
    inner = [index for index, cls in enumerate(classes) if cls is CORSMiddleware]
    if any(index < ours for index in inner):
        raise RuntimeError(
            "ExtraOriginCorsMiddleware must sit OUTSIDE CORSMiddleware: add it after CORSMiddleware, "
            "or CORSMiddleware answers the extra origin's preflight with 400 first"
        )


def parse_extra_origins(raw: str, *, own_origin: str = "") -> list[str]:
    """A setting's origins as exact origins, in order, each once.

    keksdose ``infrastructure/settings.py:631`` ``cors_extra_origin_list``: separated by
    commas or whitespace (whitespace is the safe form inside Cloud Build's comma-delimited
    ``--set-env-vars``); a trailing ``/`` is tolerated; anything that is not
    ``http(s)://host[:port]`` — a wildcard, a path, a query, a fragment, userinfo — is
    DROPPED, as is the app's own origin, so a typo narrows the list instead of widening it.
    """
    own = own_origin.rstrip("/")
    out: list[str] = []
    for item in re.split(r"[,\s]+", raw):
        value = item.strip().rstrip("/")
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or "*" in value
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or (own and value == own)
        ):
            continue
        if value not in out:
            out.append(value)
    return out
