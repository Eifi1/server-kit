"""``GET /admin/attention`` (admin-harmonization §2.4, §6.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eifi1_server_kit.user_admin import AdminAttention

KEYS = {"feedback_waiting", "deletion_requests", "invitations_pending", "users_total", "users_new_7d", "extra"}


def test_the_default_is_no_tile_at_all() -> None:
    body = AdminAttention().model_dump()
    assert set(body) == KEYS
    assert body == dict.fromkeys(KEYS - {"extra"}) | {"extra": {}}


def test_the_extra_default_is_not_shared() -> None:
    first = AdminAttention()
    first.extra["support_waiting"] = 1
    assert AdminAttention().extra == {}


def test_of_passes_every_count_through() -> None:
    body = AdminAttention.of(
        feedback_waiting=3,
        deletion_requests=None,
        invitations_pending=0,
        users_total=120,
        users_new_7d=4,
        extra={"support_waiting": 2, "jobs_failing": None},
    )
    assert body.model_dump(mode="json") == {
        "feedback_waiting": 3,
        "deletion_requests": None,
        "invitations_pending": 0,
        "users_total": 120,
        "users_new_7d": 4,
        "extra": {"support_waiting": 2, "jobs_failing": None},
    }


def test_of_without_extra_answers_an_empty_one() -> None:
    assert AdminAttention.of(feedback_waiting=1).extra == {}


def test_a_demo_session_answers_every_count_none_and_keeps_the_extra_keys() -> None:
    body = AdminAttention.of(
        feedback_waiting=3, users_total=9, extra={"released_for_work": 5, "demo_accounts_live": 2}, demo=True
    )
    assert body == AdminAttention.for_demo("released_for_work", "demo_accounts_live")
    assert body.model_dump() == dict.fromkeys(KEYS - {"extra"}) | {
        "extra": {"released_for_work": None, "demo_accounts_live": None}
    }


def test_for_demo_without_keys() -> None:
    assert AdminAttention.for_demo() == AdminAttention()


@pytest.mark.parametrize("field", sorted(KEYS - {"extra"}))
def test_a_count_is_never_negative(field: str) -> None:
    with pytest.raises(ValidationError):
        AdminAttention.model_validate({field: -1})


def test_an_extra_count_is_never_negative() -> None:
    with pytest.raises(ValidationError):
        AdminAttention.of(extra={"support_waiting": -1})


def test_it_reads_its_own_json_back() -> None:
    body = AdminAttention.of(feedback_waiting=1, extra={"support_waiting": 0})
    assert AdminAttention.model_validate_json(body.model_dump_json()) == body
