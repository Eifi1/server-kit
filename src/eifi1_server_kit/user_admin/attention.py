"""What waits on the admin: ``GET /admin/attention`` (``docs/admin-harmonization.md`` §2.4
decision 14, §6.1–§6.4 in ``Eifi1/ui-kit``).

The overview's tiles and the account menu's badge read one answer. The kit ships its shape
and the demo's; every count is the app's, made in its own SQL over its own tables, and
passed in — kastlan's company scope, keksdose's support queue and Kurvenschmiede's own
invitations service stay in the app.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import BaseModel, Field

__all__ = ["AdminAttention", "AttentionCount"]

#: One tile's count: ``None`` where the app has no such thing, or the viewer cannot act on it.
AttentionCount = Annotated[int, Field(ge=0)] | None


class AdminAttention(BaseModel):
    """``GET /admin/attention`` → ``{feedback_waiting, deletion_requests,
    invitations_pending, users_total, users_new_7d, extra: {key: count}}`` (§2.4, §6.4).

    Each count is ``None`` where the app has no such thing, or where this viewer cannot act
    on it; the page shows no tile for a ``None``, and a ``0`` as a quiet tile. The rules:

    * ``feedback_waiting`` is "To triage" (§6.1): reports in ``OPEN``, crash reports
      included, scoped to what this admin can see (kastlan: the acting company). The
      developer's lanes (``READY``, ``REWORK``, ``IN_PROGRESS``) are not the admin's.
    * ``deletion_requests`` only where this viewer acts on them (§6.2): Kurvenschmiede
      counts them; keksdose (they run on their own) and kastlan's ``/admin`` answer ``None``.
    * **Demo accounts never count, anywhere** (§6.4): not as authors of a report, not as
      users, not as new users, not in an ``extra``.
    * **In a demo session every count is ``None``** (§6.4; kastlan's demo user is a
      company admin): answer :meth:`for_demo`, or pass ``demo=`` to :meth:`of`.
    * ``extra`` holds the app's own tiles, keyed as its overview names them (keksdose
      ``support_waiting``; Kurvenschmiede ``released_for_work``).

    The badge's sum is each app's (§6.3: only work that waits on this admin); invitations
    and users never join it.
    """

    feedback_waiting: AttentionCount = None
    deletion_requests: AttentionCount = None
    invitations_pending: AttentionCount = None
    users_total: AttentionCount = None
    users_new_7d: AttentionCount = None
    extra: dict[str, AttentionCount] = Field(default_factory=dict)

    @classmethod
    def for_demo(cls, *extra_keys: str) -> Self:
        """The demo's answer: every count ``None``, the app's ``extra`` keys kept (as
        ``None``) so the shape is the one the page knows."""
        return cls(extra=dict.fromkeys(extra_keys))

    @classmethod
    def of(
        cls,
        *,
        feedback_waiting: int | None = None,
        deletion_requests: int | None = None,
        invitations_pending: int | None = None,
        users_total: int | None = None,
        users_new_7d: int | None = None,
        extra: Mapping[str, int | None] | None = None,
        demo: bool = False,
    ) -> Self:
        """The answer from the app's counts, each passed in (§6.4: the builder takes every
        count). ``demo=True`` (the viewer's session is the demo's) answers
        :meth:`for_demo` with ``extra``'s keys, whatever the counts say."""
        extras = dict(extra or {})
        if demo:
            return cls.for_demo(*extras)
        return cls(
            feedback_waiting=feedback_waiting,
            deletion_requests=deletion_requests,
            invitations_pending=invitations_pending,
            users_total=users_total,
            users_new_7d=users_new_7d,
            extra=extras,
        )
