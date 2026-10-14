"""The translation review router on the fake, over HTTP (server-services-harmonization §6.2.6–§6.2.8).

A FastAPI app built as an app builds it — its own reviewer dependency,
``install_contract_error_handlers``, the router mounted at ``/api/v1`` — so H1's routes
are tested once, here.

No ``from __future__ import annotations``: the app's dependencies below are read by
FastAPI, as an app's would be.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.routing import APIRoute

from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.cors import TRANSLATION_REVIEW_ROUTES
from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.translation_review import (
    NO_KIT_REVIEW_ACCESS,
    NO_REVIEW_ACCESS,
    TranslationReviewer,
    TranslationReviewService,
)
from eifi1_server_kit.translation_review.router import TranslationReviewDeps, translation_review_router
from eifi1_server_kit.translation_review.testing import MemoryVerdict, check_round_trip
from tests.translation_review._layer2 import (
    ADMIN,
    FRENCH,
    LAWYER,
    LIMITED_TOKEN,
    LOCALES,
    NOBODY,
    NOW,
    TOKEN,
    SpyStore,
    row,
)

#: People by the header that signs them in; the tokens by a string id.
PEOPLE: dict[str, TranslationReviewer] = {
    "admin": ADMIN,
    "french": FRENCH,
    "lawyer": LAWYER,
    "nobody": NOBODY,
    "token": TOKEN,
    "limited-token": LIMITED_TOKEN,
}
BASE = "/api/v1/translations"


# --- the app's own pieces, as each app writes them --------------------------------------


async def get_current_user(x_reviewer: Annotated[str | None, Header()] = None) -> TranslationReviewer:
    if x_reviewer is None or x_reviewer not in PEOPLE:
        raise HTTPException(401, "Not authenticated")
    return PEOPLE[x_reviewer]


async def translation_reviewer(user: Annotated[TranslationReviewer, Depends(get_current_user)]) -> TranslationReviewer:
    """The app's view of its user; an app builds the grant with ``review_grant`` here."""
    return user


class World:
    """One app: its store, and a client signed in as anyone."""

    def __init__(self, **options: Any) -> None:
        self.store = SpyStore(
            row("fr", "common.save", by=FRENCH.id, minutes=2),
            row("fr", "kit.form.save", minutes=1),
            row("fr", "legal:imprint.title", by=LAWYER.id),
        )
        values: dict[str, Any] = {
            "reviewer": translation_reviewer,
            "store": lambda: self.store,
            "locale_codes": LOCALES,
            "now": lambda: NOW,
        }
        values.update(options)
        self.deps: TranslationReviewDeps[MemoryVerdict] = TranslationReviewDeps(**values)
        self.app = FastAPI()
        install_contract_error_handlers(self.app)
        self.router = translation_review_router(self.deps)
        self.app.include_router(self.router, prefix="/api/v1")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    @staticmethod
    def signed_in(who: str) -> dict[str, str]:
        return {"X-Reviewer": who}

    async def request(self, method: str, url: str, who: str, **options: Any) -> httpx.Response:
        self.store.calls.clear()
        response = await self.client.request(method, url, headers=self.signed_in(who), **options)
        assert self.store.one_write_last(), self.store.calls
        return response


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = World()
    async with built.client:
        yield built


def _item(locale: str, key: str, verdict: str = "APPROVED", **fields: Any) -> dict[str, Any]:
    return {"locale": locale, "key": key, "text": f"{key} in {locale}", "verdict": verdict, **fields}


# --- the three routes --------------------------------------------------------------------


async def test_the_list(world: World) -> None:
    listed = await world.request("GET", f"{BASE}/reviews", "french")
    assert listed.status_code == 200
    body = listed.json()
    assert body["locales"] == ["fr"] and body["areas"] is None
    assert [review["key"] for review in body["reviews"]] == ["common.save", "kit.form.save", "legal:imprint.title"]
    first = body["reviews"][0]
    assert (first["reviewer_name"], first["reviewer_first_name"], first["reviewer_last_name"]) == (
        "Odile Lecteur",
        "Odile",
        "Lecteur",
    )
    assert world.store.calls == ["verdicts"]


async def test_a_reviewer_limited_to_legal_lists_kastlans_namespaced_key(world: World) -> None:
    listed = (await world.request("GET", f"{BASE}/reviews", "lawyer")).json()
    assert listed["areas"] == ["legal"] and [review["key"] for review in listed["reviews"]] == ["legal:imprint.title"]
    assert listed["reviews"][0]["reviewer_name"] == "Kovács Lajos", "the reader's order: hu"


async def test_a_token_lists_the_kit_only(world: World) -> None:
    listed = (await world.request("GET", f"{BASE}/reviews", "token")).json()
    assert listed["areas"] == ["kit"] and [review["key"] for review in listed["reviews"]] == ["kit.form.save"]


async def test_a_save(world: World) -> None:
    saved = await world.request(
        "PUT",
        f"{BASE}/reviews",
        "french",
        json={"items": [_item("fr", "common.cancel", "NEEDS_CHANGE", note=" Trop long ", suggestion="Annuler")]},
    )
    assert saved.status_code == 200
    (review,) = saved.json()["reviews"]
    assert review["verdict"] == "NEEDS_CHANGE" and review["note"] == "Trop long" and review["suggestion"] == "Annuler"
    assert review["reviewer_name"] == "Odile Lecteur" and review["reviewed_at"] == "2026-10-14T09:30:00Z"
    assert world.store.calls == ["put"]


async def test_a_clear(world: World) -> None:
    body = {"items": [{"locale": "fr", "key": "common.save"}, {"locale": "fr", "key": "never.reviewed"}]}
    cleared = await world.request("POST", f"{BASE}/reviews/clear", "french", json=body)
    assert cleared.status_code == 200 and cleared.json() == {"cleared": 1}
    assert world.store.calls == ["clear"]


# --- the refusals ------------------------------------------------------------------------


async def test_the_list_refuses_a_caller_granted_nothing(world: World) -> None:
    """A member, or a demo (``review_grant(…, is_demo=True)``): keksdose's and kastlan's 403."""
    refused = await world.request("GET", f"{BASE}/reviews", "nobody")
    assert refused.status_code == 403 and refused.json() == {"detail": NO_REVIEW_ACCESS}
    assert world.store.calls == []


async def test_a_token_on_a_grant_with_an_area_limit_is_refused_its_list(world: World) -> None:
    """T2 (§6.2.2): keksdose refuses such a token at mint; the kit's ``scope`` refuses its
    list for every app. Its writes are held to its areas by the grant."""
    refused = await world.request("GET", f"{BASE}/reviews", "limited-token")
    assert refused.status_code == 403 and refused.json() == {"detail": NO_KIT_REVIEW_ACCESS}
    assert world.store.calls == []
    outside = await world.request(
        "PUT", f"{BASE}/reviews", "limited-token", json={"items": [_item("fr", "kit.form.save")]}
    )
    assert outside.status_code == 403
    assert outside.json() == {"detail": "Not granted for 'kit.form.save'; limited to legal"}
    assert world.store.calls == []


async def test_the_refusals_in_their_order(world: World) -> None:
    """§6.2.8: the body (422), a token's reach (403), the locale (422), the grant (403)."""
    token_first = await world.request("PUT", f"{BASE}/reviews", "token", json={"items": [_item("xx", "common.save")]})
    assert token_first.status_code == 403
    assert token_first.json() == {"detail": "A review token reaches the kit's wording only"}
    unknown = await world.request("PUT", f"{BASE}/reviews", "french", json={"items": [_item("xx", "common.save")]})
    assert unknown.status_code == 422 and unknown.json() == {"detail": "Unknown locale 'xx'"}
    outside = await world.request("PUT", f"{BASE}/reviews", "french", json={"items": [_item("it", "common.save")]})
    assert outside.status_code == 403 and outside.json() == {"detail": "Not granted for locale 'it'"}
    area = await world.request(
        "POST", f"{BASE}/reviews/clear", "lawyer", json={"items": [{"locale": "fr", "key": "common.save"}]}
    )
    assert area.status_code == 403 and area.json() == {"detail": "Not granted for 'common.save'; limited to legal"}
    assert world.store.calls == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/reviews", {"items": [_item("fr", "common.save", "NEEDS_CHANGE", sugestion="Enregistrer")]}),
        ("PUT", "/reviews", {"items": [_item("fr", "common.save")], "dry_run": True}),
        ("POST", "/reviews/clear", {"items": [{"locale": "fr", "key": "common.save", "verdict": "APPROVED"}]}),
        ("POST", "/reviews/clear", {"items": [{"locale": "fr", "key": "common.save"}], "all": True}),
    ],
)
async def test_a_misspelt_field_is_a_422(world: World, method: str, path: str, body: dict[str, Any]) -> None:
    """T5 (KS Q1): an unknown field in a ``PUT`` item, a ``PUT`` body, a clear item and a
    clear body is a 422, and nothing is written — never a verdict saved without its
    suggestion. The body is checked before the grant: even a caller granted nothing gets
    the 422."""
    for who in ("french", "nobody"):
        refused = await world.request(method, f"{BASE}{path}", who, json=body)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["type"] == "extra_forbidden"
    assert world.store.calls == []
    assert world.store.rows["fr", "common.save"].suggestion is None


async def test_the_reviewer_dependency_is_the_apps(world: World) -> None:
    """kk's 401 for a dead token stays in its own dependency, before the kit's flow."""
    anonymous = await world.client.get(f"{BASE}/reviews")
    assert anonymous.status_code == 401 and anonymous.json() == {"detail": "Not authenticated"}
    assert world.store.calls == []


# --- the OpenAPI document and the route table ---------------------------------------------


def test_keksdoses_operation_ids_stay() -> None:
    """kk ``frontend/src/shared/api/endpoints.ts:2252-2261``; ka the same names."""
    document = World().app.openapi()
    operations = {
        (path, method): operation["operationId"]
        for path, methods in document["paths"].items()
        for method, operation in methods.items()
    }
    assert operations == {
        (f"{BASE}/reviews", "get"): "list_translation_reviews_api_v1_translations_reviews_get",
        (f"{BASE}/reviews", "put"): "save_translation_reviews_api_v1_translations_reviews_put",
        (f"{BASE}/reviews/clear", "post"): "clear_translation_reviews_api_v1_translations_reviews_clear_post",
    }
    assert document["paths"][f"{BASE}/reviews"]["get"]["tags"] == ["translations"]
    components = document["components"]["schemas"]
    assert {"TranslationReviewBatch", "TranslationReviewClear", "TranslationReviewsResponse"} <= set(components)
    assert components["TranslationReviewWrite"]["additionalProperties"] is False


def test_the_paths_are_the_cors_review_routes() -> None:
    """Mounted at ``/api/v1``, the paths and methods are ``TRANSLATION_REVIEW_ROUTES``'s, which
    keksdose opens to the showcase's origin (``main.py:184-196``)."""
    served: dict[str, set[str]] = {}
    for route in World().router.routes:
        assert isinstance(route, APIRoute)
        served.setdefault(f"/api/v1{route.path}", set()).update(route.methods or ())
    opened = {path: set(rule.methods) for path, rule in TRANSLATION_REVIEW_ROUTES.items() if "/translations/" in path}
    assert served == opened


def test_the_routes_are_in_h1s_order_with_no_guard() -> None:
    """§6.2.6: no route guard — the grant decides. Each route depends on the app's reviewer
    (and through it on its ``get_current_user``, which Kurvenschmiede's walk looks for)."""
    routes = [route for route in World().router.routes if isinstance(route, APIRoute)]
    assert [(sorted(route.methods or ())[0], route.path) for route in routes] == [
        ("GET", "/translations/reviews"),
        ("PUT", "/translations/reviews"),
        ("POST", "/translations/reviews/clear"),
    ]
    for route in routes:
        assert route.dependencies == []
        names: set[str] = set()
        stack = [route.dependant]
        while stack:
            dependant = stack.pop()
            for sub in dependant.dependencies:
                names.add(getattr(sub.call, "__qualname__", repr(sub.call)))
                stack.append(sub)
        assert {"translation_reviewer", "get_current_user"} <= names, route.path


# --- the factory -------------------------------------------------------------------------


def test_the_default_clock() -> None:
    deps = TranslationReviewDeps[MemoryVerdict](reviewer=translation_reviewer, store=SpyStore, locale_codes=LOCALES)
    assert deps.now is utc_now


async def test_the_app_chooses_the_prefix_and_the_tags() -> None:
    deps = World().deps
    app = FastAPI()
    install_contract_error_handlers(app)
    app.include_router(translation_review_router(deps, prefix="/wording", tags=("wording", "kit")), prefix="/api/v2")
    headers = World.signed_in("admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.put("/api/v2/wording/reviews", json={"items": [_item("fr", "kit.a")]}, headers=headers)
        assert saved.status_code == 200
        cleared = await client.post(
            "/api/v2/wording/reviews/clear", json={"items": [{"locale": "fr", "key": "kit.a"}]}, headers=headers
        )
        assert cleared.json() == {"cleared": 1}
    assert app.openapi()["paths"]["/api/v2/wording/reviews"]["put"]["tags"] == ["wording", "kit"]


async def test_an_apps_own_route_after_the_kits_takes_the_same_403() -> None:
    """kastlan's ``GET /translations/documents`` (§6.2.7): its own router after the kit's, on
    the same reviewer dependency, calling ``scope`` before it answers."""
    world = World()
    own = APIRouter(prefix="/translations", tags=["translations"])

    @own.get("/documents")
    async def list_document_texts(
        reviewer: Annotated[TranslationReviewer, Depends(translation_reviewer)],
    ) -> dict[str, list[str]]:
        TranslationReviewService(world.store, locale_codes=LOCALES).scope(reviewer)
        return {"texts": ["doc_text:invoice.footer"]}

    world.app.include_router(own, prefix="/api/v1")
    async with world.client:
        assert (await world.request("GET", f"{BASE}/documents", "admin")).status_code == 200
        refused = await world.request("GET", f"{BASE}/documents", "nobody")
        assert refused.status_code == 403 and refused.json() == {"detail": NO_REVIEW_ACCESS}


@pytest.mark.parametrize("who", ["admin", "token"])
async def test_check_round_trip_passes_on_the_fake(who: str) -> None:
    world = World()
    async with world.client:
        await check_round_trip(world.client, headers=World.signed_in(who), locale="fr")
    assert world.store.writes == ["put", "clear"]
