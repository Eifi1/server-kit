"""A session's life on the server: a shared computer's is short, and every sign-out ends it.

``docs/auth-harmonization.md`` §6.2 and §6.3 in ``Eifi1/ui-kit`` (Marcel, 2026-11-12).

**Why.** ui-kit 0.61 keeps a shared computer's tokens in ``sessionStorage``, to end with the
tab. But Chrome and Firefox restore a closed tab WITH its ``sessionStorage`` (Ctrl+Shift+T,
session restore), so the next person at a library PC could reopen it signed in, holding a
refresh token good for weeks: every refresh re-mints the full 30 days (kastlan's finding).
So the server ends a shared session on its own clock, whatever the browser keeps.

**The policy.** :class:`SessionPolicy`, one per app (its lifetimes are the app's settings):

* a **shared** session (:class:`~eifi1_server_kit.auth.LoginRequest`'s ``shared_device``)
  ends :data:`SHARED_IDLE` (15 minutes) after its last refresh, and
  :data:`SHARED_ABSOLUTE` (4 hours) after it started, however busy;
* a **normal** session keeps what the apps do today: it ends ``refresh_lifetime`` (30 days)
  after its last refresh — when its refresh token would — and never for its age.

Ended AT the limit, as a token's ``exp`` and :func:`~eifi1_server_kit.auth.is_expired`
are. A ``now`` before ``last_active`` or ``started_at`` (two clocks a little apart) is idle
zero, never an ended session; the absolute limit still holds.

**The idle limit counts from the last refresh**, which ``/auth/refresh`` stamps (kastlan's
``touch_session``), so no request has to write. That is why a shared session's access
token is SHORTER than the idle limit (:data:`SHARED_ACCESS_LIFETIME`, 5 minutes): the
client refreshes on the first 401 after the access token ends (ui-kit's
``attachSessionRefresh``), and only an access token that ends well before the refresh
token lets somebody at work refresh in time. kastlan's 15-minute access token would end
with its refresh token, and sign them out every quarter of an hour. So a session left
alone ends 10 to 15 minutes after its last request, and somebody at work refreshes about
every five.

**Three call sites in the app**, each with the row's ``shared_device``, ``started_at`` (its
``created_at``) and ``last_active``:

1. **minting a pair** — at sign-in and at every refresh:
   ``access_claims(…, lifetime=access_lifetime(policy, shared=…, started_at=…, now=now))``
   and ``refresh_claims(…, lifetime=refresh_lifetime(policy, …))``; neither outlives the
   session's absolute end, and the refresh token not its idle limit;
2. **``/auth/refresh``** — refuse with ``AuthError(AuthErrorCode.SESSION_ENDED)`` when
   :func:`session_expired`, else stamp ``last_active = now`` and
   ``expires_at = session_expires_at(…, last_active=now)``;
3. **every request** (``get_current_user``) — refuse with the route's 401 when
   :func:`session_expired` (or ``expires_at <= now`` in SQL), as for a revoked row.

**Every sign-out ends the session on the server**, shared or not: the client calls the
app's revoke for its own session BEFORE it drops its tokens, and a sign-out that cannot
reach the server still signs out, leaving the limits above to end it. The session table and
the routes stay in the app; this module only decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from eifi1_server_kit.auth.tokens import ACCESS_TOKEN_LIFETIME, REFRESH_TOKEN_LIFETIME, _aware

__all__ = [
    "SHARED_ABSOLUTE",
    "SHARED_ACCESS_LIFETIME",
    "SHARED_IDLE",
    "SessionPolicy",
    "access_lifetime",
    "refresh_lifetime",
    "session_expired",
    "session_expires_at",
]

#: A shared session ends this long after its last refresh (Marcel, 2026-11-12).
SHARED_IDLE = timedelta(minutes=15)
#: A shared session ends this long after it started, however busy (Marcel, 2026-11-12).
SHARED_ABSOLUTE = timedelta(hours=4)
#: A shared session's access token: a third of the idle limit, so the client's refresh —
#: on the first 401 after the access token ends — finds the refresh token alive. A session
#: left alone ends 10 to 15 minutes after its last request.
SHARED_ACCESS_LIFETIME = timedelta(minutes=5)

_ZERO = timedelta(0)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """How long a session lives on the server, normal and shared — one per app.

    The two normal lifetimes are the app's settings (kastlan:
    ``SessionPolicy(access_lifetime=timedelta(minutes=15))``); the shared limits are the
    contract's, Marcel's decision. A policy that cannot work is refused: a shared access
    token as long as the idle limit would end with its refresh token.
    """

    #: A normal session's access token — the app's ``access_token_expire_minutes``.
    access_lifetime: timedelta = ACCESS_TOKEN_LIFETIME
    #: A normal session's refresh token, and how long a normal session lives unused.
    refresh_lifetime: timedelta = REFRESH_TOKEN_LIFETIME
    #: A shared session ends this long after its last refresh.
    shared_idle: timedelta = SHARED_IDLE
    #: A shared session ends this long after it started.
    shared_absolute: timedelta = SHARED_ABSOLUTE
    #: A shared session's access token at most; shorter than ``shared_idle``.
    shared_access_lifetime: timedelta = SHARED_ACCESS_LIFETIME

    def __post_init__(self) -> None:
        for name in ("access_lifetime", "refresh_lifetime", "shared_idle", "shared_absolute", "shared_access_lifetime"):
            if getattr(self, name) <= _ZERO:
                raise ValueError(f"{name} must be positive")
        if self.shared_access_lifetime >= self.shared_idle:
            raise ValueError(
                "shared_access_lifetime must be shorter than shared_idle: the client refreshes only "
                "after the access token ends, and the refresh token ends at the idle limit"
            )
        if self.shared_idle > self.shared_absolute:
            raise ValueError("shared_idle cannot be longer than shared_absolute")


def session_expires_at(policy: SessionPolicy, *, shared: bool, started_at: datetime, last_active: datetime) -> datetime:
    """When the session ends unless it is refreshed first — the ``expires_at`` column, written
    with ``last_active`` at sign-in and at every refresh.

    Shared: the earlier of ``last_active + shared_idle`` and ``started_at +
    shared_absolute``. Normal: ``last_active + refresh_lifetime``, when its refresh token
    would end. Naive datetimes are read as UTC (SQLite); the answer is aware.
    """
    idle_end = _aware(last_active) + (policy.shared_idle if shared else policy.refresh_lifetime)
    if not shared:
        return idle_end
    return min(idle_end, _aware(started_at) + policy.shared_absolute)


def session_expired(
    policy: SessionPolicy, *, shared: bool, started_at: datetime, last_active: datetime, now: datetime
) -> bool:
    """Has the session ended at ``now``? ``True`` AT the limit (:func:`session_expires_at`).

    Ask it at ``/auth/refresh`` (``session_ended``) and on every request, with the row's
    ``shared_device``, ``created_at`` and ``last_active``. A ``now`` before either instant
    (another clock, a little behind) ends nothing the absolute limit does not.
    """
    return _aware(now) >= session_expires_at(policy, shared=shared, started_at=started_at, last_active=last_active)


def _within_the_end(lifetime: timedelta, policy: SessionPolicy, started_at: datetime, now: datetime) -> timedelta:
    """``lifetime``, cut to what is left of a shared session at ``now``; never negative."""
    return max(_ZERO, min(lifetime, _aware(started_at) + policy.shared_absolute - _aware(now)))


def refresh_lifetime(policy: SessionPolicy, *, shared: bool, started_at: datetime, now: datetime) -> timedelta:
    """The ``lifetime`` of a refresh token minted at ``now`` (``refresh_claims``).

    Shared: ``shared_idle``, so a token nobody uses ends with the idle limit, and never past
    ``started_at + shared_absolute`` — zero there and after, a token that is dead when
    minted (ask :func:`session_expired` first). Normal: ``refresh_lifetime``.
    """
    if not shared:
        return policy.refresh_lifetime
    return _within_the_end(policy.shared_idle, policy, started_at, now)


def access_lifetime(policy: SessionPolicy, *, shared: bool, started_at: datetime, now: datetime) -> timedelta:
    """The ``lifetime`` of an access token minted at ``now`` (``access_claims``).

    Shared: the app's access lifetime, at most ``shared_access_lifetime`` — shorter than
    the refresh token, so the client's refresh finds it alive — and never past the absolute
    end. Normal: ``access_lifetime``. A demo's access token is the app's own business
    (``demo_expires_at``): a demo is never shared.
    """
    if not shared:
        return policy.access_lifetime
    return _within_the_end(min(policy.access_lifetime, policy.shared_access_lifetime), policy, started_at, now)
