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
    NotificationChannelError,
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
    NotificationTypeError,
    QuietHoursFields,
    QuietHoursUpdate,
    StoredSwitches,
    TypeSwitches,
    TypeSwitchesPatch,
    apply_switches_patch,
    migrate_enabled_to_channels,
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
        "digest_cadence": "weekly",
        "low_balance_threshold": None,
    }


# ── the GET ──────────────────────────────────────────────────────────────────


def test_unset_switches_read_as_their_defaults_in_the_registrys_order() -> None:
    resolved = resolve_switches(KEKSDOSE, {})
    assert list(resolved) == list(KEKSDOSE.ids)
    assert resolved["recurring_posted"] == TypeSwitches(app=True, email=None, push=True)
    assert resolved["digest"] == TypeSwitches(app=False, email=None, push=False)
    assert resolved["support_chat"] == TypeSwitches(app=True, email=None, push=True)


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
    body = KeksdoseResponse(types=types, quiet_hours_start="22:00", quiet_hours_end="07:00")
    dumped = body.model_dump(mode="json")
    assert list(dumped) == [
        "types",
        "quiet_hours_start",
        "quiet_hours_end",
        "digest_cadence",
        "low_balance_threshold",
    ]
    assert dumped["types"]["recurring_posted"] == {"app": True, "email": None, "push": True}
    assert dumped["digest_cadence"] == "weekly", "the default"
    assert QuietHoursFields().model_dump() == {"quiet_hours_start": None, "quiet_hours_end": None}


# ── the PATCH body ───────────────────────────────────────────────────────────


def test_the_body_leads_with_types_and_takes_the_apps_knobs() -> None:
    assert list(KeksdoseUpdate.model_fields) == [
        "types",
        "quiet_hours_start",
        "quiet_hours_end",
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
        (KastlanUpdate, {"types": {"invoices_overdue": {"push": True}}}, "has no push switch"),
        (KurvenschmiedeUpdate, {"types": {"feedback_answered": {"push": None}}}, "has no push switch"),
        (KeksdoseUpdate, {"types": {"digest": {"sms": True}}}, "Extra inputs"),
        (KeksdoseUpdate, {"colour": "teal"}, "Extra inputs"),
        (KeksdoseUpdate, {"quiet_hours_start": "24:00"}, "String should match pattern"),
        (KeksdoseUpdate, {"quiet_hours_end": "7:00"}, "String should match pattern"),
        (KeksdoseUpdate, {"digest_cadence": "daily"}, "'weekly' or 'monthly'"),
        (KeksdoseUpdate, {"types": {"digest": {"app": "maybe"}}}, "valid boolean"),
    ],
)
def test_each_422(model: type[NotificationSettingsUpdate], body: dict[str, Any], words: str) -> None:
    """Rule 4: an unknown field, an unknown type, a switch the type doesn't offer."""
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


class LegacySettingsShim(DigestUpdate, QuietHoursUpdate):
    """keksdose's ``/push/settings`` shim body (``LegacyNotificationSettingsUpdate``)."""


@pytest.mark.parametrize("model", [KeksdoseUpdate, KastlanUpdate, KurvenschmiedeUpdate, LegacySettingsShim])
@pytest.mark.parametrize(
    "sent",
    [{"timezone": "Europe/Zurich"}, {"timezone": None}, {"timezone": "Europe/Zurich", "quiet_hours_start": "22:00"}],
)
def test_a_stale_bundles_timezone_is_refused_from_0_18(model: type[Any], sent: dict[str, Any]) -> None:
    """A 0.45 bundle still sends ``{timezone}`` (its picker, its quiet hours' first entry):
    ignored in 0.16 and 0.17, refused from 0.18 as any unknown field is (a 422)."""
    with pytest.raises(ValidationError, match="Extra inputs") as caught:
        model.model_validate(sent)
    assert ("timezone",) in [error["loc"] for error in caught.value.errors()]


def test_nulls_clear_the_quiet_hours() -> None:
    body = KeksdoseUpdate.model_validate({"quiet_hours_start": None, "quiet_hours_end": None})
    row = _row()
    written = apply_patch(row, body, not_nullable=SETTINGS_NOT_NULLABLE, skip=SETTINGS_SKIP)
    assert written == {"quiet_hours_start": None, "quiet_hours_end": None}
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
    assert answer.types["digest"] == TypeSwitches(app=False, email=None, push=False), "reset to its default"
    assert answer.types["scan_done"] == TypeSwitches(app=True, email=None, push=False), "push is its own switch"
    assert answer.digest_cadence == "monthly"


# ── a switch per channel (§10.2, 0.16) ───────────────────────────────────────


def test_an_app_without_push_answers_push_null_on_every_type() -> None:
    """kastlan's ``app=False`` types would read ``push: false``; ``push_channel=False``
    makes it ``null``: not offered."""
    resolved = resolve_switches(KASTLAN, {"invoices_overdue": StoredSwitches(app=True, push=True)})
    assert {switches.push for switches in resolved.values()} == {None}
    assert resolve_switches(KURVENSCHMIEDE, {})["feedback_answered"].model_dump() == {
        "app": None,
        "email": True,
        "push": None,
    }


def test_push_sent_where_the_app_has_none_is_refused_before_anything_is_written() -> None:
    with pytest.raises(NotificationChannelError) as refused:
        apply_switches_patch(KASTLAN, {}, {"invoices_overdue": TypeSwitchesPatch(app=True, push=True)})
    assert refused.value.status_code == 422 and refused.value.code == "notification_channel_not_offered"
    assert refused.value.extra == {"types": ["invoices_overdue"], "channel": "push"}
    with pytest.raises(NotificationChannelError, match=r"\(email\): digest"):
        apply_switches_patch(KEKSDOSE, {}, {"digest": TypeSwitchesPatch(email=True)})


def test_push_is_written_on_its_own() -> None:
    stored = {"scan_done": StoredSwitches(app=True)}
    changed = apply_switches_patch(KEKSDOSE, stored, {"scan_done": TypeSwitchesPatch(push=True)})
    assert changed == {"scan_done": StoredSwitches(app=True, push=True)}
    reset = apply_switches_patch(KEKSDOSE, changed, {"scan_done": None})
    assert reset == {"scan_done": StoredSwitches()}, "a type's null resets all three"


def test_an_old_bundles_app_alone_writes_push_too_with_the_shim() -> None:
    """keksdose's ``couple_push_to_app=True`` while ``/push/*`` requests still arrive."""
    body = {"scan_done": TypeSwitchesPatch(app=True), "support_chat": TypeSwitchesPatch(app=False, push=True)}
    coupled = apply_switches_patch(KEKSDOSE, {}, body, couple_push_to_app=True)
    assert coupled["scan_done"] == StoredSwitches(app=True, push=True)
    assert coupled["support_chat"] == StoredSwitches(app=False, push=True), "a push sent wins"
    assert apply_switches_patch(KEKSDOSE, {}, body)["scan_done"] == StoredSwitches(app=True), "off by default"
    kastlan = apply_switches_patch(
        KASTLAN, {}, {"invoices_overdue": TypeSwitchesPatch(app=True)}, couple_push_to_app=True
    )
    assert kastlan["invoices_overdue"] == StoredSwitches(app=True), "no push to couple"


def test_a_type_the_account_doesnt_receive_is_refused_on_patch_and_absent_on_read() -> None:
    """Kurvenschmiede's ``feedback_received`` is an ADMIN's: a MEMBER is neither offered
    nor allowed it."""
    member = KURVENSCHMIEDE.receivable(lambda item: item.id != "team_added")
    assert "team_added" not in resolve_switches(member, {"team_added": StoredSwitches(email=True)})
    with pytest.raises(NotificationTypeError):
        apply_switches_patch(member, {}, {"team_added": TypeSwitchesPatch(email=True)})


@pytest.mark.parametrize(
    ("enabled", "stored"),
    [
        (True, StoredSwitches(app=True, push=True)),
        (False, StoredSwitches(app=False, push=False)),
        (None, StoredSwitches()),
    ],
)
def test_keksdoses_enabled_becomes_both_in_app_switches(enabled: bool | None, stored: StoredSwitches) -> None:
    assert migrate_enabled_to_channels(enabled) == stored
