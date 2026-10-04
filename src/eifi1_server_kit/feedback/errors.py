"""The refusals the feedback rules raise — plain exceptions, never ``HTTPException``.

Every class is a :class:`ValueError` (so a ``ValueError`` raised inside a Pydantic
validator still becomes a 422 there) and carries the HTTP status the contract gives it
as ``status_code`` (§3.7). Each app maps them itself — Kurvenschmiede answers a foreign
row with 404 where keksdose says 403, and kastlan routes everything through its
``DomainError`` handler — so the class says WHAT went wrong and the app says how loudly.

Typical mapping::

    except FeedbackError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
"""

from __future__ import annotations


class FeedbackError(ValueError):
    """Base class for every refusal in :mod:`eifi1_server_kit.feedback`."""

    status_code: int = 422


class FeedbackValidationError(FeedbackError):
    """A value the payload may not carry, whoever sends it (→ 422)."""

    status_code = 422


class CrashCategoryNotAssignableError(FeedbackValidationError):
    """CRASH was set by hand on a create or an update (keksdose ``feedback_service.py:19``)."""


class UnknownAttachmentUrlError(FeedbackValidationError):
    """A ``screenshot_url`` / ``attachment_urls`` entry that is not this app's own attachment URL."""


class FeedbackForbiddenError(FeedbackError):
    """The actor may not make this change (→ 403; keksdose ``FeedbackUpdateForbiddenError``).

    Raised for: not the author and not an admin; an author changing a field outside
    title / body / category; an author editing a row outside OPEN / IN_PROGRESS without
    it being a rework append.
    """

    status_code = 403
