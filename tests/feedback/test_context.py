"""The ``context`` object (contract §3.2): identity is the server's, and the size is capped."""

from __future__ import annotations

from eifi1_server_kit.feedback import CONTEXT_KEYS, MAX_CONTEXT_SIZE, context_size, stamp_identity


def test_the_server_overwrites_who_filed_it() -> None:
    """Kurvenschmiede ``feedback_service.py:165``: a client cannot file in someone else's name."""
    sent = {"route": "/x", "user_id": 99, "user_email": "someone@else.ch", "user_display_name": "Mallory"}
    stamped = stamp_identity(sent, user_id=7, user_email="a@b.ch", user_display_name="Erika")
    assert stamped == {"route": "/x", "user_id": 7, "user_email": "a@b.ch", "user_display_name": "Erika"}
    assert sent["user_id"] == 99, "a new dict, never a mutation"
    assert stamp_identity(None, user_id=1, user_email=None, user_display_name=None) == {
        "user_id": 1,
        "user_email": None,
        "user_display_name": None,
    }


def test_the_size_is_kurvenschmiedes_measure() -> None:
    assert context_size(None) == 0
    assert context_size({"a": "ü"}) == len('{"a": "ü"}')
    assert MAX_CONTEXT_SIZE == 8192
    assert len(CONTEXT_KEYS) == 10 and "environment" in CONTEXT_KEYS
