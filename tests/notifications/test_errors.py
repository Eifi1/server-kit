"""The notifications' coded refusals, answered at their status with their code."""

from __future__ import annotations

import json

import pytest

from eifi1_server_kit.errors import CONTRACT_ERRORS, contract_error_response
from eifi1_server_kit.notifications import (
    NotificationError,
    NotificationTypeError,
    PushEndpointTakenError,
    UnsubscribeTokenError,
)


def test_every_refusal_is_a_value_error_with_a_status_and_a_code() -> None:
    assert NotificationError in CONTRACT_ERRORS
    for error, status, code in (
        (NotificationError(), 422, "notification_invalid"),
        (NotificationTypeError(["digest"]), 422, "notification_type_not_received"),
        (PushEndpointTakenError(), 409, "push_endpoint_taken"),
        (UnsubscribeTokenError(), 400, "unsubscribe_invalid"),
    ):
        assert isinstance(error, ValueError) and isinstance(error, NotificationError)
        assert (error.status_code, error.code) == (status, code)
        assert str(error)


def test_a_detail_says_more() -> None:
    assert str(PushEndpointTakenError("Taken")) == "Taken"
    assert str(NotificationTypeError(["digest"], "Not for you")) == "Not for you"
    assert str(NotificationTypeError(("digest", "scan_done"))) == (
        "This account doesn't receive these notifications: digest, scan_done"
    )


@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (
            NotificationTypeError(["dunnings_pending"]),
            422,
            {
                "detail": "This account doesn't receive these notifications: dunnings_pending",
                "code": "notification_type_not_received",
                "types": ["dunnings_pending"],
            },
        ),
        (
            PushEndpointTakenError(),
            409,
            {"detail": PushEndpointTakenError.default_detail, "code": "push_endpoint_taken"},
        ),
        (
            UnsubscribeTokenError(),
            400,
            {"detail": "This unsubscribe link is not valid", "code": "unsubscribe_invalid"},
        ),
    ],
)
async def test_the_answer_carries_the_code(error: NotificationError, status: int, body: dict[str, object]) -> None:
    response = await contract_error_response(None, error)  # type: ignore[arg-type]
    assert response.status_code == status
    assert json.loads(bytes(response.body)) == body
