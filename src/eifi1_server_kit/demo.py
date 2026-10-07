"""The demo: a throwaway account that lets a visitor try the app before an invitation.

``docs/landing-demo-harmonization.md`` §5–§7 in ``Eifi1/ui-kit``. Layer 1, like the rest of
the kit: the settings, the gate's order, the refusals and their codes, the address and the
dates — no database, no app ``User``, no route. The demo data and its seeder, the access
mechanics (keksdose's RLS and viewer share, kastlan's company ADMIN and read-only
transaction, Kurvenschmiede's grants and sandbox), the reap's row list and the refusal at
each route stay in each app (§7.3). kastlan's demo backend settles the gate's order and
the read-only recipe; keksdose's names the settings.

* :class:`DemoSettings` — the five settings of §6.1, for the app's ``Settings`` to inherit;
  the switch is OFF by default;
* :class:`DemoGate` — ``POST /auth/demo-session``'s checks in §5.1's order, around the
  app's three callbacks (reap, count the live users, is the demo data there);
* :class:`DemoError` and :class:`DemoErrorCode` — the six refusals of §6.2, answered
  ``{detail, code}`` (and ``Retry-After``) by
  :func:`~eifi1_server_kit.errors.install_contract_error_handlers`;
* :func:`demo_write_allowed` — layer 1 of model R's read-only (§6.3); :func:`refuse_demo`
  — the never-list of §6.4;
* :func:`demo_address`, :func:`is_demo_address`, :func:`demo_password` — the throwaway
  user (§5.1); :func:`demo_expires_at`, :func:`stale_cutoff` — its one lifetime (§5.3,
  §5.7).

The per-IP window keys on :func:`~eifi1_server_kit.limiter.client_ip`, counted from the
right of ``X-Forwarded-For`` (§5.1).
"""

from __future__ import annotations

import enum
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field

from eifi1_server_kit.auth.accounts import normalise_email
from eifi1_server_kit.auth.limits import Budget
from eifi1_server_kit.limiter import Clock, retry_after_header

__all__ = [
    "DEMO_ERROR_DETAIL",
    "DEMO_ERROR_STATUS",
    "DEMO_FIRST_NAME",
    "DEMO_LOCAL_PART",
    "DEMO_SUBDOMAIN",
    "READ_METHODS",
    "REAP_PER_START",
    "DemoAdmission",
    "DemoCounter",
    "DemoError",
    "DemoErrorCode",
    "DemoGate",
    "DemoReaper",
    "DemoSettings",
    "demo_address",
    "demo_expires_at",
    "demo_password",
    "demo_write_allowed",
    "is_demo_address",
    "refuse_demo",
    "stale_cutoff",
]

#: A start reaps at most this many stale demo users, the oldest first (§5.1 step 3, §5.7).
#: The scheduled job, or the next starts, take the rest: a start that had to delete a
#: thousand rows would time out, and the live cap would close the demo meanwhile.
REAP_PER_START = 20
#: The methods a demo user may always send (§6.3): they read.
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
#: The demo user's first name; it has no last name, so every app passes ``is_demo`` to
#: :func:`~eifi1_server_kit.auth.name_incomplete` or each demo is asked to complete it
#: (§5.1).
DEMO_FIRST_NAME = "Demo"
#: The local part of every demo address, before its ``+<32 hex>`` tag.
DEMO_LOCAL_PART = "demo"
#: The subdomain of the app's own domain that demo addresses live on. It has no MX record
#: and no routing rule, so nothing sent to it can reach anyone (§5.1).
DEMO_SUBDOMAIN = "demo"


# --- §6.1: the settings ------------------------------------------------------------------


class DemoSettings(BaseModel):
    """The demo's settings (§6.1), named as keksdose names them.

    Meant to be INHERITED by the app's settings, so each one reads its environment variable
    there (``KEKSDOSE_DEMO_SESSION_ENABLED``, ``KASTLAN_DEMO_USER_MAX_AGE_HOURS``)::

        class Settings(BaseSettings, DemoSettings):
            demo_session_enabled: bool = True   # keksdose turns it on in its own settings
            demo_company_slug: str = "demo"     # kastlan's own

    An app that keeps its own fields builds one from them instead:
    ``DemoSettings.model_validate(settings, from_attributes=True)``.

    **The switch is off by default** (§2.6): a fresh deployment has no demo data, and a demo
    that answers 503 on the landing page's main button is worse than none.
    ``demo_session_token_minutes`` is gone (§5.3): the token lives as long as the account.
    """

    model_config = ConfigDict(from_attributes=True)

    #: Off: ``POST /auth/demo-session`` answers 404 ``demo_disabled`` — "not there", not
    #: "forbidden" (§2.6).
    demo_session_enabled: bool = False
    #: The demo account's life AND its token's (§5.3): 24 hours, after which the reap
    #: deletes it (§5.7).
    demo_user_max_age_hours: int = Field(default=24, gt=0)
    #: At most this many live demo users — those younger than the maximum age (§5.1 step 4).
    #: Counted in the database, so it holds across every instance.
    demo_session_max_live: int = Field(default=500, ge=0)
    #: New demos per IP in the window below (§5.1 step 2); 0 or less turns the window off.
    #: Process-local, like every :class:`~eifi1_server_kit.limiter.SlidingWindowRateLimiter`.
    demo_session_rate_max: int = 5
    #: The per-IP window, in seconds.
    demo_session_rate_window_seconds: int = 3600

    @property
    def demo_user_max_age(self) -> timedelta:
        """``demo_user_max_age_hours`` as a :class:`~datetime.timedelta`."""
        return timedelta(hours=self.demo_user_max_age_hours)

    @property
    def demo_session_rate(self) -> Budget:
        """The per-IP window as a :class:`~eifi1_server_kit.auth.Budget` — ``Budget(5,
        3600)`` by default."""
        return Budget(self.demo_session_rate_max, self.demo_session_rate_window_seconds)


def demo_expires_at(created_at: datetime, settings: DemoSettings) -> datetime:
    """When a demo account — and its token — ends: ``created_at`` plus the maximum age
    (§5.3). The access token's ``exp``, ``TokenResponse.expires_at`` and
    ``UserResponse.demo_expires_at`` all say this one instant, so the countdown and the
    401 agree. Naive in, naive out."""
    return created_at + settings.demo_user_max_age


def stale_cutoff(now: datetime, settings: DemoSettings) -> datetime:
    """The instant that splits stale from live (§5.7): a demo user created BEFORE it is
    stale and reaped; one created at or after it is live and counted. The same instant
    :func:`demo_expires_at` ends the token at, seen from the other side. Naive in, naive
    out — pass ``now`` in the kind your column stores."""
    return now - settings.demo_user_max_age


# --- §6.2: the codes ---------------------------------------------------------------------


class DemoErrorCode(enum.StrEnum):
    """The ``code`` of a demo refusal (§6.2); the kit's ``DemoErrorCode`` reads the same
    words. A ``StrEnum``, so ``str(code)`` is the wire value."""

    #: The switch is off: 404, "not there" (§2.6).
    DEMO_DISABLED = "demo_disabled"
    #: The per-IP window: 429 with ``Retry-After``; the page says "try again in N min".
    DEMO_RATE_LIMITED = "demo_rate_limited"
    #: The live cap: 429 without ``Retry-After`` — nobody knows when a place frees up.
    DEMO_CAPACITY = "demo_capacity"
    #: The demo data isn't there yet (a deploy step seeds it, never a request): 503.
    DEMO_NOT_READY = "demo_not_ready"
    #: Model R: any write by a demo user (§6.3): 403. The page shows no toast for it; the
    #: write lock already explains.
    DEMO_READ_ONLY = "demo_read_only"
    #: Either model: an action a demo user never may (§6.4): 403.
    DEMO_REFUSED = "demo_refused"


#: Each code's status (§6.2).
DEMO_ERROR_STATUS: Mapping[DemoErrorCode, int] = {
    DemoErrorCode.DEMO_DISABLED: 404,
    DemoErrorCode.DEMO_RATE_LIMITED: 429,
    DemoErrorCode.DEMO_CAPACITY: 429,
    DemoErrorCode.DEMO_NOT_READY: 503,
    DemoErrorCode.DEMO_READ_ONLY: 403,
    DemoErrorCode.DEMO_REFUSED: 403,
}

#: The English ``detail`` of each code, for logs and API clients; the kit's pages show their
#: own words for the code.
DEMO_ERROR_DETAIL: Mapping[DemoErrorCode, str] = {
    DemoErrorCode.DEMO_DISABLED: "The demo is not available",
    DemoErrorCode.DEMO_RATE_LIMITED: "Too many demos from this network",
    DemoErrorCode.DEMO_CAPACITY: "The demo is full right now",
    DemoErrorCode.DEMO_NOT_READY: "The demo is not ready",
    DemoErrorCode.DEMO_READ_ONLY: "Not possible in the demo: it is read-only",
    DemoErrorCode.DEMO_REFUSED: "Not possible for a demo account",
}


class DemoError(ValueError):
    """A demo refusal: ``status_code`` and ``code`` from :class:`DemoErrorCode`, and a
    ``Retry-After`` where there is one.

    ``DemoError(DemoErrorCode.DEMO_RATE_LIMITED, retry_after=wait)`` answers 429
    ``{"detail": …, "code": "demo_rate_limited"}`` with ``Retry-After`` in whole seconds,
    rounded up (:func:`~eifi1_server_kit.limiter.retry_after_header`), through
    :func:`~eifi1_server_kit.errors.install_contract_error_handlers`; an app that maps it
    itself reads ``status_code``, ``code`` and :attr:`headers`. A ``ValueError`` like every
    kit refusal, so an app-wide ``ValueError`` → 400 handler does not swallow its status.
    """

    #: Replaced per instance from the code; the class default keeps every
    #: ``CONTRACT_ERRORS`` class carrying an ``int`` status.
    status_code: int = 403
    code: DemoErrorCode
    #: Seconds until a retry can succeed — the per-IP window's answer; ``None`` for no
    #: ``Retry-After``.
    retry_after: float | None

    def __init__(self, code: DemoErrorCode, detail: str | None = None, *, retry_after: float | None = None) -> None:
        self.code = DemoErrorCode(code)
        self.status_code = DEMO_ERROR_STATUS[self.code]
        self.retry_after = retry_after
        super().__init__(detail or DEMO_ERROR_DETAIL[self.code])

    @property
    def headers(self) -> dict[str, str]:
        """``{"Retry-After": …}`` when :attr:`retry_after` is set, else empty — the headers
        the answer carries."""
        if self.retry_after is None:
            return {}
        return {"Retry-After": retry_after_header(self.retry_after)}


def refuse_demo(user_is_demo: bool, what: str) -> None:
    """Refuse an action a demo user never may (§6.4): 403 ``demo_refused``, naming ``what``.

    The never-list: ways in and out (passkeys, API tokens, 2FA, E2EE enrolment, an email or
    password change, deletion, **the account export**), outside contact (feedback, support,
    push subscriptions), uploads, billed AI without the app's own budget, roles and money,
    and — model R — new top-level containers::

        refuse_demo(user.is_demo, "passkeys")

    **Call it on every such route, GET ones included**: layer 1 of model R
    (:func:`demo_write_allowed`) catches only writes, and ``GET /auth/me/export`` is a read.
    The app's demo test sends each route. Mail is the exception: skipped silently instead
    (a reset answers as usual), with :func:`is_demo_address`.
    """
    if user_is_demo:
        # "for a demo account", not "in the demo": it also refuses an admin acting ON a
        # demo account (kastlan's 0.31 adoption), and reads right from either side.
        raise DemoError(DemoErrorCode.DEMO_REFUSED, f"Not possible for a demo account: {what}")


# --- §6.3: model R, read-only ------------------------------------------------------------


_TEMPLATE_FIELD = re.compile(r"\{[^{}/]+\}")


@lru_cache(maxsize=256)
def _allow_rule(entry: str) -> tuple[str, re.Pattern[str]]:
    """``"DELETE /assistant/threads/{id}"`` → ``("DELETE", <regex>)``: ``{name}`` stands for
    exactly one path segment, everything else is literal."""
    method, _, path = entry.strip().partition(" ")
    path = path.strip()
    if not method.isalpha() or not path.startswith("/") or " " in path:
        raise ValueError(f"an allow-list entry is 'METHOD /path', with {{name}} for a segment: {entry!r}")
    pieces = _TEMPLATE_FIELD.split(path)
    pattern = "[^/]+".join(re.escape(piece) for piece in pieces)
    return method.upper(), re.compile(pattern)


def demo_write_allowed(method: str, path: str, allow: Collection[str] = frozenset()) -> bool:
    """May a demo user send this request? Layer 1 of model R's read-only (§6.3).

    The app's ``get_current_user`` asks, for a demo user, on every request — the one place
    every authenticated route passes::

        if user.is_demo and not demo_write_allowed(request.method, request.url.path, DEMO_WRITES):
            raise DemoError(DemoErrorCode.DEMO_READ_ONLY)

    * GET, HEAD and OPTIONS always may (:data:`READ_METHODS`) — and run in a read-only
      database transaction, layer 2, which is the app's (§6.3: Postgres ``SET TRANSACTION
      READ ONLY`` kept by an ``after_begin`` listener, so a write hidden in a GET fails at
      the database; keksdose's RLS is its layer 2).
    * Every other method only when ``allow`` names it: ``"METHOD /path"``, the path as the
      request has it (with the app's prefix — ``/api/v1/auth/logout``), and ``{name}`` for
      one path segment. **The list is the app's and empty by default** (§10.4): kastlan
      allows ``POST /api/v1/auth/logout``; keksdose has no logout route but lets a demo use
      its assistant (``POST /assistant/ask``, ``DELETE /assistant/threads/{id}``).

    A malformed entry is a :class:`ValueError` on the first write that reaches it — a
    programming error the app's demo test meets.
    """
    verb = method.strip().upper()
    if verb in READ_METHODS:
        return True
    for entry in allow:
        allowed_method, pattern = _allow_rule(entry)
        if allowed_method == verb and pattern.fullmatch(path):
            return True
    return False


# --- §5.1: the user ----------------------------------------------------------------------


_DOMAIN = re.compile(r"^(?=.{1,240}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_DEMO_ADDRESS = re.compile(rf"^{DEMO_LOCAL_PART}\+[0-9a-f]{{32}}@{DEMO_SUBDOMAIN}\.(?P<domain>.+)$")


def _checked_domain(domain: str) -> str:
    normalised = domain.strip().lower()
    if not _DOMAIN.match(normalised):
        raise ValueError(f"the app's own domain, like 'keksdose.app': {domain!r}")
    return normalised


def demo_address(domain: str) -> str:
    """A fresh demo user's address: ``demo+<32 hex>@demo.<domain>`` (§5.1; keksdose's and
    kastlan's ``DEMO_EMAIL_DOMAIN``).

    ``domain`` is the app's own (``keksdose.app``), never a reserved TLD: ``.invalid`` fails
    the email validation the user's answer goes through (keksdose's note). The ``demo.``
    subdomain has no MX record and no routing rule, so a mail sent by mistake reaches
    nobody; the 128 random bits keep every address unique without a lookup.
    """
    return f"{DEMO_LOCAL_PART}+{secrets.token_hex(16)}@{DEMO_SUBDOMAIN}.{_checked_domain(domain)}"


def is_demo_address(email: str, domain: str | None = None) -> bool:
    """Is this a demo user's address (:func:`demo_address`'s shape)? For the mail transports,
    which skip a demo silently (§6.4) — a reset for a demo address answers as usual and
    sends nothing.

    With ``domain``, only that app's demo addresses count. The address is normalised first,
    as every entry is.
    """
    match = _DEMO_ADDRESS.match(normalise_email(email))
    if match is None:
        return False
    return domain is None or match["domain"] == _checked_domain(domain)


def demo_password() -> str:
    """A random password nobody holds, for the demo user's ``NOT NULL`` hash (§5.1): 256
    bits, 43 URL-safe characters — inside bcrypt's 72 bytes. Never shown and never sent: the
    session the start answers with is the only way in."""
    return secrets.token_urlsafe(32)


# --- §5.1: the gate ----------------------------------------------------------------------


class DemoReaper(Protocol):
    """The app's reap (§5.7): delete at most ``limit`` demo users created before ``cutoff``,
    the oldest first; answer how many went.

    First delete or detach every row that would block the user's deletion (the app's own
    list), then the user. **Commit per user** — a savepoint inside the start's transaction,
    or a transaction of its own — so one bad row can't roll the batch back, and so a refusal
    after the reap (the cap, readiness) never undoes it. Log the count, never an address.
    The same function serves the app's scheduled job, with :func:`stale_cutoff`.
    """

    def __call__(self, *, cutoff: datetime, limit: int) -> Awaitable[int]: ...


class DemoCounter(Protocol):
    """The app's live count (§5.1 step 4): demo users created at or after ``cutoff`` — the
    live ones. A user the reap couldn't delete is not live, so it never holds a place for
    ever."""

    def __call__(self, *, cutoff: datetime) -> Awaitable[int]: ...


class DemoAdmission(NamedTuple):
    """What :meth:`DemoGate.admit` answers when the start may go on: mint the user now."""

    #: The instant the gate decided at. Create the user with ``created_at=now``, and its
    #: token ends at ``demo_expires_at(now, settings)``.
    now: datetime
    #: :func:`stale_cutoff` of ``now`` — what the reap and the count were given.
    cutoff: datetime
    #: How many stale demo users this start reaped, for the app's log line.
    reaped: int


class DemoGate:
    """``POST /auth/demo-session``'s checks in §5.1's order (kastlan's) — one instance per
    app, built in ``create_app`` beside the other limiters::

        admission = await app.state.demo_gate.admit(
            client_ip(request.headers, peer, trusted_hops=settings.trusted_proxy_hops),
            reap=lambda *, cutoff, limit: demo_service.reap(session, cutoff=cutoff, limit=limit),
            count_live=lambda *, cutoff: demo_service.count_live(session, cutoff=cutoff),
            is_ready=lambda: demo_service.is_ready(session),
        )
        user = await demo_service.mint(session, created_at=admission.now, locale=…)

    1. the switch is off → 404 ``demo_disabled``;
    2. the per-IP window (``Budget(5, 3600)`` from the settings) → 429
       ``demo_rate_limited`` with ``Retry-After``. Charged before any database work, so a
       flood is shed early;
    3. reap the stale demo users, at most :data:`REAP_PER_START` of the oldest — on EVERY
       start, so the live cap stays honest when the app's job is late;
    4. the live cap, counting only users younger than the maximum age → 429
       ``demo_capacity``, without ``Retry-After``;
    5. the demo data is missing → 503 ``demo_not_ready``. "Ready" means the data exists in
       ANY version (§10.2), so a deploy that changes the seed never answers 503 in between;
    6. → :class:`DemoAdmission`: the app mints the user and the session (201).

    Each refusal is a :class:`DemoError`. The callbacks get the cutoff as keywords, so the
    reap and the count split stale from live at one instant and no user is both or
    neither. ``now`` defaults to the current time in UTC; an app whose columns are naive
    passes its own. Async, because all three apps' sessions are; the callbacks are
    awaited in order and never concurrently.

    The window is process-local, like every limiter in the kit; the live cap, counted in the
    database, is the cross-instance bound.
    """

    def __init__(
        self,
        settings: DemoSettings,
        *,
        reap_limit: int = REAP_PER_START,
        clock: Clock = time.monotonic,
    ) -> None:
        if reap_limit < 1:
            raise ValueError("a start reaps at least one stale demo user")
        self._settings = settings
        self._reap_limit = reap_limit
        self._window = settings.demo_session_rate.limiter(clock)

    @property
    def settings(self) -> DemoSettings:
        return self._settings

    async def admit(
        self,
        ip: str,
        *,
        reap: DemoReaper,
        count_live: DemoCounter,
        is_ready: Callable[[], Awaitable[bool]],
        now: datetime | None = None,
    ) -> DemoAdmission:
        """Run §5.1's checks 1–5 for one start from ``ip``; raise the first refusal, or
        answer the :class:`DemoAdmission` to mint with."""
        settings = self._settings
        if not settings.demo_session_enabled:
            raise DemoError(DemoErrorCode.DEMO_DISABLED)
        wait = self._window.hit(ip)
        if wait is not None:
            raise DemoError(DemoErrorCode.DEMO_RATE_LIMITED, retry_after=wait)
        moment = datetime.now(UTC) if now is None else now
        cutoff = stale_cutoff(moment, settings)
        reaped = await reap(cutoff=cutoff, limit=self._reap_limit)
        if await count_live(cutoff=cutoff) >= settings.demo_session_max_live:
            raise DemoError(DemoErrorCode.DEMO_CAPACITY)
        if not await is_ready():
            raise DemoError(DemoErrorCode.DEMO_NOT_READY)
        return DemoAdmission(now=moment, cutoff=cutoff, reaped=reaped)

    def reset(self) -> None:
        """Empty the per-IP window — the test seam."""
        self._window.reset()
