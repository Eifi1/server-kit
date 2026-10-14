"""The clock layer 2's services read: an aware UTC ``datetime``.

``docs/server-services-harmonization.md`` §6.2.1 in ``Eifi1/ui-kit``. The feedback service
(0.9.0) and the translation review (0.10.0) take the same clock, so it lives here, and
:mod:`eifi1_server_kit.feedback` re-exports the same objects. A test passes its own
(``lambda: moment``); an app passes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

__all__ = ["Clock", "utc_now"]

#: A service's clock: an aware UTC ``datetime`` (feedback's ``resolved_at`` and a crash's
#: ``seen_at``, a verdict's ``reviewed_at``). Not :data:`eifi1_server_kit.limiter.Clock`,
#: which is a monotonic float.
type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The current time, aware, in UTC — the default :data:`Clock`."""
    return datetime.now(UTC)
