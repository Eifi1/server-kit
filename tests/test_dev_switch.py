"""The dev switch (top-bar §2.7, §6.1–§6.3): the gate needs the flag AND the app's proof of a
local run AND not production; the flag in production stops the start; the route, mounted
as the README shows, is absent unless the gate holds, and takes the app's roles and plans
only."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from functools import partial
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request

from eifi1_server_kit.dev_switch import (
    DEV_SWITCH_ERROR_DETAIL,
    DEV_SWITCH_ERROR_STATUS,
    DEV_SWITCH_LOG_TAG,
    DEV_SWITCH_PATH,
    LOCAL_DATABASE_PORTS,
    LOCAL_SOCKET_DIRS,
    DevSwitchAnswer,
    DevSwitchError,
    DevSwitchErrorCode,
    DevSwitchRequest,
    DevSwitchSettings,
    DevSwitchUnsafeError,
    assert_dev_switch_safe,
    database_is_local,
    dev_switch_enabled,
    effective_today,
    require_dev_switch,
    validate_dev_switch,
)
from eifi1_server_kit.errors import CONTRACT_ERRORS, install_contract_error_handlers

ROLES = ("ADMIN", "MEMBER", "CUSTOMER")
PLANS = ("free", "pro")


class _Settings(BaseSettings, DevSwitchSettings):
    """An app's settings as the README shows them."""

    model_config = SettingsConfigDict(env_prefix="GARDEN_")


# --- the setting -------------------------------------------------------------------------


def test_the_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARDEN_DEV_SWITCH_ENABLED", raising=False)
    assert _Settings().dev_switch_enabled is False
    assert DevSwitchSettings().dev_switch_enabled is False


def test_the_flag_is_read_from_the_apps_own_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARDEN_DEV_SWITCH_ENABLED", "true")
    assert _Settings().dev_switch_enabled is True
    monkeypatch.setenv("GARDEN_DEV_SWITCH_ENABLED", "false")
    assert _Settings().dev_switch_enabled is False


# --- the gate ----------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("local_run", [True, False])
@pytest.mark.parametrize("production", [True, False])
def test_the_gate_holds_only_with_all_three(flag: bool, local_run: bool, production: bool) -> None:
    expected = flag and local_run and not production
    assert dev_switch_enabled(flag=flag, local_run=local_run, production=production) is expected
    assert dev_switch_enabled(flag=flag, local_run=lambda: local_run, production=production) is expected


def test_the_proof_is_asked_only_when_the_flag_is_on_outside_production() -> None:
    asked: list[bool] = []

    def proof() -> bool:
        asked.append(True)
        return True

    assert dev_switch_enabled(flag=False, local_run=proof, production=False) is False
    assert dev_switch_enabled(flag=True, local_run=proof, production=True) is False
    assert asked == []
    assert dev_switch_enabled(flag=True, local_run=proof, production=False) is True
    assert asked == [True]


@pytest.mark.parametrize(
    ("flag", "local_run", "production"),
    [
        ("true", True, False),  # a raw environment value, not a parsed bool
        (1, True, False),
        (True, "yes", False),
        (True, 1, False),
        (True, True, None),  # an unknown production signal counts as production
        (True, True, ""),
        (True, True, "development"),  # an environment name is never the signal
    ],
)
def test_the_gate_fails_closed_on_anything_but_real_booleans(
    flag: object, local_run: object, production: object
) -> None:
    assert dev_switch_enabled(flag=flag, local_run=local_run, production=production) is False  # type: ignore[arg-type]
    assert dev_switch_enabled(flag=flag, local_run=lambda: local_run, production=production) is False  # type: ignore[arg-type, return-value]


def test_the_flag_in_production_stops_the_start() -> None:
    with pytest.raises(DevSwitchUnsafeError, match="DEV_SWITCH_ENABLED"):
        assert_dev_switch_safe(flag=True, production=True)
    with pytest.raises(DevSwitchUnsafeError):
        assert_dev_switch_safe(flag=True, production=None)  # type: ignore[arg-type]
    assert isinstance(DevSwitchUnsafeError(), RuntimeError)
    assert DevSwitchUnsafeError not in CONTRACT_ERRORS  # raised at start, never answered


@pytest.mark.parametrize(("flag", "production"), [(False, True), (False, False), (True, False)])
def test_every_other_combination_starts(flag: bool, production: bool) -> None:
    assert_dev_switch_safe(flag=flag, production=production)


# --- the body ----------------------------------------------------------------------------


def test_the_body_names_a_role_a_plan_or_both() -> None:
    assert DevSwitchRequest.model_validate({"role": "ADMIN"}) == DevSwitchRequest(role="ADMIN")
    assert DevSwitchRequest.model_validate({"plan": " pro "}).plan == "pro"
    both = DevSwitchRequest.model_validate({"role": "MEMBER", "plan": "free"})
    assert (both.role, both.plan) == ("MEMBER", "free")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": None, "plan": None},
        {"role": ""},
        {"plan": "   "},
        {"role": "x" * 65},
        {"role": "ADMIN", "user_id": 7},  # never somebody else's account
        {"role": "ADMIN", "comped_until": "2030-01-01"},
    ],
)
def test_the_body_refuses_nothing_and_anything_extra(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DevSwitchRequest.model_validate(body)


def test_the_answer_says_what_is_in_effect() -> None:
    assert DevSwitchAnswer(role="ADMIN").model_dump() == {"role": "ADMIN", "plan": None, "today": None}
    assert DevSwitchAnswer(role="MEMBER", plan="pro").plan == "pro"
    assert DevSwitchAnswer(role="MEMBER", today=dt.date(2026, 10, 31)).model_dump(mode="json")["today"] == "2026-10-31"


def test_today_left_out_is_unchanged_and_null_clears_it() -> None:
    """The dev clock (0.20): unlike a role or a plan, an override is taken back, so the
    body tells "not named" from "named as null"."""
    set_ = DevSwitchRequest.model_validate({"today": "2026-10-31"})
    assert (set_.sets_today, set_.today) == (True, dt.date(2026, 10, 31))
    cleared = DevSwitchRequest.model_validate({"today": None})
    assert (cleared.sets_today, cleared.today) == (True, None)
    role_only = DevSwitchRequest.model_validate({"role": "ADMIN"})
    assert (role_only.sets_today, role_only.today) == (False, None)


@pytest.mark.parametrize("today", ["31.10.2026", "2026-02-30", "tomorrow", 20261031.5])
def test_today_is_a_real_date(today: object) -> None:
    with pytest.raises(ValidationError):
        DevSwitchRequest.model_validate({"today": today})


# --- the validator -----------------------------------------------------------------------


def test_the_apps_values_come_back_in_its_own_spelling() -> None:
    checked = validate_dev_switch(DevSwitchRequest(role="admin", plan="PRO"), roles=ROLES, plans=PLANS)
    assert (checked.role, checked.plan) == ("ADMIN", "pro")
    assert validate_dev_switch(DevSwitchRequest(role="MEMBER"), roles=ROLES) == DevSwitchRequest(role="MEMBER")
    assert validate_dev_switch(DevSwitchRequest(plan="free"), roles=(), plans=PLANS) == DevSwitchRequest(plan="free")


def test_the_validator_keeps_today_and_whether_it_was_named() -> None:
    cleared = validate_dev_switch(DevSwitchRequest.model_validate({"today": None}), roles=ROLES, clock=True)
    assert (cleared.sets_today, cleared.today) == (True, None)
    moved = validate_dev_switch(
        DevSwitchRequest.model_validate({"role": "admin", "today": "2026-12-31"}), roles=ROLES, clock=True
    )
    assert (moved.role, moved.today, moved.sets_today) == ("ADMIN", dt.date(2026, 12, 31), True)
    assert validate_dev_switch(DevSwitchRequest(role="MEMBER"), roles=ROLES).sets_today is False


@pytest.mark.parametrize("today", ["2026-10-31", None])
def test_without_a_clock_any_today_is_a_422(today: str | None) -> None:
    """As a plan for an app without plans (kastlan's 0.51 review): on 0.19 the field was
    refused as unknown, and an app that has not wired the clock must not answer 200 to it."""
    with pytest.raises(DevSwitchError) as caught:
        validate_dev_switch(DevSwitchRequest.model_validate({"role": "ADMIN", "today": today}), roles=ROLES)
    assert (caught.value.status_code, caught.value.code) == (422, DevSwitchErrorCode.DEV_SWITCH_NO_CLOCK)


# --- the dev clock -----------------------------------------------------------------------

REAL = dt.date(2026, 10, 26)
OVERRIDE = dt.date(2026, 10, 31)


def test_the_override_applies_only_while_the_gate_is_open() -> None:
    assert effective_today(OVERRIDE, gate=True, today=lambda: REAL) == OVERRIDE
    assert effective_today(OVERRIDE, gate=False, today=lambda: REAL) == REAL
    assert effective_today(OVERRIDE, gate=lambda: True, today=lambda: REAL) == OVERRIDE
    assert effective_today(OVERRIDE, gate=lambda: False, today=lambda: REAL) == REAL
    assert effective_today(None, gate=True, today=lambda: REAL) == REAL


@pytest.mark.parametrize("gate", [1, "true", None, object()])
def test_the_clock_fails_closed_on_anything_but_true(gate: object) -> None:
    assert effective_today(OVERRIDE, gate=gate, today=lambda: REAL) == REAL  # type: ignore[arg-type]
    assert effective_today(OVERRIDE, gate=lambda: gate, today=lambda: REAL) == REAL  # type: ignore[arg-type,return-value]


def test_the_gate_is_asked_only_when_there_is_an_override() -> None:
    asked: list[bool] = []

    def gate() -> bool:
        asked.append(True)
        return True

    assert effective_today(None, gate=gate, today=lambda: REAL) == REAL
    assert asked == []
    assert effective_today(OVERRIDE, gate=gate, today=lambda: REAL) == OVERRIDE
    assert asked == [True]


def test_the_real_date_is_the_apps_own_and_has_no_default() -> None:
    with pytest.raises(TypeError):
        effective_today(None, gate=True)  # type: ignore[call-arg]


def test_an_unknown_role_is_a_422() -> None:
    with pytest.raises(DevSwitchError) as refused:
        validate_dev_switch(DevSwitchRequest(role="SUPERUSER", plan="pro"), roles=ROLES, plans=PLANS)
    assert refused.value.status_code == 422
    assert refused.value.code is DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_ROLE
    assert "SUPERUSER" in str(refused.value)


def test_an_unknown_plan_is_a_422_and_without_plans_every_plan_is() -> None:
    with pytest.raises(DevSwitchError) as refused:
        validate_dev_switch(DevSwitchRequest(role="ADMIN", plan="enterprise"), roles=ROLES, plans=PLANS)
    assert (refused.value.status_code, refused.value.code) == (422, DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_PLAN)
    with pytest.raises(DevSwitchError):
        validate_dev_switch(DevSwitchRequest(plan="free"), roles=ROLES)


def test_every_code_has_a_status_and_a_detail() -> None:
    assert set(DEV_SWITCH_ERROR_STATUS) == set(DevSwitchErrorCode) == set(DEV_SWITCH_ERROR_DETAIL)
    for code in DevSwitchErrorCode:
        error = DevSwitchError(code)
        assert error.status_code == DEV_SWITCH_ERROR_STATUS[code]
        assert str(error) == DEV_SWITCH_ERROR_DETAIL[code]
    refused = DevSwitchError(DevSwitchErrorCode.DEV_SWITCH_REFUSED, "Sign in as the seed's customer")
    assert (refused.status_code, str(refused)) == (409, "Sign in as the seed's customer")
    assert isinstance(refused, ValueError)
    assert DevSwitchError in CONTRACT_ERRORS
    assert (DEV_SWITCH_PATH, DEV_SWITCH_LOG_TAG) == ("/dev/switch", "dev_switch")


# --- the route, as the README mounts it ---------------------------------------------------


class _User:
    def __init__(self, role: str, *, owns_work: bool = False) -> None:
        self.role = role
        self.plan = "free"
        self.owns_work = owns_work
        self.today: dt.date | None = None


def _current_user(request: Request) -> _User:
    """The app's ``get_current_user``: any signed-in user, never ``require_admin``."""
    user: _User = request.app.state.user
    return user


def _app(settings: _Settings, *, local: bool, production: bool, user: _User) -> FastAPI:
    def on() -> bool:
        return dev_switch_enabled(flag=settings.dev_switch_enabled, local_run=lambda: local, production=production)

    assert_dev_switch_safe(flag=settings.dev_switch_enabled, production=production)
    app = FastAPI()
    install_contract_error_handlers(app)

    app.state.user = user
    router = APIRouter(include_in_schema=False, dependencies=[Depends(require_dev_switch(on))])

    @router.post(DEV_SWITCH_PATH)
    async def dev_switch(body: DevSwitchRequest, me: Annotated[_User, Depends(_current_user)]) -> DevSwitchAnswer:
        body = validate_dev_switch(body, roles=ROLES, plans=PLANS, clock=True)
        if body.role == "CUSTOMER" and me.owns_work:
            raise DevSwitchError(DevSwitchErrorCode.DEV_SWITCH_REFUSED, "Sign in as the seed's customer")
        if body.role is not None:
            me.role = body.role
        if body.plan is not None:
            me.plan = body.plan
        if body.sets_today:
            me.today = body.today
        return DevSwitchAnswer(role=me.role, plan=me.plan, today=me.today)

    if on():
        app.include_router(router)
    app.state.dev_router = router
    return app


async def _post(app: FastAPI, body: object) -> tuple[int, object]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(DEV_SWITCH_PATH, json=body)
    return response.status_code, response.json()


async def test_a_member_switches_to_admin_and_back_locally() -> None:
    user = _User("MEMBER")
    app = _app(_Settings(dev_switch_enabled=True), local=True, production=False, user=user)
    assert await _post(app, {"role": "admin", "plan": "pro"}) == (200, {"role": "ADMIN", "plan": "pro", "today": None})
    assert await _post(app, {"role": "MEMBER"}) == (200, {"role": "MEMBER", "plan": "pro", "today": None})
    assert DEV_SWITCH_PATH not in str(app.openapi())


async def test_the_dev_clock_is_set_kept_and_cleared_locally() -> None:
    user = _User("MEMBER")
    app = _app(_Settings(dev_switch_enabled=True), local=True, production=False, user=user)
    assert await _post(app, {"today": "2026-10-31"}) == (200, {"role": "MEMBER", "plan": "free", "today": "2026-10-31"})
    # A role switch leaves it where it is.
    assert await _post(app, {"role": "ADMIN"}) == (200, {"role": "ADMIN", "plan": "free", "today": "2026-10-31"})
    assert await _post(app, {"today": None}) == (200, {"role": "ADMIN", "plan": "free", "today": None})


@pytest.mark.parametrize(
    ("flag", "local"),
    [(False, True), (True, False), (False, False)],
)
async def test_without_the_gate_the_route_is_absent(flag: bool, local: bool) -> None:
    user = _User("MEMBER")
    app = _app(_Settings(dev_switch_enabled=flag), local=local, production=False, user=user)
    status, _ = await _post(app, {"role": "ADMIN"})
    assert status == 404
    assert user.role == "MEMBER"


def test_the_flag_in_production_never_builds_the_app() -> None:
    with pytest.raises(DevSwitchUnsafeError):
        _app(_Settings(dev_switch_enabled=True), local=True, production=True, user=_User("MEMBER"))


async def test_a_router_included_by_mistake_still_answers_404() -> None:
    settings = _Settings(dev_switch_enabled=True)
    user = _User("MEMBER")
    app = _app(settings, local=True, production=False, user=user)
    settings.dev_switch_enabled = False  # the gate shuts after the mount: the second lock holds
    assert await _post(app, {"role": "ADMIN"}) == (404, {"detail": "Not Found", "code": "dev_switch_disabled"})
    assert user.role == "MEMBER"

    stray = FastAPI()
    install_contract_error_handlers(stray)
    stray.state.user = user
    stray.include_router(app.state.dev_router)  # never mounted through the gate
    assert (await _post(stray, {"role": "ADMIN"}))[0] == 404
    assert user.role == "MEMBER"


async def test_the_route_refuses_unknown_values_and_the_apps_own_invariant() -> None:
    owner = _User("ADMIN", owns_work=True)
    app = _app(_Settings(dev_switch_enabled=True), local=True, production=False, user=owner)
    status, body = await _post(app, {"role": "ROOT"})
    assert (status, body["code"]) == (422, "dev_switch_unknown_role")  # type: ignore[index]
    status, body = await _post(app, {"plan": "gold"})
    assert (status, body["code"]) == (422, "dev_switch_unknown_plan")  # type: ignore[index]
    assert (await _post(app, {}))[0] == 422
    assert await _post(app, {"role": "CUSTOMER"}) == (
        409,
        {"detail": "Sign in as the seed's customer", "code": "dev_switch_refused"},
    )
    assert owner.role == "ADMIN"


def _gate(value: object) -> Callable[[], bool]:
    return lambda: value  # type: ignore[return-value]


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_the_dependency_lets_through_only_true(value: object) -> None:
    with pytest.raises(DevSwitchError) as refused:
        require_dev_switch(_gate(value))(None)  # type: ignore[arg-type]
    assert refused.value.status_code == 404
    require_dev_switch(_gate(True))(None)  # type: ignore[arg-type]


# --- database_is_local (0.21): the database half of the local-run proof ------------------


def test_the_local_endpoints_are_postgres_own() -> None:
    assert frozenset({5432}) == LOCAL_DATABASE_PORTS
    assert frozenset({"/run/postgresql", "/var/run/postgresql", "/tmp"}) == LOCAL_SOCKET_DIRS


@pytest.mark.parametrize(
    "url",
    [
        # the apps' own defaults (kastlan config.py:21, Kurvenschmiede settings.py:156)
        "postgresql+asyncpg://kastlan:kastlan_dev@127.0.0.1:5432/kastlan",
        "postgresql+asyncpg://kurvenschmiede:kurvenschmiede_dev@127.0.0.1:5432/kurvenschmiede",
        "postgresql+asyncpg://keksdose_test:keksdose_test@127.0.0.1:5432/keksdose_test",
        "postgresql+asyncpg://kastlan:kastlan_dev@localhost/kastlan",  # no port: 5432
        "postgresql+psycopg://kastlan@LOCALHOST:5432/kastlan",
        "postgresql://u:p@[::1]:5432/db",
        "postgresql://u:p@127.0.0.2:5432/db",  # all of 127.0.0.0/8
        "postgres://u:p@127.0.0.1/db",
        "  postgresql://u:p@127.0.0.1:5432/db  ",
        # a local socket, as a query or as a percent-encoded host
        "postgresql+asyncpg://kastlan@/kastlan?host=/var/run/postgresql",
        "postgresql+asyncpg://kastlan@/kastlan?host=/run/postgresql/",
        "postgresql+asyncpg://kastlan@/kastlan?host=%2Ftmp",
        "postgresql://kastlan@%2Fvar%2Frun%2Fpostgresql/kastlan",
        "postgresql+asyncpg://kastlan@/kastlan?host=/run/postgresql&port=5433",  # the socket's port is its own
        "postgresql://u:p@localhost:5432/db?host=/run/postgresql",
        "postgresql://u:p@localhost/db?port=5432",
    ],
)
def test_a_database_on_this_machine_is_local(url: str) -> None:
    assert database_is_local(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # the apps' tunnels to their deployments: the loopback, another port
        "postgresql+asyncpg://kastlan_app:secret@127.0.0.1:5433/kastlan",  # kastlan feedback-db.sh
        "postgresql+asyncpg://kastlan_app:secret@127.0.0.1:5436/kastlan",
        "postgresql://keksdose_feedback:secret@127.0.0.1:5433/keksdose",  # keksdose feedback-live-lib.sh
        "postgresql://postgres:secret@127.0.0.1:55432/kurvenschmiede",  # KS apply-roles.sh
        "postgresql://u:p@localhost/db?port=5433",
        # Cloud SQL's sockets
        "postgresql+asyncpg://u:p@/db?host=/cloudsql/keksdose:europe-west4:keksdose-db",
        "postgresql+asyncpg://u:p@/db?host=/tmp/keksdose:europe-west4:keksdose-db",
        "postgresql+asyncpg://u:p@/db?host=/home/ada/sockets",
        # a remote host, a bind address, a container's name
        "postgresql+asyncpg://u:p@db.example.com:5432/db",
        "postgresql+asyncpg://u:p@10.0.0.5:5432/db",
        "postgresql+asyncpg://u:p@0.0.0.0:5432/db",
        "postgresql+asyncpg://u:p@db:5432/db",
        "postgresql+asyncpg://u:p@localhost.example.com:5432/db",
        # no host: the driver picks one (PGHOST, keksdose's db_socket_dir on Cloud Run)
        "postgresql+asyncpg://keksdose:secret@/keksdose",
        "postgresql:///db",
        # every host must be local, one host and one port only
        "postgresql://u:p@127.0.0.1:5432/db?host=/cloudsql/p:r:i",
        "postgresql://u:p@127.0.0.1:5432/db?host=db.example.com",
        "postgresql://u:p@/db?host=/run/postgresql&host=db.example.com",
        "postgresql://u:p@/db?host=/run/postgresql,db.example.com",
        "postgresql://u:p@127.0.0.1:5432,db.example.com:5432/db",
        "postgresql://u:p@127.0.0.1:5432/db?port=5433",
        "postgresql://u:p@127.0.0.1/db?port=5432&port=5433",
        "postgresql://u:p@127.0.0.1/db?port=fifty",
        "postgresql://u:p@127.0.0.1/db?port=５４３２",
        # not Postgres, not a URL
        "sqlite+aiosqlite:///./keksdose.db",
        "sqlite:///:memory:",
        "mysql://u:p@127.0.0.1:5432/db",
        "",
        "127.0.0.1:5432",
        "postgresql://u:p@[::1/db",
        "postgresql://u:p@127.0.0.1:port/db",
    ],
)
def test_anything_else_is_not(url: str) -> None:
    assert database_is_local(url) is False


def test_an_app_names_its_own_local_port_and_sockets() -> None:
    """keksdose's Docker Postgres on 5433 (``.env.example``) — once its tunnel moved off it."""
    docker = "postgresql+asyncpg://keksdose:keksdose@127.0.0.1:5433/keksdose"
    assert database_is_local(docker) is False
    assert database_is_local(docker, ports={5432, 5433}) is True
    assert database_is_local("postgresql://u:p@127.0.0.1:5432/db", ports={5433}) is False
    socket = "postgresql://u@/db?host=/Users/ada/pg"
    assert database_is_local(socket) is False
    assert database_is_local(socket, socket_dirs={"/Users/ada/pg/"}) is True
    assert database_is_local("postgresql://u@/db?host=/tmp", socket_dirs=()) is False


def test_it_is_the_proof_the_gate_takes() -> None:
    tunnel = "postgresql+asyncpg://kastlan_app:secret@127.0.0.1:5433/kastlan"
    laptop = "postgresql+asyncpg://kastlan:kastlan_dev@127.0.0.1:5432/kastlan"
    for url, on in ((tunnel, False), (laptop, True)):
        assert dev_switch_enabled(flag=True, local_run=partial(database_is_local, url), production=False) is on
