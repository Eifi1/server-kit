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

**When to charge is the caller's choice.** Call ``hit()`` where a request should start
costing a slot:

* uploads — keksdose charges BEFORE the type/size checks (``upload_guards.throttle_attachment``),
  so a refused file uses a slot; the contract recommends AFTER validation;
* crashes — keksdose charges BEFORE the dedupe (``feedback_router.py:155``), so repeats
  count and ``occurrences`` stops rising after 20 an hour; the contract recommends AFTER
  the dedupe, charging only rows actually filed.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

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
