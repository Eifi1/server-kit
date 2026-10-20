"""Web Push: the device shapes, the payload, a push service's answer (notifications §3.5, §2.7)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eifi1_server_kit.errors import contract_error_response
from eifi1_server_kit.mail import support_address
from eifi1_server_kit.notifications import (
    PUSH_GONE_GRACE,
    PUSH_MAX_FAILURES,
    PUSH_PAYLOAD_LIMIT,
    PUSH_TEST_TYPE,
    PUSH_TIMEOUT,
    PUSH_TTL,
    NotificationError,
    PushBodyInvalidError,
    PushData,
    PushDeviceResponse,
    PushDeviceUpdate,
    PushKeyResponse,
    PushOutcome,
    PushPayload,
    PushReplaceIn,
    PushSubscriptionIn,
    PushSubscriptionKeys,
    PushTestResponse,
    PushUnregisterIn,
    digest_tag,
    parse_push_body,
    parse_push_replace,
    push_auth_matches,
    push_gone_expired,
    push_outcome,
    should_reap,
    vapid_subject,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/dXJ6-example:APA91b"
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}


def test_keksdoses_numbers() -> None:
    assert (PUSH_PAYLOAD_LIMIT, PUSH_TTL, PUSH_TIMEOUT, PUSH_MAX_FAILURES) == (3072, 86_400, 10.0, 10)
    assert PUSH_TEST_TYPE == "test"


# ── the device shapes ────────────────────────────────────────────────────────


def test_a_registration_is_the_browsers_subscription() -> None:
    """``PushSubscription.toJSON()`` carries ``expirationTime`` too, which is ignored."""
    body = PushSubscriptionIn.model_validate(
        {"endpoint": ENDPOINT, "expirationTime": None, "keys": KEYS, "user_agent": "Mozilla/5.0 (iPhone)"}
    )
    assert body.endpoint == ENDPOINT and body.keys == PushSubscriptionKeys(**KEYS)
    assert body.user_agent == "Mozilla/5.0 (iPhone)"
    assert PushSubscriptionIn(endpoint=ENDPOINT, keys=PushSubscriptionKeys(**KEYS)).user_agent is None


@pytest.mark.parametrize(
    "body",
    [
        {"endpoint": "http://push.example/x", "keys": KEYS},
        {"endpoint": "", "keys": KEYS},
        {"endpoint": "https://", "keys": KEYS},
        {"endpoint": "https://push.example/a b", "keys": KEYS},
        {"endpoint": "https://push.example/" + "x" * 2048, "keys": KEYS},
        {"endpoint": ENDPOINT},
        {"endpoint": ENDPOINT, "keys": {"p256dh": "", "auth": "x"}},
        {"endpoint": ENDPOINT, "keys": {"p256dh": "x"}},
        {"endpoint": ENDPOINT, "keys": KEYS, "user_agent": "x" * 401},
    ],
)
def test_a_registration_that_is_no_push_subscription_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PushSubscriptionIn.model_validate(body)


def test_unregistering_takes_the_endpoint_in_a_body() -> None:
    assert PushUnregisterIn(endpoint=ENDPOINT).endpoint == ENDPOINT
    with pytest.raises(ValidationError):
        PushUnregisterIn(endpoint="")


def test_a_rotated_subscription_is_replaced_by_the_old_endpoint_and_its_auth() -> None:
    body = PushReplaceIn.model_validate(
        {
            "old_endpoint": ENDPOINT,
            "old_auth": KEYS["auth"],
            "subscription": {"endpoint": ENDPOINT + "-new", "keys": KEYS, "user_agent": "Firefox"},
        }
    )
    assert body.subscription.endpoint == ENDPOINT + "-new"
    for bad in (
        {"old_endpoint": "", "old_auth": "a"},
        {"old_endpoint": ENDPOINT, "old_auth": ""},
        {"old_endpoint": ENDPOINT, "old_auth": "a", "subscription": {"endpoint": "http://plain", "keys": KEYS}},
    ):
        with pytest.raises(ValidationError):
            PushReplaceIn.model_validate({"subscription": {"endpoint": ENDPOINT, "keys": KEYS}, **bad})


def test_the_old_auth_is_compared_without_padding() -> None:
    assert push_auth_matches(KEYS["auth"], KEYS["auth"])
    assert push_auth_matches(KEYS["auth"] + "==", KEYS["auth"]), "a stored padding"
    assert not push_auth_matches(KEYS["auth"], KEYS["auth"][:-1])
    assert not push_auth_matches(KEYS["auth"], "")


def test_the_small_answers() -> None:
    assert PushKeyResponse(key="").key == "", "push off"
    assert PushTestResponse(sent=2).model_dump() == {"sent": 2}


@dataclass
class _Row:
    id: uuid.UUID | int
    endpoint: str
    user_agent: str | None
    created_at: datetime
    last_success_at: datetime | None
    failure_count: int


@pytest.mark.parametrize("row_id", [uuid.UUID("0b6c3a4e-1f2d-4c5b-9a8e-7d6c5b4a3f2e"), 17])
def test_a_device_is_read_from_the_apps_row(row_id: uuid.UUID | int) -> None:
    created = datetime(2026, 10, 13, 9, tzinfo=UTC)
    device = PushDeviceResponse.model_validate(_Row(row_id, ENDPOINT, None, created, None, 3))
    assert device.id == row_id and device.endpoint == ENDPOINT, "the owner gets the whole endpoint"
    assert device.failure_count == 3 and device.last_success_at is None


# ── the payload ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["/", "/support", "/budgets/7?month=2026-10#top", "/a//b"])
def test_a_click_goes_to_a_path_in_the_app(url: str) -> None:
    assert PushData(url=url, type="support_chat").url == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "support",
        "//evil.example",
        "/\\evil.example",
        "https://evil.example/",
        "javascript:alert(1)",
        "/\t/evil.example",
        "/\n/evil.example",
        "/a\\b",
        "/a\x7f",
    ],
)
def test_a_click_never_leaves_the_app(url: str) -> None:
    with pytest.raises(ValidationError, match="path in the app"):
        PushData(url=url, type="support_chat")


def test_the_type_is_a_type_id_or_test() -> None:
    assert PushData(url="/", type=PUSH_TEST_TYPE).type == "test"
    with pytest.raises(ValidationError):
        PushData(url="/", type="Support-Chat")


def _payload(body: str, *, title: str = "Keksdose", tag: str = "support:1") -> PushPayload:
    return PushPayload(title=title, body=body, tag=tag, data=PushData(url="/support", type="support_chat"))


def test_the_payload_is_compact_json_in_utf_8() -> None:
    encoded = _payload("Antwort von Zoë").encode()
    assert (
        encoded
        == (
            '{"title":"Keksdose","body":"Antwort von Zoë","tag":"support:1",'
            '"data":{"url":"/support","type":"support_chat","inbox":true}}'
        ).encode()
    )
    assert json.loads(encoded)["data"]["type"] == "support_chat"


def test_the_digest_tag_is_the_payloads_tag() -> None:
    payload = PushPayload(title="t", body="b", tag=digest_tag("2026-W42"), data=PushData(url="/", type="digest"))
    assert json.loads(payload.encode())["tag"] == "digest:2026-W42"
    with pytest.raises(ValidationError):
        PushPayload(title="t", body="b", tag="", data=PushData(url="/", type="digest"))


def test_a_long_body_is_cut_with_an_ellipsis_to_fit() -> None:
    body = "Ein Satz mit Umlauten: äöü. " * 200
    encoded = _payload(body).encode()
    assert len(encoded) <= PUSH_PAYLOAD_LIMIT
    cut = json.loads(encoded)["body"]
    assert cut.endswith("…") and body.startswith(cut[:-1])
    assert not cut[:-1].endswith(" "), "no space before the ellipsis"
    following = len(cut) - 1  # past the spaces the cut dropped, one more character
    while body[following] == " ":
        following += 1
    longer = len(_payload(body[: following + 1] + "…").encode(limit=10 * PUSH_PAYLOAD_LIMIT))
    assert longer > PUSH_PAYLOAD_LIMIT, "the longest cut that fits"


def test_the_limit_is_the_callers() -> None:
    small = _payload("x" * 100).encode(limit=150)
    assert len(small) <= 150 and json.loads(small)["body"].endswith("…")
    whole = _payload("short").encode(limit=150)
    assert json.loads(whole)["body"] == "short"


def test_a_body_that_fits_only_empty_is_sent_empty() -> None:
    empty = len(_payload("").encode())
    assert json.loads(_payload("abcdef").encode(limit=empty + 2))["body"] == "", "even … alone is too long"
    assert json.loads(_payload("abcdef").encode(limit=empty + 3))["body"] == "…"


def test_a_payload_too_long_with_an_empty_body_is_refused() -> None:
    with pytest.raises(ValueError, match="even with an empty body"):
        _payload("b", title="T" * PUSH_PAYLOAD_LIMIT).encode()


# ── a push service's answer ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, PushOutcome.DELIVERED),
        (201, PushOutcome.DELIVERED),
        (204, PushOutcome.DELIVERED),
        (404, PushOutcome.GONE),
        (410, PushOutcome.GONE),
        (199, PushOutcome.FAILED),
        (301, PushOutcome.FAILED),
        (400, PushOutcome.FAILED),
        (401, PushOutcome.FAILED),
        (403, PushOutcome.FAILED),
        (413, PushOutcome.FAILED),
        (429, PushOutcome.FAILED),
        (500, PushOutcome.FAILED),
        (503, PushOutcome.FAILED),
        (None, PushOutcome.FAILED),
    ],
)
def test_push_outcome(status: int | None, outcome: PushOutcome) -> None:
    """404/410 delete the row now; everything else, a timeout included, counts a failure."""
    assert push_outcome(status) is outcome
    assert str(outcome) in {"delivered", "gone", "failed"}


def test_ten_failures_in_a_row_reap_the_device() -> None:
    assert not should_reap(0) and not should_reap(9)
    assert should_reap(10) and should_reap(11)


def test_the_app_may_set_its_own_reap_limit() -> None:
    """keksdose's 0.36 finding 2: its ``push_max_failures`` setting is the limit."""
    assert not should_reap(4, limit=5) and should_reap(5, limit=5)
    assert not should_reap(0, limit=1) and should_reap(1, limit=1)
    assert not should_reap(10, limit=20) and should_reap(20, limit=20)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_reap_limit_below_one_is_refused(limit: int) -> None:
    """It would reap a device that never failed."""
    with pytest.raises(ValueError, match=f"a reap limit is 1 or more: {limit}"):
        should_reap(0, limit=limit)


def test_the_vapid_subject_is_the_support_address() -> None:
    assert vapid_subject("support@keksdose.app") == "mailto:support@keksdose.app"
    assert vapid_subject(" support@kastlan.ch ") == "mailto:support@kastlan.ch"
    assert vapid_subject(support_address("Kurvenschmiede <noreply@kurvenschmiede.app>")) == (
        "mailto:support@kurvenschmiede.app"
    )


@pytest.mark.parametrize(
    "address",
    ["", "support", "support@localhost", "Keksdose <support@keksdose.app>", "mailto:support@keksdose.app", "a b@x.ch"],
)
def test_the_vapid_subject_takes_a_bare_address(address: str) -> None:
    with pytest.raises(ValueError, match="bare address"):
        vapid_subject(address)


# ── 0.16: the inbox flag, per-device mutes, the gone grace (§10.2, §10.6) ────


def test_a_push_says_whether_the_inbox_holds_it() -> None:
    """``data.inbox: false`` (push on, in-app off): the worker shows it even while focused."""
    assert PushData(url="/", type="scan_done").inbox is True
    payload = PushPayload(title="t", body="b", tag="x", data=PushData(url="/", type="scan_done", inbox=False))
    assert json.loads(payload.encode())["data"] == {"url": "/", "type": "scan_done", "inbox": False}


def test_a_device_lists_its_muted_types() -> None:
    device = PushDeviceResponse(
        id=1,
        endpoint="https://push.example/x",
        user_agent=None,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
        last_success_at=None,
        failure_count=0,
    )
    assert device.muted_types == []
    assert PushDeviceUpdate(muted_types=["digest"]).muted_types == ["digest"]
    with pytest.raises(ValidationError):
        PushDeviceUpdate.model_validate({"muted_types": ["digest"], "label": "x"})
    with pytest.raises(ValidationError):
        PushDeviceUpdate.model_validate({})


def test_a_gone_row_is_claimable_for_seven_days() -> None:
    gone = datetime(2026, 10, 1, tzinfo=UTC)
    assert timedelta(days=7) == PUSH_GONE_GRACE
    assert not push_gone_expired(None, gone + timedelta(days=30)), "not gone"
    assert not push_gone_expired(gone, gone + timedelta(days=7)), "the replace may still claim it"
    assert push_gone_expired(gone, gone + timedelta(days=7, seconds=1)), "the reap deletes it"
    assert push_gone_expired(gone, gone + timedelta(hours=2), grace=timedelta(hours=1))


def test_the_replace_routes_rules_are_in_its_docstring() -> None:
    """§10.6: the app's route follows these; the adopt row points here."""
    doc = PushReplaceIn.__doc__ or ""
    for rule in ("PUSH_GONE_GRACE", "inactive", "demo", "sign out everywhere", "erasure", "No RLS", "Never log"):
        assert rule in doc, rule


# ── parsing a body that carries a secret, without echoing it (0.16.1) ────────


SECRET = "s3cr3t-auth-never-echoed"


async def _answer(error: Exception) -> dict[str, Any]:
    response = await contract_error_response(None, error)  # type: ignore[arg-type]
    assert response.status_code == 422
    return dict(json.loads(bytes(response.body)))


def test_a_replace_body_is_parsed_into_the_model() -> None:
    body = parse_push_replace(
        {
            "old_endpoint": ENDPOINT,
            "old_auth": KEYS["auth"],
            "subscription": {"endpoint": ENDPOINT + "-n", "keys": KEYS},
        }
    )
    assert isinstance(body, PushReplaceIn) and body.old_auth == KEYS["auth"]


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        (
            {"old_endpoint": "", "old_auth": SECRET, "subscription": {"endpoint": ENDPOINT, "keys": KEYS}},
            ["old_endpoint"],
        ),
        (
            {"old_endpoint": ENDPOINT, "old_auth": SECRET, "subscription": {"endpoint": "http://x", "keys": KEYS}},
            ["subscription.endpoint"],
        ),
        ({"old_endpoint": ENDPOINT, "old_auth": SECRET * 20, "subscription": None}, ["old_auth", "subscription"]),
        ([SECRET], ["body"]),
        (SECRET, ["body"]),
        (None, ["body"]),
    ],
)
async def test_a_bad_replace_body_is_a_plain_coded_422_that_never_echoes_it(body: object, fields: list[str]) -> None:
    with pytest.raises(PushBodyInvalidError) as caught:
        parse_push_replace(body)
    error = caught.value
    assert isinstance(error, NotificationError) and error.__cause__ is None and error.__context__ is None
    answer = await _answer(error)
    assert answer == {"detail": PushBodyInvalidError.default_detail, "code": "push_body_invalid", "fields": fields}
    assert SECRET not in json.dumps(answer) and SECRET not in repr(error)


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (PushSubscriptionIn, {"endpoint": "http://x", "keys": {"p256dh": SECRET, "auth": SECRET}}),
        (PushSubscriptionIn, {"endpoint": ENDPOINT, "keys": {"p256dh": "", "auth": SECRET}}),
        (PushUnregisterIn, {"endpoint": SECRET * 200}),
    ],
)
async def test_every_push_body_with_a_secret_parses_without_an_echo(model: type[Any], body: object) -> None:
    with pytest.raises(PushBodyInvalidError) as caught:
        parse_push_body(model, body)
    answer = await _answer(caught.value)
    assert answer["code"] == "push_body_invalid" and SECRET not in json.dumps(answer)
    assert SECRET not in str(caught.value)
    assert parse_push_body(PushUnregisterIn, {"endpoint": ENDPOINT}) == PushUnregisterIn(endpoint=ENDPOINT)


def test_the_no_echo_rule_is_in_the_module_docstring() -> None:
    from eifi1_server_kit.notifications import push

    doc = push.__doc__ or ""
    for word in ("parse_push_replace", "parse_push_body", "Body()", "input"):
        assert word in doc, word
