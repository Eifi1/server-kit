"""An in-process sliding-window rate limiter: at most N hits per key per window.

keksdose ``backend/keksdose/infrastructure/rate_limit.py:45-124``
``SlidingWindowRateLimiter``, unchanged in behaviour — the lock, the blocked-hit rule and
the once-per-window sweep included. kastlan's copy (``infrastructure/rate_limit.py``) has
the same constructor and ``hit``/``reset``, so it swaps in as is.

**Process-local.** The window lives in this process's memory: each Cloud Run instance and
each uvicorn worker keeps its own counters (Kurvenschmiede's 2 workers make "20 an hour"
an effective 40). It bounds one client hammering one process — a browser stuck in a render
loop — not a cross-instance total. ``hit()`` is the whole contract, so a shared store
(Redis, a table) could replace the deques without touching a caller.

**No module globals here.** Build limiters per app — in ``create_app``, on
``app.state`` — so a test that builds an app per test starts with empty windows
(Kurvenschmiede ``infrastructure/security.py:288`` ``AuthLimiters`` / ``FeedbackLimiters``).
:class:`FeedbackLimiters` is that pair for the feedback routes.

**Whose address a window counts** is :func:`client_ip`'s answer: counted from the RIGHT of
``X-Forwarded-For``, as many hops as the app has proxies — never the left-most entry,
which the client writes itself.

**When to charge is the caller's choice.** Call ``hit()`` where a request should start
costing a slot:

* uploads — keksdose charges BEFORE the type/size checks (``upload_guards.throttle_attachment``),
  so a refused file uses a slot; the contract recommends AFTER validation;
* crashes — keksdose charges BEFORE the dedupe (``feedback_router.py:155``), so repeats
  count and ``occurrences`` stops rising after 20 an hour; the contract recommends AFTER
  the dedupe, charging only rows actually filed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping

from starlette.datastructures import Headers

__all__ = [
    "FEEDBACK_CRASHES_PER_HOUR",
    "FEEDBACK_UPLOADS_PER_HOUR",
    "UNKNOWN_CLIENT",
    "Clock",
    "FeedbackLimiters",
    "SlidingWindowRateLimiter",
    "client_ip",
    "retry_after_header",
]

Clock = Callable[[], float]


class SlidingWindowRateLimiter:
    """Per-key sliding window: at most ``max_hits`` allowed hits within any ``window_seconds``.

    ``clock`` is injectable so tests can drive time. ``max_hits <= 0`` or
    ``window_seconds <= 0`` disables the limiter (every hit allowed).
    """

    def __init__(self, *, max_hits: int, window_seconds: float, clock: Clock = time.monotonic) -> None:
        self._max = max_hits
        self._window = window_seconds
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        #: When the map was last swept of keys whose window has fully aged out (:meth:`_sweep`).
        self._last_sweep = clock()

    @property
    def max_hits(self) -> int:
        return self._max

    @property
    def window_seconds(self) -> float:
        return self._window

    @property
    def enabled(self) -> bool:
        return self._max > 0 and self._window > 0

    def hit(self, key: str) -> float | None:
        """Register an attempt for ``key``.

        ``None`` when it is permitted; otherwise the seconds until the oldest hit ages out
        (a ``Retry-After`` hint — see :func:`retry_after_header`). A blocked attempt is NOT
        recorded, so a client that keeps hammering does not push its own unblock time out.
        """
        if not self.enabled:
            return None
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._sweep(now, cutoff)
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                return max(hits[0] + self._window - now, 0.0)
            hits.append(now)
            return None

    def count(self, key: str) -> int:
        """How many hits ``key`` has inside the window now, without recording one.

        Never more than ``max_hits``, since a blocked hit is not recorded. A key never
        hit answers ``0`` and is not added to the map.
        """
        cutoff = self._clock() - self._window
        with self._lock:
            return sum(1 for moment in self._hits.get(key, ()) if moment > cutoff)

    def forget(self, key: str) -> None:
        """Drop ``key``'s window, as if it had never been hit. A sign-in that succeeds
        clears its address's failures this way (:class:`eifi1_server_kit.auth.LoginFailureThrottle`)."""
        with self._lock:
            self._hits.pop(key, None)

    def _sweep(self, now: float, cutoff: float) -> None:
        """Drop every key whose whole window has aged out. Caller holds the lock.

        ``_hits`` is a ``defaultdict``, so ``hit()`` creates an entry for every key it is
        asked about; without this the map grew with every distinct client the process had
        ever seen (keksdose ``rate_limit.py:95``). Once per window, not per hit: O(map) in
        the request path on every call would make a cheap guard a linear one.
        """
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        """Drop all windows — the test seam."""
        with self._lock:
            self._hits.clear()
            self._last_sweep = self._clock()


def retry_after_header(wait_seconds: float) -> str:
    """The ``Retry-After`` value for a ``hit()`` answer: whole seconds, rounded up past it
    (keksdose ``upload_guards.py:82``: ``str(int(retry_after) + 1)``)."""
    return str(int(wait_seconds) + 1)


#: What :func:`client_ip` answers when it has no address at all (keksdose ``client_ip``).
UNKNOWN_CLIENT = "unknown"


def client_ip(headers: Mapping[str, str], peer: str | None, *, trusted_hops: int) -> str:
    """The caller's address for a per-IP window, counted from the RIGHT of
    ``X-Forwarded-For`` (``docs/landing-demo-harmonization.md`` §5.1)::

        ip = client_ip(request.headers, request.client.host if request.client else None,
                       trusted_hops=settings.trusted_proxy_hops)

    **Why from the right.** Every proxy APPENDS the address it was connected from, so the
    right end of the header is written by the app's own proxies and the left end by the
    client — who may send any ``X-Forwarded-For`` it likes. The left-most entry (what
    keksdose's ``client_ip`` and Kurvenschmiede's ``caller_address`` read) is therefore the
    client's own choice: a random header per request is a fresh window per request, so the
    demo's five-an-hour per IP would not hold, and 500 scripted starts would fill the live
    cap and close the demo for a day. Counted from the right, the answer is the address the
    outermost trusted proxy saw, which the client cannot choose.

    ``trusted_hops`` is the number of proxies in front of the app that each append to the
    header (Caddy, a Cloud Run front end); it is the app's setting, per environment.
    **Measure it once**: send a request with ``X-Forwarded-For: 203.0.113.7`` and log the
    header that arrives — your own address sits ``trusted_hops`` entries from the right,
    and the fake one left of it. Then:

    * ``0`` — no proxy: the header is ignored and ``peer`` answers (local development, a
      directly exposed process);
    * ``N`` — the ``N``-th entry from the right;
    * a header with fewer than ``N`` entries did not pass every proxy counted, so nothing
      in it is vouched for, and ``peer`` answers — never the left-most entry. Every such
      request shares the peer's window, which is the safe error: it limits too much,
      never too little.

    Repeated ``X-Forwarded-For`` lines count as one comma-joined list, in order, when
    ``headers`` is Starlette's :class:`~starlette.datastructures.Headers` (a plain mapping
    holds one value per name, matched without regard to case). Empty entries are skipped.
    ``peer`` is the TCP peer (``request.client.host``); without one the answer is
    :data:`UNKNOWN_CLIENT`. The entry is returned as written — a limiter key, not a parsed
    address.
    """
    if trusted_hops < 0:
        raise ValueError("trusted_hops counts proxies: 0 or more")
    fallback = peer or UNKNOWN_CLIENT
    if trusted_hops == 0:
        return fallback
    if isinstance(headers, Headers):
        lines = headers.getlist("x-forwarded-for")
    else:
        lines = [value for name, value in headers.items() if name.lower() == "x-forwarded-for"]
    entries = [entry.strip() for line in lines for entry in line.split(",") if entry.strip()]
    if len(entries) < trusted_hops:
        _warn_short_header(len(entries), trusted_hops)
        return fallback
    return entries[-trusted_hops]


logger = logging.getLogger("eifi1_server_kit.limiter")
_short_header_warned = False


def _warn_short_header(entries: int, trusted_hops: int) -> None:
    """Say ONCE per process that a header was shorter than ``trusted_hops`` (0.5.1,
    keksdose). Every such request falls back to the peer, so a mis-measured hop count
    silently puts every caller — sign-in included — into one window; a log line is the
    only way an operator notices. Once, because after a bad measurement every request
    would repeat it. The count only, never an address."""
    global _short_header_warned
    if _short_header_warned:
        return
    _short_header_warned = True
    logger.warning(
        "X-Forwarded-For had %d entries, fewer than trusted_hops=%d: answering the peer. "
        "If this is not a direct request, re-measure trusted_hops.",
        entries,
        trusted_hops,
    )


#: The contract's budgets (§3.5, §3.6): 20 uploads and 20 crash reports per user per hour.
FEEDBACK_UPLOADS_PER_HOUR = 20
FEEDBACK_CRASHES_PER_HOUR = 20


class FeedbackLimiters:
    """The feedback routes' two budgets, keyed by user — one instance per app.

    ``upload`` answers a refusal with 429 + ``Retry-After``; ``crash`` answers with a 202
    ``stored: false``, never an error status (a crashed page must not collect failures).
    """

    def __init__(
        self,
        *,
        uploads_per_hour: int = FEEDBACK_UPLOADS_PER_HOUR,
        crashes_per_hour: int = FEEDBACK_CRASHES_PER_HOUR,
        clock: Clock = time.monotonic,
    ) -> None:
        self.upload = SlidingWindowRateLimiter(max_hits=uploads_per_hour, window_seconds=3600, clock=clock)
        self.crash = SlidingWindowRateLimiter(max_hits=crashes_per_hour, window_seconds=3600, clock=clock)

    def reset(self) -> None:
        self.upload.reset()
        self.crash.reset()
