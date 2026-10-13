"""Who gets what, for one recipient of one type (notifications §3.3)."""

from __future__ import annotations

from typing import Any

import pytest

from eifi1_server_kit.notifications import (
    Delivery,
    NotificationType,
    TypeSwitches,
    plan_delivery,
)
from tests.notifications._types import KASTLAN, KEKSDOSE, KURVENSCHMIEDE

INVOICES = KASTLAN["invoices_overdue"]  # app and e-mail, quiet
SUPPORT = KEKSDOSE["support_chat"]  # app, not quiet
ANSWERED = KURVENSCHMIEDE["feedback_answered"]  # e-mail only

ON = TypeSwitches(app=True, email=True)
OFF = TypeSwitches(app=False, email=False)


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


def test_step_2_the_app_channel_is_an_inbox_row_and_a_push() -> None:
    assert _plan(INVOICES, TypeSwitches(app=True, email=False)) == Delivery(inbox=True, push=True, email=False)
    assert _plan(INVOICES, TypeSwitches(app=True, email=False), keeps_inbox=False) == Delivery(
        inbox=False, push=True, email=False
    ), "kastlan keeps no inbox"


def test_step_2_quiet_hours_drop_the_push_of_a_quiet_type_only() -> None:
    assert _plan(INVOICES, ON, quiet_now=True) == Delivery(inbox=True, push=False, email=True), "mail goes as usual"
    assert _plan(SUPPORT, TypeSwitches(app=True, email=None), quiet_now=True) == Delivery(
        inbox=True, push=True, email=False
    ), "a person-sent type is never held"


def test_step_3_mail_goes_only_to_a_verified_address() -> None:
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True)) == Delivery(inbox=False, push=False, email=True)
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True), email_verified=False) == Delivery.NONE


def test_step_4_both_off_is_nothing_not_even_an_inbox_row() -> None:
    assert _plan(INVOICES, OFF) == Delivery.NONE
    assert _plan(ANSWERED, TypeSwitches(app=None, email=False)) == Delivery.NONE


def test_a_switch_the_type_doesnt_offer_never_sends() -> None:
    """A resolved switch is ``None`` there; a stray ``True`` is not trusted either."""
    assert _plan(ANSWERED, TypeSwitches(app=True, email=False)) == Delivery.NONE
    assert _plan(SUPPORT, TypeSwitches(app=False, email=True)) == Delivery.NONE


def test_a_service_notice_skips_the_switch_on_the_app_channel() -> None:
    """keksdose's deadline on a guest's budget: a push and an inbox row whatever
    ``share_events`` says, and never a notification mail."""
    shares = KEKSDOSE["share_events"]
    assert _plan(shares, TypeSwitches(app=False, email=None), service=True) == Delivery(
        inbox=True, push=True, email=False
    )
    assert _plan(INVOICES, ON, service=True) == Delivery(inbox=True, push=True, email=False)
    assert _plan(INVOICES, OFF, service=True, quiet_now=True) == Delivery(inbox=True, push=False, email=False)
    assert _plan(ANSWERED, TypeSwitches(app=None, email=True), service=True) == Delivery.NONE, "no app channel"
