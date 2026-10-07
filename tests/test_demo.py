"""The demo (landing-demo contract §5–§7): settings, the gate, the refusals, the user."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route

from eifi1_server_kit.auth import Budget, TokenResponse, UserResponse
from eifi1_server_kit.demo import (
    DEMO_ERROR_DETAIL,
    DEMO_ERROR_STATUS,
    DEMO_FIRST_NAME,
    READ_METHODS,
    REAP_PER_START,
    DemoAdmission,
    DemoError,
    DemoErrorCode,
    DemoGate,
    DemoSettings,
    demo_address,
    demo_expires_at,
    demo_password,
    demo_write_allowed,
    is_demo_address,
    refuse_demo,
    stale_cutoff,
)
from eifi1_server_kit.errors import CONTRACT_ERRORS, install_contract_error_handlers
from eifi1_server_kit.limiter import client_ip

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ON = DemoSettings(demo_session_enabled=True)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@dataclass
class _Store:
    """The app's side of the gate: demo users by creation time, and the demo data."""

    created: list[datetime] = field(default_factory=list)
    ready: bool = True
    calls: list[str] = field(default_factory=list)
    #: Users the reap fails to delete (a row still points at them).
    stuck: int = 0

    async def reap(self, *, cutoff: datetime, limit: int) -> int:
        self.calls.append(f"reap<{cutoff.isoformat()}|{limit}")
        stale = sorted(moment for moment in self.created if moment < cutoff)[: max(limit - self.stuck, 0)]
        for moment in stale:
            self.created.remove(moment)
        return len(stale)

    async def count_live(self, *, cutoff: datetime) -> int:
        self.calls.append("count")
        return sum(1 for moment in self.created if moment >= cutoff)

    async def is_ready(self) -> bool:
        self.calls.append("ready")
        return self.ready


async def _admit(gate: DemoGate, store: _Store, ip: str = "198.51.100.4", now: datetime = NOW) -> DemoAdmission:
    return await gate.admit(ip, reap=store.reap, count_live=store.count_live, is_ready=store.is_ready, now=now)


# ── §6.1: the settings ──────────────────────────────────────────────────────


def test_the_settings_and_their_defaults() -> None:
    settings = DemoSettings()
    assert settings.model_dump() == {
        "demo_session_enabled": False,
        "demo_user_max_age_hours": 24,
        "demo_session_max_live": 500,
        "demo_session_rate_max": 5,
        "demo_session_rate_window_seconds": 3600,
    }
    assert "demo_session_token_minutes" not in DemoSettings.model_fields, "gone (§5.3)"
    assert settings.demo_user_max_age == timedelta(hours=24)
    assert settings.demo_session_rate == Budget(5, 3600)
    with pytest.raises(ValidationError):
        DemoSettings(demo_user_max_age_hours=0)
    with pytest.raises(ValidationError):
        DemoSettings(demo_session_max_live=-1)


def test_an_apps_settings_inherit_them_or_are_read_from_attributes() -> None:
    class KeksdoseSettings(DemoSettings):
        demo_session_enabled: bool = True
        assistant_demo_daily_limit: int = 5

    keksdose = KeksdoseSettings()
    assert keksdose.demo_session_enabled and keksdose.demo_session_max_live == 500
    assert DemoGate(keksdose).settings is keksdose

    @dataclass
    class KastlanSettings:
        demo_session_enabled: bool = True
        demo_company_slug: str = "demo"
        demo_user_max_age_hours: int = 12

    view = DemoSettings.model_validate(KastlanSettings(), from_attributes=True)
    assert view.demo_session_enabled and view.demo_user_max_age == timedelta(hours=12)


def test_one_lifetime_for_the_account_and_its_token() -> None:
    created = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
    assert demo_expires_at(created, ON) == datetime(2026, 10, 8, 9, 30, tzinfo=UTC)
    assert stale_cutoff(NOW, ON) == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    # The cutoff is the expiry seen from the other side; naive stays naive.
    assert demo_expires_at(stale_cutoff(NOW, ON), ON) == NOW
    naive = datetime(2026, 10, 7, 12, 0)
    assert stale_cutoff(naive, ON).tzinfo is None and demo_expires_at(naive, ON).tzinfo is None
    short = DemoSettings(demo_user_max_age_hours=1)
    assert demo_expires_at(created, short) - created == timedelta(hours=1)


# ── §5.1: the gate, in kastlan's order ──────────────────────────────────────


async def test_a_start_reaps_counts_and_checks_readiness_in_order() -> None:
    store = _Store(created=[NOW - timedelta(hours=30), NOW - timedelta(hours=2)])
    admission = await _admit(DemoGate(ON), store)
    cutoff = NOW - timedelta(hours=24)
    assert admission == DemoAdmission(now=NOW, cutoff=cutoff, reaped=1)
    assert store.calls == [f"reap<{cutoff.isoformat()}|{REAP_PER_START}", "count", "ready"]
    assert store.created == [NOW - timedelta(hours=2)]


async def test_a_disabled_demo_is_not_there_and_touches_nothing() -> None:
    store = _Store()
    with pytest.raises(DemoError) as caught:
        await _admit(DemoGate(DemoSettings()), store)
    assert (caught.value.code, caught.value.status_code) == (DemoErrorCode.DEMO_DISABLED, 404)
    assert store.calls == [], "no window charged, no database work"


async def test_the_per_ip_window_comes_before_any_database_work() -> None:
    clock = _Clock()
    gate = DemoGate(ON, clock=clock)
    store = _Store()
    for _ in range(5):
        await _admit(gate, store)
    store.calls.clear()
    with pytest.raises(DemoError) as caught:
        await _admit(gate, store)
    error = caught.value
    assert (error.code, error.status_code) == (DemoErrorCode.DEMO_RATE_LIMITED, 429)
    assert error.retry_after is not None and error.headers == {"Retry-After": "3601"}
    assert store.calls == []
    # Another network is unaffected; the window frees up after an hour (§2.7: a new demo
    # may start after one ends).
    await _admit(gate, store, ip="203.0.113.9")
    clock.t += 3601
    await _admit(gate, store)
    gate.reset()
    for _ in range(5):
        await _admit(gate, store)


async def test_the_window_counts_from_the_right_so_a_spoofed_header_buys_nothing() -> None:
    gate = DemoGate(ON, clock=_Clock())
    store = _Store()
    for n in range(5):
        await _admit(gate, store, ip=client_ip({"x-forwarded-for": f"10.9.9.{n}, 198.51.100.4"}, None, trusted_hops=1))
    with pytest.raises(DemoError, match="Too many demos"):
        await _admit(gate, store, ip=client_ip({"x-forwarded-for": "10.9.9.99, 198.51.100.4"}, None, trusted_hops=1))


async def test_a_rate_max_of_zero_turns_the_window_off() -> None:
    gate = DemoGate(DemoSettings(demo_session_enabled=True, demo_session_rate_max=0))
    store = _Store()
    for _ in range(20):
        await _admit(gate, store)


async def test_the_cap_counts_live_users_only() -> None:
    """keksdose's review: a user the reap can't delete must not hold a place for ever."""
    settings = DemoSettings(demo_session_enabled=True, demo_session_max_live=2, demo_session_rate_max=0)
    gate = DemoGate(settings)
    stale = [NOW - timedelta(hours=25 + n) for n in range(3)]
    store = _Store(created=[*stale, NOW - timedelta(minutes=5)], stuck=REAP_PER_START)
    admission = await _admit(gate, store)
    assert admission.reaped == 0 and len(store.created) == 4, "nothing could be reaped"
    store.created.append(NOW - timedelta(minutes=1))
    with pytest.raises(DemoError) as caught:
        await _admit(gate, store)
    error = caught.value
    assert (error.code, error.status_code, error.headers) == (DemoErrorCode.DEMO_CAPACITY, 429, {})
    assert store.calls[-1] == "count", "readiness is not asked once the cap refuses"


async def test_a_start_reaps_at_most_the_twenty_oldest() -> None:
    store = _Store(created=[NOW - timedelta(hours=25, minutes=n) for n in range(30)])
    admission = await _admit(DemoGate(ON), store)
    assert admission.reaped == 20 and len(store.created) == 10
    # The oldest went first: the ten youngest of the stale ones are left for the next start.
    assert sorted(store.created) == [NOW - timedelta(hours=25, minutes=n) for n in range(9, -1, -1)]
    small = DemoGate(ON, reap_limit=5)
    assert (await _admit(small, store)).reaped == 5
    with pytest.raises(ValueError, match="at least one"):
        DemoGate(ON, reap_limit=0)


async def test_missing_demo_data_is_a_503_after_the_reap() -> None:
    store = _Store(created=[NOW - timedelta(hours=30)], ready=False)
    with pytest.raises(DemoError) as caught:
        await _admit(DemoGate(ON), store)
    assert (caught.value.code, caught.value.status_code) == (DemoErrorCode.DEMO_NOT_READY, 503)
    assert store.created == [], "the reap ran anyway (§5.1 step 3 runs on every start)"


async def test_now_defaults_to_the_current_time_in_utc() -> None:
    store = _Store()
    gate = DemoGate(ON)
    before = datetime.now(UTC)
    admission = await gate.admit("198.51.100.4", reap=store.reap, count_live=store.count_live, is_ready=store.is_ready)
    assert admission.now.tzinfo is UTC and before <= admission.now <= datetime.now(UTC)
    assert admission.cutoff == admission.now - timedelta(hours=24)


# ── §6.2: the codes ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (DemoErrorCode.DEMO_DISABLED, 404),
        (DemoErrorCode.DEMO_RATE_LIMITED, 429),
        (DemoErrorCode.DEMO_CAPACITY, 429),
        (DemoErrorCode.DEMO_NOT_READY, 503),
        (DemoErrorCode.DEMO_READ_ONLY, 403),
        (DemoErrorCode.DEMO_REFUSED, 403),
    ],
)
def test_each_code_has_its_status_and_detail(code: DemoErrorCode, status: int) -> None:
    error = DemoError(code)
    assert isinstance(error, ValueError) and error.status_code == status == DEMO_ERROR_STATUS[code]
    assert str(error) == DEMO_ERROR_DETAIL[code] and str(code) == code.value
    assert DemoError(code.value, "said otherwise").code is code  # type: ignore[arg-type]


async def test_the_refusals_are_answered_with_code_and_retry_after() -> None:
    assert DemoError in CONTRACT_ERRORS

    async def start(_request: Request) -> None:
        raise DemoError(DemoErrorCode.DEMO_RATE_LIMITED, retry_after=1799.2)

    async def capacity(_request: Request) -> None:
        raise DemoError(DemoErrorCode.DEMO_CAPACITY)

    app = Starlette(
        routes=[Route("/auth/demo-session", start, methods=["POST"]), Route("/full", capacity, methods=["POST"])]
    )
    install_contract_error_handlers(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        limited = await client.post("/auth/demo-session")
        full = await client.post("/full")
    assert limited.status_code == 429 and limited.headers["retry-after"] == "1800"
    assert limited.json() == {"detail": "Too many demos from this network", "code": "demo_rate_limited"}
    assert full.status_code == 429 and "retry-after" not in full.headers
    assert full.json() == {"detail": "The demo is full right now", "code": "demo_capacity"}


def test_refuse_demo_names_what_a_demo_never_may() -> None:
    refuse_demo(False, "passkeys")
    with pytest.raises(DemoError) as caught:
        refuse_demo(True, "the account export")
    assert (caught.value.code, caught.value.status_code) == (DemoErrorCode.DEMO_REFUSED, 403)
    assert str(caught.value) == "Not possible for a demo account: the account export"


# ── §6.3: model R, read-only ────────────────────────────────────────────────


def test_reads_are_always_allowed_and_writes_never_by_default() -> None:
    assert frozenset({"GET", "HEAD", "OPTIONS"}) == READ_METHODS
    for method in ("GET", "head", " options "):
        assert demo_write_allowed(method, "/api/v1/budgets")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not demo_write_allowed(method, "/api/v1/auth/logout"), "empty by default (§10.4)"


def test_the_apps_allow_list() -> None:
    kastlan = frozenset({"POST /api/v1/auth/logout"})
    assert demo_write_allowed("post", "/api/v1/auth/logout", kastlan)
    assert not demo_write_allowed("DELETE", "/api/v1/auth/logout", kastlan)
    assert not demo_write_allowed("POST", "/api/v1/auth/logout/all", kastlan)
    assert not demo_write_allowed("POST", "/api/v1/auth/logout/", kastlan)

    keksdose = ("POST /assistant/ask", "DELETE /assistant/threads/{id}")
    assert demo_write_allowed("POST", "/assistant/ask", keksdose)
    assert demo_write_allowed("DELETE", "/assistant/threads/42", keksdose)
    assert demo_write_allowed("DELETE", "/assistant/threads/{thread_id}", keksdose), "a route template matches too"
    assert not demo_write_allowed("DELETE", "/assistant/threads/42/messages", keksdose), "one segment only"
    assert not demo_write_allowed("DELETE", "/assistant/threads/", keksdose)
    assert not demo_write_allowed("POST", "/assistant/askx", keksdose)
    # Literal, not a regex: a dot in a path is a dot.
    assert not demo_write_allowed("POST", "/v1Xexport", ["POST /v1.export"])


@pytest.mark.parametrize("entry", ["/auth/logout", "POST auth/logout", "POST /a b", "PO5T /x", ""])
def test_a_malformed_allow_entry_is_a_programming_error(entry: str) -> None:
    with pytest.raises(ValueError, match="METHOD /path"):
        demo_write_allowed("POST", "/x", [entry])


# ── §5.1: the user ──────────────────────────────────────────────────────────


def test_a_demo_address_lives_on_a_subdomain_without_mail() -> None:
    address = demo_address(" Keksdose.APP ")
    assert re.fullmatch(r"demo\+[0-9a-f]{32}@demo\.keksdose\.app", address)
    assert demo_address("keksdose.app") != address, "128 random bits each"
    assert is_demo_address(address) and is_demo_address(address.upper())
    assert is_demo_address(address, "keksdose.app") and not is_demo_address(address, "kastlan.app")
    for domain in ("", "localhost", "keksdose..app", "-x.app", "ada@example.com", "a b.app"):
        with pytest.raises(ValueError, match="own domain"):
            demo_address(domain)


@pytest.mark.parametrize(
    "email",
    [
        "ada@example.com",
        "demo@demo.keksdose.app",
        "demo+123@demo.keksdose.app",
        "demo+" + "g" * 32 + "@demo.keksdose.app",
        "demo+" + "0" * 32 + "@keksdose.app",
        "ada+" + "0" * 32 + "@demo.keksdose.app",
    ],
)
def test_other_addresses_are_not_demo_addresses(email: str) -> None:
    assert not is_demo_address(email)


def test_the_password_nobody_holds() -> None:
    password = demo_password()
    assert len(password) == 43 and len(password.encode()) <= 72 and password != demo_password()


def test_the_demo_user_and_its_session_on_the_wire() -> None:
    """§5.1, §5.3: first name "Demo", no last name, never asked to complete it; no refresh
    token, and both instants the same."""

    class AppUser(UserResponse):
        is_demo: bool = False

        def name_completion_exempt(self) -> bool:
            return self.is_demo

    created = NOW
    ends = demo_expires_at(created, ON)
    user = AppUser(
        id=7,
        email=demo_address("kastlan.app"),
        first_name=DEMO_FIRST_NAME,
        last_name="",
        locale="de-CH",
        role="ADMIN",
        is_active=True,
        email_verified=True,
        created_at=created,
        is_demo=True,
        demo_expires_at=ends,
    )
    session = TokenResponse[AppUser](access_token="jwt", user=user, expires_at=ends).model_dump(mode="json")
    assert session["refresh_token"] is None and session["expires_at"] == "2026-10-08T12:00:00Z"
    assert session["user"]["demo_expires_at"] == session["expires_at"]
    assert session["user"]["name_incomplete"] is False and session["user"]["display_name"] == "Demo"
