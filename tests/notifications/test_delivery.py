"""Who gets what, for one recipient of one type (notifications §3.3)."""

from __future__ import annotations

from typing import Any

import pytest

from eifi1_server_kit.notifications import (
    Delivery,
    NotificationChannelError,
    NotificationType,
    NotificationTypeError,
    TypeSwitches,
    apply_muted_types,
    device_receives,
    plan_delivery,
    read_muted_types,
)
from tests.notifications._types import KASTLAN, KEKSDOSE, KURVENSCHMIEDE

INVOICES = KASTLAN["invoices_overdue"]  # app and e-mail, quiet
SUPPORT = KEKSDOSE["support_chat"]  # app, not quiet
ANSWERED = KURVENSCHMIEDE["feedback_answered"]  # e-mail only
DUE = KEKSDOSE["scheduled_due"]  # app and push, quiet

ON = TypeSwitches(app=True, email=True)
OFF = TypeSwitches(app=False, email=False)
PUSH_ON = TypeSwitches(app=True, email=None, push=True)


def _plan(notification_type: NotificationType, switches: TypeSwitches, **overrides: Any) -> Delivery:
    context: dict[str, Any] = {
        "active": True,
        "is_actor": False,
        "quiet_now": False,
        "email_verified": True,
        "keeps_inbox": True,
    }
    return plan_delivery(notification_type, switches, **(context | overrides))


def test_nothing_is_none_of_the_three() -> None:
    assert Delivery(inbox=False, push=False, email=False) == Delivery.NONE


@pytest.mark.parametrize("who", [{"active": False}, {"is_actor": True}, {"active": False, "service": True}])
def test_step_1_nobody_inactive_and_nobody_who_acted(who: dict[str, bool]) -> None:
    """A deactivated account, a demo, the event's own author: nothing, a service notice included."""
    assert _plan(INVOICES, ON, **who) is Delivery.NONE


def test_step_2_the_app_channel_is_an_inbox_row() -> None:
    assert _plan(INVOICES, TypeSwitches(app=True, email=False)) == Delivery(inbox=True, push=False, email=False)
    assert _plan(INVOICES, TypeSwitches(app=True, email=False), keeps_inbox=False) == Delivery(
        inbox=False, push=False, email=False
    ), "kastlan keeps no inbox, and has no push"


def test_step_2_push_follows_its_own_switch_not_app() -> None:
    """§10.2 rule 2 (0.16): push on with in-app off sends the push with no inbox row."""
    assert _plan(DUE, TypeSwitches(app=False, email=None, push=True)) == Delivery(inbox=False, push=True, email=False)
    assert _plan(DUE, TypeSwitches(app=True, email=None, push=False)) == Delivery(inbox=True, push=False, email=False)
    assert _plan(DUE, TypeSwitches(app=True, email=None, push=True)) == Delivery(inbox=True, push=True, email=False)


def test_step_2_quiet_hours_hold_the_push_of_a_quiet_type_only() -> None:
    assert _plan(DUE, PUSH_ON, quiet_now=True) == Delivery(inbox=True, push=False, email=False), "the row stays"
    assert _plan(INVOICES, ON, quiet_now=True) == Delivery(inbox=True, push=False, email=True), "mail goes as usual"
    assert _plan(SUPPORT, TypeSwitches(app=True, email=None, push=True), quiet_now=True) == Delivery(
        inbox=True, push=True, email=False
    ), "a person-sent type is never held"


def test_step_3_mail_goes_only_to_a_verified_address() -> None:
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True)) == Delivery(inbox=False, push=False, email=True)
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True), email_verified=False) == Delivery.NONE


def test_step_4_all_off_is_nothing_not_even_an_inbox_row() -> None:
    assert _plan(INVOICES, OFF) == Delivery.NONE
    assert _plan(DUE, TypeSwitches(app=False, email=None, push=False)) == Delivery.NONE
    assert _plan(ANSWERED, TypeSwitches(app=None, email=False)) == Delivery.NONE


def test_a_switch_the_type_doesnt_offer_never_sends() -> None:
    """A resolved switch is ``None`` there; a stray ``True`` is not trusted either."""
    assert _plan(ANSWERED, TypeSwitches(app=True, email=False, push=True)) == Delivery.NONE
    assert _plan(SUPPORT, TypeSwitches(app=False, email=True, push=False)) == Delivery.NONE
    assert _plan(INVOICES, TypeSwitches(app=False, email=False, push=True)) == Delivery.NONE, "kastlan: no push"


def test_a_service_notice_skips_the_switches_in_the_app_and_by_push() -> None:
    """keksdose's deadline on a guest's budget: a push and an inbox row whatever
    ``share_events`` says, and never a notification mail."""
    shares = KEKSDOSE["share_events"]
    assert _plan(shares, TypeSwitches(app=False, email=None, push=False), service=True) == Delivery(
        inbox=True, push=True, email=False
    )
    assert _plan(INVOICES, ON, service=True) == Delivery(inbox=True, push=False, email=False), "kastlan: no push"
    assert _plan(DUE, OFF, service=True, quiet_now=True) == Delivery(inbox=True, push=False, email=False)
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True), service=True) == Delivery.NONE, "no app channel"


def test_a_device_mutes_types_never_adds_them() -> None:
    """§10.2 rule 3: after the account's push switch, a device's own mutes; a muted id the
    registry dropped matches nothing."""
    assert device_receives([], "scheduled_due")
    assert device_receives(None, "scheduled_due")
    assert not device_receives(["scheduled_due"], "scheduled_due")
    assert device_receives(["gone_type"], "scheduled_due")


def test_the_muted_types_read_drops_what_the_registry_no_longer_holds() -> None:
    assert read_muted_types(KEKSDOSE, ["gone_type", "support_chat", "overspend"]) == ["overspend", "support_chat"]
    assert read_muted_types(KEKSDOSE, None) == []
    assert read_muted_types(KASTLAN, ["invoices_overdue"]) == [], "no push, nothing to mute"


def test_the_muted_types_write_is_the_whole_list_in_registry_order() -> None:
    assert apply_muted_types(KEKSDOSE, ["support_chat", "overspend", "support_chat"]) == ["overspend", "support_chat"]
    assert apply_muted_types(KEKSDOSE, []) == []


def test_the_muted_types_write_refuses_an_unreceived_or_pushless_type() -> None:
    with pytest.raises(NotificationTypeError) as unknown:
        apply_muted_types(KEKSDOSE.receivable(lambda item: item.id != "digest"), ["digest"])
    assert unknown.value.type_ids == ("digest",)
    with pytest.raises(NotificationChannelError) as pushless:
        apply_muted_types(KASTLAN, ["invoices_overdue"])
    assert pushless.value.extra == {"types": ["invoices_overdue"], "channel": "push"}
