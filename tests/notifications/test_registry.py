"""The types and the registry (notifications §2.1, §3.1): each app declares its own."""

from __future__ import annotations

import dataclasses

import pytest

from eifi1_server_kit.notifications import TYPE_ID_PATTERN, NotificationType, NotificationTypes
from tests.notifications._types import KASTLAN, KEKSDOSE, KURVENSCHMIEDE


def test_a_type_has_its_defaults_and_is_held_by_quiet_hours_unless_said() -> None:
    support = NotificationType("support_chat", app=True, quiet=False)
    assert (support.id, support.app, support.email, support.quiet) == ("support_chat", True, None, False)
    digest = NotificationType("digest", app=False)
    assert digest.email is None and digest.quiet is True, "machine-sent by default"
    answered = KURVENSCHMIEDE["feedback_answered"]
    assert (answered.app, answered.email) == (None, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        support.app = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "type_id",
    ["", "a", "1digest", "_digest", "Digest", "share-events", "share events", "digest\n", "a" * 41, "dïgest"],
)
def test_a_type_id_outside_the_pattern_is_refused(type_id: str) -> None:
    with pytest.raises(ValueError, match="type id matches"):
        NotificationType(type_id, app=True)


def test_the_pattern_fits_keksdoses_column() -> None:
    assert TYPE_ID_PATTERN == r"^[a-z][a-z0-9_]{1,39}$"
    assert NotificationType("a" * 40, app=True).id == "a" * 40
    assert NotificationType("ab", app=True).id == "ab"


def test_a_type_with_neither_channel_is_refused() -> None:
    with pytest.raises(ValueError, match="offers no channel"):
        NotificationType("silent", app=None)
    with pytest.raises(ValueError, match="offers no channel"):
        NotificationType("silent", app=None, email=None)


def test_the_registry_keeps_the_apps_order() -> None:
    assert KEKSDOSE.ids == (
        "recurring_posted",
        "overspend",
        "scan_done",
        "share_events",
        "low_balance",
        "digest",
        "announcements",
        "support_chat",
        "scheduled_due",
    )
    assert [item.id for item in KASTLAN] == ["invoices_overdue", "dunnings_pending"]
    assert len(KEKSDOSE) == 9 and len(KURVENSCHMIEDE) == 3


def test_the_registry_answers_ids() -> None:
    assert "digest" in KEKSDOSE
    assert "feedback_answered" not in KEKSDOSE
    assert KEKSDOSE["digest"] == NotificationType("digest", app=False)
    assert 42 not in KEKSDOSE and None not in KEKSDOSE
    assert ["digest"] not in KEKSDOSE, "an unhashable value is simply not an id"
    with pytest.raises(KeyError):
        KEKSDOSE["feedback_answered"]


def test_a_doubled_id_is_refused() -> None:
    with pytest.raises(ValueError, match="'digest' is declared twice"):
        NotificationTypes(NotificationType("digest", app=False), NotificationType("digest", app=True))


def test_receivable_narrows_in_the_same_order() -> None:
    """kastlan: staff in any company receive both, a tenant neither."""
    staff = KASTLAN.receivable(lambda _type: True)
    assert isinstance(staff, NotificationTypes) and staff.ids == KASTLAN.ids
    tenant = KASTLAN.receivable(lambda _type: False)
    assert tenant.ids == () and len(tenant) == 0 and list(tenant) == []
    quiet_only = KEKSDOSE.receivable(lambda item: item.quiet)
    assert quiet_only.ids == ("recurring_posted", "overspend", "scan_done", "low_balance", "digest", "scheduled_due")


def test_the_registry_reads_as_its_types() -> None:
    assert repr(KASTLAN) == (
        "NotificationTypes(NotificationType(id='invoices_overdue', app=False, email=False, quiet=True), "
        "NotificationType(id='dunnings_pending', app=False, email=None, quiet=True))"
    )
    assert repr(NotificationTypes()) == "NotificationTypes()"
