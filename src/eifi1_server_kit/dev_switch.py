"""The dev switch: in local development only, a signed-in user sets their own role and plan.

``docs/top-bar-harmonization.md`` §2.7 and §6.1–§6.3 in ``Eifi1/ui-kit`` (§6 wins). The
kit's ``DevSwitchers`` (role and plan, flask icon) call ``POST /dev/switch {role?, plan?}``,
and the SERVER then enforces that role and plan, so the app behaves as it. **The route lets
a user make themselves ADMIN**, so its gate is the point of this module: it must be
impossible to switch on in a deployment by accident.

Layer 1, like the rest of the kit: the gate, the startup check, the body, the refusals and
a validator — no route, no database. Each app writes the role and the plan its own way (the
recipe below and in the README).

* :class:`DevSwitchSettings` — ``dev_switch_enabled: bool = False``, for the app's settings
  to inherit (``<APP>_DEV_SWITCH_ENABLED``, set only in the local ``.env``);
* :func:`dev_switch_enabled` — the gate: the flag AND the app's proof of a local run AND
  not production; :func:`assert_dev_switch_safe` and :class:`DevSwitchUnsafeError` — the
  app refuses to start with the flag set in production;
* :func:`require_dev_switch` — a route dependency that answers 404 whenever the gate
  doesn't hold, behind the mount;
* :class:`DevSwitchRequest`, :class:`DevSwitchAnswer` — the wire shapes;
* :func:`validate_dev_switch` — the app's roles and plans only (422);
  :class:`DevSwitchError` and :class:`DevSwitchErrorCode` — the refusals.

**The gate is never an environment name** (§6.1). Every app has one that says
"development" where it must not be trusted:

* keksdose runs a HOSTED dev deployment (``keksdose-dev`` on Cloud Run,
  ``.github/workflows/deploy-dev.yml``), and its only environment-like setting,
  ``metrics_environment``, defaults to ``"dev"`` (``infrastructure/settings.py:711``):
  gated on it, anyone signed in on the hosted dev service could make themselves ADMIN
  (review K-1);
* kastlan's ``environment`` DEFAULTS to ``"development"``
  (``kastlan/infrastructure/config.py:25-27``); a deployment that forgot
  ``KASTLAN_ENVIRONMENT=production`` would mount the route (F3);
* Kurvenschmiede's ``KURVENSCHMIEDE_DEPLOYMENT`` defaults to ``development``
  (``backend/kurvenschmiede/infrastructure/settings.py:135``); only the image bakes in
  ``production``, so a hand-run backend against a Cloud SQL proxy would mount it (KS 6a).

So the gate takes three things, and all three must agree: the explicit, off-by-default flag;
the app's own proof that this is a local run (kastlan: a loopback database host and no
``K_SERVICE``, which Cloud Run sets; keksdose: a localhost or local-socket ``database_url``
and no ``db_socket_dir``; KS ``is_development and is_local_database(settings)``,
``feedback_loop.py:266``); and the app's production signal, which closes it whatever the
other two say (kastlan ``environment == "production"``; keksdose
``bool(db_socket_dir) or metrics_environment == "prod"``, since both its Cloud Run
services set ``KEKSDOSE_DB_SOCKET_DIR``; KS ``deployment is Deployment.PRODUCTION``).

**The recipe** (the kit ships no router for it, as for billing's routes; ``<APP>``,
``is_local_run`` and ``is_production`` are the app's own, as above)::

    class Settings(BaseSettings, DevSwitchSettings):  # <APP>_DEV_SWITCH_ENABLED=true in .env only
        model_config = SettingsConfigDict(env_prefix="<APP>_")

    def dev_switch_on(settings: Settings) -> bool:
        return dev_switch_enabled(
            flag=settings.dev_switch_enabled,
            local_run=lambda: is_local_run(settings),  # the app's proof
            production=is_production(settings),
        )

    def create_app(settings: Settings) -> FastAPI:
        assert_dev_switch_safe(flag=settings.dev_switch_enabled, production=is_production(settings))
        ...
        if dev_switch_on(settings):  # otherwise the route is absent: 404
            app.include_router(dev_router)

    dev_router = APIRouter(
        include_in_schema=False,  # never in the OpenAPI schema
        dependencies=[Depends(require_dev_switch(lambda: dev_switch_on(get_settings())))],
    )

    @dev_router.post(DEV_SWITCH_PATH)
    async def dev_switch(body: DevSwitchRequest, user=Depends(get_current_user), session=Depends(db)) -> DevSwitchAnswer:
        body = validate_dev_switch(body, roles=ROLES, plans=PLANS)  # a signed-in user, NOT require_admin
        if body.role is not None:
            user.role = body.role  # the app's own write, see below
        if body.plan is not None:
            await set_plan(session, user, body.plan, comped_until=None)  # the operator's grant, permanent
        await session.commit()
        return DevSwitchAnswer(role=user.role, plan=...)

    @dev_router.get(DEV_SWITCH_PATH)
    async def dev_switch_state(user=Depends(get_current_user)) -> DevSwitchAnswer:
        return DevSwitchAnswer(role=user.role, plan=...)  # the plan in effect, billing on or off

* **``GET /dev/switch`` is the client's probe** (kastlan B4): behind the same gate, it
  answers :class:`DevSwitchAnswer` — the role and the plan in effect. A 404 means the gate
  is shut, so the client hides ``DevSwitchers`` (``enabled={import.meta.env.DEV &&
  answered}``) rather than drawing switchers with nothing behind them; an answer gives
  ``currentRole`` and ``currentPlan``, which the overview can't while billing is off.

* **A signed-in user only, never ADMIN** (§6.2): a switch to a lesser role must be able to
  switch back. The kit's ``DevSwitchers`` keeps the original role in session storage and
  offers "Back to {role}".
* **The role is written in the local database** (§6.2), since every app reads it from the
  row on each request: keksdose ``users.role``; kastlan the acting company's
  ``user_role_assignments`` (replace the caller's roles there), then ``issue_session``;
  Kurvenschmiede ``users.role`` on the caller's own row, directly, skipping
  ``admin_service.set_role``'s self and last-admin refusals, but still refusing CUSTOMER
  for an account that owns work — ``DevSwitchError(DevSwitchErrorCode.DEV_SWITCH_REFUSED,
  "Sign in as the seed's customer")``, a 409.
* **The plan goes through the operator's grant, permanent** (§6.3): keksdose
  ``admin_user_actions_service.set_plan(session, user, plan, comped_until=None)``, kastlan
  ``BillingService.set_plan(company_id, plan, comped_until=None)``, Kurvenschmiede
  ``billing_service.change_plan(…, comped_until=None, acknowledged=True)`` on the caller's
  own row. That makes a permanent grant, and Billing §16's permanent-grant warning shows
  on that account's checkout — EXCEPT on a row under a running beta grant (keksdose
  before launch, kastlan likewise): ``comped_until=None`` keeps the beta status and source
  and moves only the plan, so no permanent grant is made and no warning shows. That is
  accepted: the switch is for previewing limits. With billing off, nothing is enforced;
  the plan menu says so.
* **No admin action log entry** — or one tagged :data:`DEV_SWITCH_LOG_TAG` — so a local
  switch doesn't fill the activity log (review K-3).
* The client then re-reads ``/auth/me`` and the overview. kastlan's real
  ``/auth/switch-role`` (a held role) stays as it is.

**The dev clock** (0.20, keksdose dev #640): ``{today: "2026-10-31"}`` sets the date the
server takes for today, ``{today: null}`` takes it back. The app keeps the override where
it keeps its other local state (a settings row, a column on the user, a process-wide
variable for a one-user dev server) and answers it in :class:`DevSwitchAnswer`; every path
that asks for today — scheduled and recurring items, month ends, the forecast — asks
:func:`effective_today`, which gives the real date whenever the gate is shut::

    def today(settings: Settings, override: date | None) -> date:
        return effective_today(override, gate=lambda: dev_switch_on(settings), today=app_today)

While it is set, the client takes "today" from the answer (``GET /dev/switch``), not from
its own clock, so the dates it works out itself agree with the server's.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Callable, Collection, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request

__all__ = [
    "DEV_SWITCH_ERROR_DETAIL",
    "DEV_SWITCH_ERROR_STATUS",
    "DEV_SWITCH_LOG_TAG",
    "DEV_SWITCH_PATH",
    "DevSwitchAnswer",
    "DevSwitchError",
    "DevSwitchErrorCode",
    "DevSwitchRequest",
    "DevSwitchSettings",
    "DevSwitchUnsafeError",
    "assert_dev_switch_safe",
    "dev_switch_enabled",
    "effective_today",
    "require_dev_switch",
    "validate_dev_switch",
]

#: The route, under the app's API prefix (``/api/v1/dev/switch``).
DEV_SWITCH_PATH = "/dev/switch"
#: The tag an app that does log the switch puts on its admin action entry, so a local
#: switch is told apart from an operator's (review K-3). Logging nothing is as good.
DEV_SWITCH_LOG_TAG = "dev_switch"


# --- the setting -------------------------------------------------------------------------


class DevSwitchSettings(BaseModel):
    """The flag, for the app's settings to INHERIT, so each reads its own variable
    (``KEKSDOSE_DEV_SWITCH_ENABLED``, ``KASTLAN_DEV_SWITCH_ENABLED``,
    ``KURVENSCHMIEDE_DEV_SWITCH_ENABLED``)::

        class Settings(BaseSettings, DemoSettings, DevSwitchSettings):
            model_config = SettingsConfigDict(env_prefix="KASTLAN_")

    An app that keeps its own field declares ``<app>_dev_switch_enabled: bool = False``
    the same way. **Set it only in the local ``.env``**, never in a deploy file, a
    Cloud Run variable or a Secret: it is one of three signals, and the only one a
    developer turns on by hand.
    """

    model_config = ConfigDict(from_attributes=True)

    #: Off by default: the gate stays shut and the route is absent (404). On in production,
    #: the app refuses to start (:func:`assert_dev_switch_safe`).
    dev_switch_enabled: bool = False


# --- the gate ----------------------------------------------------------------------------


class DevSwitchUnsafeError(RuntimeError):
    """The dev switch's flag is set in production: the app must not start (§6.1).

    A ``RuntimeError``, not a refusal: it is raised while the app is built, and no
    request is ever answered with it.
    """

    def __init__(self, detail: str = "The dev switch is enabled in production: unset <APP>_DEV_SWITCH_ENABLED") -> None:
        super().__init__(detail)


def dev_switch_enabled(*, flag: bool, local_run: bool | Callable[[], bool], production: bool) -> bool:
    """Is the dev switch on? Only when ``flag`` is on AND ``local_run`` holds AND
    ``production`` is off (§6.1).

    * ``flag`` — :attr:`DevSwitchSettings.dev_switch_enabled`, the explicit opt-in;
    * ``local_run`` — the app's own proof of a local run, or a callable giving it, which
      is asked only when the flag is on and production off (KS's
      ``is_local_database(settings)`` looks at the database URL and the key ring);
    * ``production`` — the app's production signal (kastlan
      ``environment == "production"``, KS ``not is_development``).

    **Never pass an environment name as the proof alone**: see the module's notes.
    Fail-closed: anything but exactly ``True`` for the flag and the proof, or exactly
    ``False`` for production, shuts the gate.
    """
    if flag is not True or production is not False:
        return False
    proof = local_run() if callable(local_run) else local_run
    return proof is True


def assert_dev_switch_safe(*, flag: bool, production: bool) -> None:
    """Refuse to start when the flag is set in production (§6.1):
    :class:`DevSwitchUnsafeError`. Call it while building the app, before anything is
    mounted. An unknown production signal (anything but ``False``) counts as production."""
    if flag and production is not False:
        raise DevSwitchUnsafeError


# --- the refusals ------------------------------------------------------------------------


class DevSwitchErrorCode(enum.StrEnum):
    """The ``code`` of a dev switch refusal. A ``StrEnum``, so ``str(code)`` is the wire
    value."""

    #: The gate doesn't hold: 404, "not there" — the same as an absent route.
    DEV_SWITCH_DISABLED = "dev_switch_disabled"
    #: A role the app doesn't have: 422.
    DEV_SWITCH_UNKNOWN_ROLE = "dev_switch_unknown_role"
    #: A plan the app doesn't have: 422.
    DEV_SWITCH_UNKNOWN_PLAN = "dev_switch_unknown_plan"
    #: The app's own invariant refuses the switch (KS: CUSTOMER for an account that owns
    #: work): 409, with the app's detail.
    DEV_SWITCH_REFUSED = "dev_switch_refused"


#: Each code's status.
DEV_SWITCH_ERROR_STATUS: Mapping[DevSwitchErrorCode, int] = {
    DevSwitchErrorCode.DEV_SWITCH_DISABLED: 404,
    DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_ROLE: 422,
    DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_PLAN: 422,
    DevSwitchErrorCode.DEV_SWITCH_REFUSED: 409,
}

#: The English ``detail`` of each code, for logs and API clients.
DEV_SWITCH_ERROR_DETAIL: Mapping[DevSwitchErrorCode, str] = {
    DevSwitchErrorCode.DEV_SWITCH_DISABLED: "Not Found",
    DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_ROLE: "Not a role of this app",
    DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_PLAN: "Not a plan of this app",
    DevSwitchErrorCode.DEV_SWITCH_REFUSED: "This switch is not possible for this account",
}


class DevSwitchError(ValueError):
    """A dev switch refusal: ``status_code`` and ``code`` from :class:`DevSwitchErrorCode`,
    answered ``{detail, code}`` by
    :func:`~eifi1_server_kit.errors.install_contract_error_handlers`. A ``ValueError``
    like every kit refusal."""

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 404
    code: DevSwitchErrorCode

    def __init__(self, code: DevSwitchErrorCode, detail: str | None = None) -> None:
        self.code = DevSwitchErrorCode(code)
        self.status_code = DEV_SWITCH_ERROR_STATUS[self.code]
        super().__init__(detail or DEV_SWITCH_ERROR_DETAIL[self.code])


def require_dev_switch(gate: Callable[[], bool]) -> Callable[[Request], None]:
    """The route dependency behind the mount: 404 ``dev_switch_disabled`` whenever
    ``gate()`` isn't exactly ``True``, asked at every call::

        dev_router = APIRouter(dependencies=[Depends(require_dev_switch(lambda: dev_switch_on(get_settings())))])

    The mount is the gate; this is the second lock, for a router included by mistake or a
    setting changed in a test. It takes Starlette's ``Request``, as FastAPI hands a
    dependency, so the kit imports no FastAPI.
    """

    def dev_switch_dependency(request: Request) -> None:
        del request
        if gate() is not True:
            raise DevSwitchError(DevSwitchErrorCode.DEV_SWITCH_DISABLED)

    return dev_switch_dependency


# --- the wire shapes ---------------------------------------------------------------------


class DevSwitchRequest(BaseModel):
    """``POST /dev/switch``'s body: ``{role?, plan?, today?}``, at least one of them (§2.7).

    Unknown fields are refused, and so is an empty body (422 through Pydantic). Whether the
    values are the app's is :func:`validate_dev_switch`'s question.

    ``today`` (0.20, keksdose dev #640, the kit's ``DevMenu`` "Time" section) is a date the
    server takes for "today" — for scheduled, recurring and month-end paths — until it is
    cleared. Unlike ``role`` and ``plan`` it can be taken back, so leaving it out and
    sending ``null`` differ: left out it is unchanged, ``null`` clears it
    (:attr:`sets_today` tells them apart). The app keeps it its own way, as it does the
    role, and reads it only through :func:`effective_today`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: The role to act as, in the app's own words (``ADMIN``, ``MEMBER``, ``CUSTOMER``).
    role: str | None = Field(default=None, min_length=1, max_length=64)
    #: The plan to act on, a plan code (``free``, ``pro``).
    plan: str | None = Field(default=None, min_length=1, max_length=64)
    #: The date to take for today; ``null`` goes back to the real one. Left out: unchanged.
    today: dt.date | None = None

    @property
    def sets_today(self) -> bool:
        """Whether the body names ``today`` at all — a date, or ``null`` to clear it."""
        return "today" in self.model_fields_set

    @model_validator(mode="after")
    def _one_at_least(self) -> DevSwitchRequest:
        if self.role is None and self.plan is None and not self.sets_today:
            raise ValueError("a dev switch names a role, a plan, a today, or several")
        return self


class DevSwitchAnswer(BaseModel):
    """The role and the plan now in effect: the answer of ``POST /dev/switch``, and of the
    ``GET /dev/switch`` probe the client reads to learn that the gate is open and what to
    check in ``DevSwitchers`` (see the module's recipe). After a switch the client re-reads
    ``/auth/me`` and the overview all the same."""

    role: str
    #: ``None`` for an app or account without a plan.
    plan: str | None = None
    #: The date the server takes for today while one is set, else ``None`` (the real date).
    #: While it is set the client takes "today" from here too, so the dates it works out
    #: itself (a month's end, a "Committed <date>") are the server's.
    today: dt.date | None = None


def _match(value: str, offered: Collection[str]) -> str | None:
    """``value`` in the app's spelling, compared without case; ``None`` when not offered."""
    wanted = value.casefold()
    for candidate in offered:
        if candidate.casefold() == wanted:
            return candidate
    return None


def validate_dev_switch(
    request: DevSwitchRequest, *, roles: Collection[str], plans: Collection[str] = ()
) -> DevSwitchRequest:
    """``request`` with its role and plan in the app's own spelling, or a refusal: 422
    ``dev_switch_unknown_role`` / ``dev_switch_unknown_plan`` for a value the app doesn't
    offer (compared without case: ``admin`` is ``ADMIN``).

    ``roles`` and ``plans`` are the app's (a role enum's values, the plan catalogue's
    codes); an app without plans passes none, and every plan is then refused. Whether the
    caller may hold that role — KS's CUSTOMER for an account that owns work — stays the
    app's, with :attr:`DevSwitchErrorCode.DEV_SWITCH_REFUSED`.
    """
    role = request.role
    if role is not None:
        role = _match(role, roles)
        if role is None:
            raise DevSwitchError(
                DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_ROLE, f"Not a role of this app: {request.role!r}"
            )
    plan = request.plan
    if plan is not None:
        plan = _match(plan, plans)
        if plan is None:
            raise DevSwitchError(
                DevSwitchErrorCode.DEV_SWITCH_UNKNOWN_PLAN, f"Not a plan of this app: {request.plan!r}"
            )
    # `today` only when the body named it, so "left out" stays "unchanged".
    today = {"today": request.today} if request.sets_today else {}
    return DevSwitchRequest(role=role, plan=plan, **today)


# --- the dev clock -----------------------------------------------------------------------


def effective_today(
    override: dt.date | None,
    *,
    gate: bool | Callable[[], bool],
    today: Callable[[], dt.date] = dt.date.today,
) -> dt.date:
    """The date the app takes for today: the dev switch's ``override`` while the gate is
    open, else ``today()`` — the app's own real "today" (its time zone; default the server's
    local date).

    Every path that asks for today goes through this, so a stale override can never apply
    outside a local run: ``gate`` is the same predicate the route is mounted on
    (``dev_switch_on(settings)``, or a callable giving it), and anything but exactly ``True``
    shuts it — an override left in a local database that is later pointed at by a
    deployment reads as the real date. ``gate`` is asked only when there is an override.
    """
    if override is None:
        return today()
    open_ = gate() if callable(gate) else gate
    return override if open_ is True else today()
