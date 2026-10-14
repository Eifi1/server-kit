"""The refusals the feedback rules raise — plain exceptions, never ``HTTPException``.

Every class is a :class:`ValueError` (so a ``ValueError`` raised inside a Pydantic
validator still becomes a 422 there) and carries the HTTP status the contract gives it
as ``status_code`` (§3.7). Each app maps them itself — Kurvenschmiede answers a foreign
row with 404 where keksdose says 403, and kastlan routes everything through its
``DomainError`` handler — so the class says WHAT went wrong and the app says how loudly.

Typical mapping::

    except FeedbackError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

and, for whatever no endpoint caught,
:func:`eifi1_server_kit.errors.install_contract_error_handlers` — without it an app-wide
``ValueError`` handler answers these with its own status.

Layer 2 (:mod:`eifi1_server_kit.feedback.service`, ``feedback.router``) raises four more:
:class:`FeedbackNotFoundError` and :class:`AttachmentNotFoundError` (404),
:class:`FeedbackThrottledError` (429 with ``Retry-After``) and, since 0.11,
:class:`FeedbackConflictError` (409). The router raises no
``HTTPException`` of its own; the installer answers all of them.
"""

from __future__ import annotations

from eifi1_server_kit.limiter import retry_after_header

#: The upload throttle's sentence, keksdose's (``upload_guards.py:97``); its support door
#: raises it too, through :func:`eifi1_server_kit.feedback.throttle_upload`.
THROTTLED_DETAIL = "Too many attachments uploaded — please try again later."
#: The comment and vote windows' sentences (§9.2 decision 19), on the same 429.
COMMENTS_THROTTLED_DETAIL = "Too many comments — please try again later."
VOTES_THROTTLED_DETAIL = "Too many votes — please try again later."


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
    title / body / category; an author editing a row outside OPEN / READY / IN_PROGRESS
    without it being a rework append.
    """

    status_code = 403


class FeedbackNotFoundError(FeedbackError):
    """No such report, or one this actor may not see (→ 404, "Feedback not found").

    Also what a foreign row answers where the app chose ``foreign_row="not_found"``
    (Kurvenschmiede): indistinguishable from a missing one (server-services §2.10).
    """

    status_code = 404

    def __init__(self, detail: str = "Feedback not found") -> None:
        super().__init__(detail)


class FeedbackConflictError(FeedbackError):
    """The request contradicts the report's state (→ 409): the author taking back the vote
    their report is (§9.6, "Your report counts as your vote")."""

    status_code = 409


class AttachmentNotFoundError(FeedbackError):
    """A key outside the app's pattern, a file the store does not have, or one this
    viewer may not read (→ 404, "Attachment not found")."""

    status_code = 404

    def __init__(self, detail: str = "Attachment not found") -> None:
        super().__init__(detail)


class FeedbackThrottledError(FeedbackError):
    """The upload window is spent (→ 429, contract §3.5): :data:`THROTTLED_DETAIL` with a
    ``Retry-After`` in whole seconds, rounded up, as :class:`~eifi1_server_kit.demo.DemoError`
    carries one."""

    status_code = 429

    def __init__(self, retry_after: float, detail: str = THROTTLED_DETAIL) -> None:
        #: Seconds until the oldest hit leaves the window — the limiter's ``hit()`` answer.
        self.retry_after = retry_after
        super().__init__(detail)

    @property
    def headers(self) -> dict[str, str]:
        """``{"Retry-After": …}`` — the header the answer carries."""
        return {"Retry-After": retry_after_header(self.retry_after)}
