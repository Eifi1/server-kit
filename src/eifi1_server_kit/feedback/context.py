"""The reporter's ``context`` object (contract §3.2): its size cap and who it names.

The dialog writes ``url``, ``route``, ``origin``, ``environment``, ``user_id``,
``user_email``, ``user_display_name``, ``viewport``, ``ua`` and ``version``; every key is
optional on read. Two recommendations of the contract live here as code, both from
Kurvenschmiede: identity is the SERVER's — it overwrites the three ``user_*`` keys from
the session, so a client cannot file in someone else's name — and the object's size is
capped, so it is not a free-form blob somebody else's request writes into a column.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: The ten keys the dialog writes (kit ``feedbackContext``; keksdose ``use-feedback-dialog.tsx:105``).
CONTEXT_KEYS: tuple[str, ...] = (
    "url",
    "route",
    "origin",
    "environment",
    "user_id",
    "user_email",
    "user_display_name",
    "viewport",
    "ua",
    "version",
)

#: The cap on a sent ``context``, measured as Kurvenschmiede measures it
#: (``schemas/feedback.py:69`` ``MAX_CONTEXT_BYTES``, ``:144``): the length of
#: ``json.dumps(context, ensure_ascii=False)``. The ten keys fit in a fraction of it.
MAX_CONTEXT_SIZE = 8 * 1024


def context_size(context: Mapping[str, Any] | None) -> int:
    """The size :data:`MAX_CONTEXT_SIZE` is compared with (``0`` for no context)."""
    if context is None:
        return 0
    return len(json.dumps(context, ensure_ascii=False, default=str))


def stamp_identity(
    context: Mapping[str, Any] | None,
    *,
    user_id: int | str | None,
    user_email: str | None,
    user_display_name: str | None,
) -> dict[str, Any]:
    """The sent context with ``user_id`` / ``user_email`` / ``user_display_name`` written by
    the server from the session (Kurvenschmiede ``feedback_service.py:165`` ``stamped_context``).

    Everything else — the page, the browser, the build — is the client's to know. Returns a
    NEW dict: a plain JSON column has no mutation tracking (keksdose
    ``feedback_service.py:323-327``).
    """
    return {
        **(context or {}),
        "user_id": user_id,
        "user_email": user_email,
        "user_display_name": user_display_name,
    }
