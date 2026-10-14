"""The settings resource (notifications §3.2): the switches as they apply, and each PATCH rule."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import Field, ValidationError

from eifi1_server_kit.notifications import (
    SETTINGS_NOT_NULLABLE,
    SETTINGS_SKIP,
    DigestFields,
    DigestUpdate,
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
    NotificationTypeError,
    QuietHoursFields,
    QuietHoursUpdate,
    StoredSwitches,
    TypeSwitches,
    TypeSwitchesPatch,
    apply_switches_patch,
    resolve_switches,
)
from eifi1_server_kit.settings import PatchNullError, apply_patch
from tests.notifications._types import KASTLAN, KEKSDOSE, KURVENSCHMIEDE


class KeksdoseUpdate(DigestUpdate, QuietHoursUpdate, NotificationSettingsUpdate):
    """keksdose's body: the mixins first, so ``types`` leads; its own threshold beside them."""

    registry = KEKSDOSE
    low_balance_threshold: Decimal | None = Field(default=None, ge=0)


class KeksdoseResponse(DigestFields, QuietHoursFields, NotificationSettingsResponse):
    low_balance_threshold: Decimal | None = None


class KastlanUpdate(NotificationSettingsUpdate):
    registry = KASTLAN


class KurvenschmiedeUpdate(NotificationSettingsUpdate):
    registry = KURVENSCHMIEDE


def _row() -> dict[str, Any]:
    return {
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "timezone": "Europe/Zurich",
        "digest_cadence": "weekly",
        "low_balance_threshold": None,
    }


# ── the GET ──────────────────────────────────────────────────────────────────


def test_unset_switches_read_as_their_defaults_in_the_registrys_order() -> None:
    resolved = resolve_switches(KEKSDOSE, {})
    assert list(resolved) == list(KEKSDOSE.ids)
    assert resolved["recurring_posted"] == TypeSwitches(app=True, email=None)
    assert resolved["digest"] == TypeSwitches(app=False, email=None)
    assert resolved["support_chat"] == TypeSwitches(app=True, email=None)


def test_a_stored_switch_wins_and_an_unset_one_reads_as_its_default() -> None:
    stored = {
        "invoices_overdue": StoredSwitches(app=True),
        "dunnings_pending": StoredSwitches(app=None, email=None),
    }
    assert resolve_switches(KASTLAN, stored) == {
        "invoices_overdue": TypeSwitches(app=True, email=False),
        "dunnings_pending": TypeSwitches(app=False, email=None),
    }


def test_a_channel_the_type_doesnt_offer_is_null_whatever_is_stored() -> None:
    """Kurvenschmiede: ``{"feedback_answered": {"app": null, "email": true}}``."""
    stored = {"feedback_answered": StoredSwitches(app=True), "team_added": StoredSwitches(email=True)}
    assert resolve_switches(KURVENSCHMIEDE, stored) == {
        "feedback_answered": TypeSwitches(app=None, email=True),
        "share_granted": TypeSwitches(app=None, email=False),
        "team_added": TypeSwitches(app=None, email=True),
    }


def test_only_the_types_the_account_receives_are_answered() -> None:
    """A kastlan tenant receives neither type; a stored row for one is left out."""
    tenant = KASTLAN.receivable(lambda _type: False)
    assert resolve_switches(tenant, {"invoices_overdue": StoredSwitches(app=True)}) == {}


def test_the_response_is_the_contracts_shape() -> None:
    types = resolve_switches(KEKSDOSE, {"share_events": StoredSwitches(app=False)})
    body = KeksdoseResponse(types=types, quiet_hours_start="22:00", quiet_hours_end="07:00", timezone="Europe/Zurich")
    dumped = body.model_dump(mode="json")
    assert list(dumped) == [
        "types",
        "quiet_hours_start",
        "quiet_hours_end",
        "timezone",
        "digest_cadence",
        "low_balance_threshold",
    ]
    assert dumped["types"]["recurring_posted"] == {"app": True, "email": None}
    assert dumped["digest_cadence"] == "weekly", "the default"
    assert QuietHoursFields().model_dump() == {"quiet_hours_start": None, "quiet_hours_end": None, "timezone": None}


# ── the PATCH body ───────────────────────────────────────────────────────────


def test_the_body_leads_with_types_and_takes_the_apps_knobs() -> None:
    assert list(KeksdoseUpdate.model_fields) == [
        "types",
        "quiet_hours_start",
        "quiet_hours_end",
        "timezone",
        "digest_cadence",
        "low_balance_threshold",
    ]
    body = KeksdoseUpdate.model_validate(
        {
            "types": {"digest": {"app": True}, "support_chat": {"app": None}, "scan_done": None},
            "digest_cadence": "monthly",
        }
    )
    assert body.types == {
        "digest": TypeSwitchesPatch(app=True),
        "support_chat": TypeSwitchesPatch(app=None),
        "scan_done": None,
    }
    assert body.model_fields_set == {"types", "digest_cadence"}


@pytest.mark.parametrize(
    ("model", "body", "words"),
    [
        (KeksdoseUpdate, {"types": {"feedback_answered": {"app": True}}}, "unknown notification type"),
        (KeksdoseUpdate, {"types": {"digest": {"email": True}}}, "has no email switch"),
        (KeksdoseUpdate, {"types": {"digest": {"email": None}}}, "has no email switch"),
        (KurvenschmiedeUpdate, {"types": {"feedback_answered": {"app": False}}}, "has no app switch"),
        (KastlanUpdate, {"types": {"dunnings_pending": {"app": True, "email": True}}}, "has no email switch"),
        (KeksdoseUpdate, {"types": {"digest": {"push": True}}}, "Extra inputs"),
        (KeksdoseUpdate, {"colour": "teal"}, "Extra inputs"),
        (KeksdoseUpdate, {"quiet_hours_start": "24:00"}, "String should match pattern"),
        (KeksdoseUpdate, {"quiet_hours_end": "7:00"}, "String should match pattern"),
        (KeksdoseUpdate, {"timezone": "Mars/Olympus"}, "IANA zone"),
        (KeksdoseUpdate, {"timezone": "Europe/" + "x" * 60}, "at most 64 characters"),
        (KeksdoseUpdate, {"digest_cadence": "daily"}, "'weekly' or 'monthly'"),
        (KeksdoseUpdate, {"types": {"digest": {"app": "maybe"}}}, "valid boolean"),
    ],
)
def test_each_422(model: type[NotificationSettingsUpdate], body: dict[str, Any], words: str) -> None:
    """Rule 4: an unknown field, an unknown type, a switch the type doesn't offer, a bad zone."""
    with pytest.raises(ValidationError, match=words):
        model.model_validate(body)


def test_an_unknown_type_id_is_cut_in_the_answer() -> None:
    with pytest.raises(ValidationError) as caught:
        KeksdoseUpdate.model_validate({"types": {"x" * 500: None}})
    assert "x" * 41 not in str(caught.value)


def test_a_body_without_its_registry_is_a_programming_error() -> None:
    class Forgotten(NotificationSettingsUpdate):
        pass

    with pytest.raises(TypeError, match=r"Forgotten\.registry is unset"):
        Forgotten.model_validate({})


def test_nulls_clear_the_quiet_hours_and_the_zone() -> None:
    body = KeksdoseUpdate.model_validate({"quiet_hours_start": None, "quiet_hours_end": None, "timezone": None})
    row = _row()
    written = apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    assert written == {"quiet_hours_start": None, "quiet_hours_end": None, "timezone": None}
    assert row["digest_cadence"] == "weekly", "omitted keeps"


# ── the PATCH rules ──────────────────────────────────────────────────────────


def test_rule_1_omitted_keeps() -> None:
    """A type left out of ``types``, a switch left out of a type, a field left out."""
    stored = {"invoices_overdue": StoredSwitches(app=True, email=True)}
    body = KastlanUpdate.model_validate({"types": {"invoices_overdue": {"email": False}}})
    assert apply_switches_patch(KASTLAN, stored, body.types) == {
        "invoices_overdue": StoredSwitches(app=True, email=False)
    }, "dunnings_pending is not named, and app is kept"
    empty = KastlanUpdate.model_validate({"types": {"invoices_overdue": {}}})
    assert apply_switches_patch(KASTLAN, stored, empty.types) == stored
    assert apply_switches_patch(KASTLAN, stored, KastlanUpdate().types) == {}, "types left out"
    assert apply_switches_patch(KASTLAN, stored, {}) == {}


def test_rule_2_a_types_null_resets_both_switches() -> None:
    stored = {"invoices_overdue": StoredSwitches(app=True, email=True)}
    body = KastlanUpdate.model_validate({"types": {"invoices_overdue": None}})
    changed = apply_switches_patch(KASTLAN, stored, body.types)
    assert changed == {"invoices_overdue": StoredSwitches()}
    assert resolve_switches(KASTLAN, {**stored, **changed})["invoices_overdue"] == TypeSwitches(app=False, email=False)


def test_rule_2_a_switchs_null_resets_that_one() -> None:
    stored = {"invoices_overdue": StoredSwitches(app=True, email=True)}
    body = KastlanUpdate.model_validate({"types": {"invoices_overdue": {"app": None}}})
    assert apply_switches_patch(KASTLAN, stored, body.types) == {
        "invoices_overdue": StoredSwitches(app=None, email=True)
    }


def test_a_new_switch_on_a_type_with_no_row() -> None:
    body = KeksdoseUpdate.model_validate({"types": {"support_chat": {"app": False}, "digest": {"app": True}}})
    assert apply_switches_patch(KEKSDOSE, {}, body.types) == {
        "digest": StoredSwitches(app=True),
        "support_chat": StoredSwitches(app=False),
    }, "in the registry's order, not the body's"


def test_rule_3_types_null_is_refused_through_skip() -> None:
    row = _row()
    body = KeksdoseUpdate.model_validate({"types": None, "quiet_hours_start": "23:00"})
    with pytest.raises(PatchNullError) as caught:
        apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    assert caught.value.fields == ("types",)
    assert row == _row(), "nothing written"
    both = KeksdoseUpdate.model_validate({"types": None, "digest_cadence": None})
    with pytest.raises(PatchNullError) as caught:
        apply_patch(row, both, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    assert caught.value.fields == ("types", "digest_cadence")


def test_an_app_without_a_digest_refuses_types_null_alone() -> None:
    body = KurvenschmiedeUpdate.model_validate({"types": None})
    with pytest.raises(PatchNullError):
        apply_patch({}, body, not_nullable=SETTINGS_SKIP, skip=SETTINGS_SKIP)
    with pytest.raises(ValueError, match="no such field on KurvenschmiedeUpdate: digest_cadence"):
        apply_patch({}, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)


def test_a_body_of_types_alone_has_no_row_to_write() -> None:
    """kastlan's 0.36 finding 3: ``types`` is the whole body, so there is no row — ``None``,
    not a throwaway object. Rule 3 still holds; a body with a plain field needs its row."""
    sent = KastlanUpdate.model_validate({"types": {"invoices_overdue": {"email": True}}})
    assert apply_patch(None, sent, not_nullable=SETTINGS_SKIP, skip=SETTINGS_SKIP) == {}
    with pytest.raises(PatchNullError):
        apply_patch(None, KastlanUpdate.model_validate({"types": None}), not_nullable=SETTINGS_SKIP, skip=SETTINGS_SKIP)
    with pytest.raises(TypeError, match="every field of KeksdoseUpdate is skipped; not skipped: quiet_hours_start,"):
        apply_patch(None, KeksdoseUpdate(), not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)


def test_rule_4_a_type_the_account_doesnt_receive() -> None:
    """kastlan's tenant: the registry knows the type, the account doesn't receive it."""
    tenant = KASTLAN.receivable(lambda _type: False)
    body = KastlanUpdate.model_validate({"types": {"dunnings_pending": {"app": True}, "invoices_overdue": None}})
    with pytest.raises(NotificationTypeError) as caught:
        apply_switches_patch(tenant, {}, body.types)
    error = caught.value
    assert isinstance(error, ValueError) and error.status_code == 422
    assert error.code == "notification_type_not_received"
    assert error.extra == {"types": ["dunnings_pending", "invoices_overdue"]}


def test_the_whole_patch_writes_the_plain_fields_and_leaves_types_to_the_switches() -> None:
    row = _row()
    stored = {"digest": StoredSwitches(app=True)}
    body = KeksdoseUpdate.model_validate(
        {
            "types": {"digest": None, "scan_done": {"app": True}},
            "digest_cadence": "monthly",
            "low_balance_threshold": "50",
        }
    )
    changed = apply_switches_patch(KEKSDOSE, stored, body.types)
    written = apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    assert written == {"digest_cadence": "monthly", "low_balance_threshold": Decimal(50)}
    assert "types" not in row
    stored = {**stored, **changed}
    answer = KeksdoseResponse(types=resolve_switches(KEKSDOSE, stored), **row)
    assert answer.types["digest"] == TypeSwitches(app=False, email=None), "reset to its default"
    assert answer.types["scan_done"] == TypeSwitches(app=True, email=None)
    assert answer.digest_cadence == "monthly"
