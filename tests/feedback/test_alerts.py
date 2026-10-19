"""The admins' alert for a new report, a rework and a new crash (feedback §11, server-kit 0.16)."""

from __future__ import annotations

from typing import Any

import pytest

from eifi1_server_kit.demo import DemoError
from eifi1_server_kit.feedback import (
    FEEDBACK_RECEIVED,
    CrashReportCreate,
    FeedbackAlert,
    FeedbackCategory,
    FeedbackCommentCreate,
    FeedbackReplyCreate,
    FeedbackReworkCreate,
    FeedbackStatus,
    FeedbackUpdate,
    admin_alert_recipients,
    mails_at_once,
)
from eifi1_server_kit.feedback.testing import MemoryFeedbackRow, RecordingAlerts
from tests.feedback._layer2 import ADMIN, AUTHOR, DEMO
from tests.feedback.test_service_public import MEMBER, World

S = FeedbackStatus


def _crash(message: str = "x is undefined") -> CrashReportCreate:
    return CrashReportCreate(name="TypeError", message=message, route="/accounts", version="1.0.0")


@pytest.fixture
def alerts() -> RecordingAlerts:
    return RecordingAlerts()


@pytest.fixture
def world(alerts: RecordingAlerts) -> World:
    return World(alerts=alerts)


def test_the_type_id_is_the_kits() -> None:
    assert FEEDBACK_RECEIVED == "feedback_received"


async def test_a_new_report_tells_once_after_the_write(world: World, alerts: RecordingAlerts) -> None:
    row = await world.file(AUTHOR)
    [(alert, seen)] = alerts.alerts
    assert alert == FeedbackAlert(
        kind="report", feedback_id=row.id, category=FeedbackCategory.OTHER, actor_id=AUTHOR.id, company_id=None
    )
    assert seen is row
    assert world.repository.calls[-1] == "add", "the alert comes after the write"


async def test_a_new_crash_tells_and_a_folded_repeat_does_not(world: World, alerts: RecordingAlerts) -> None:
    first = await world.service.report_crash(AUTHOR, _crash())
    again = await world.service.report_crash(AUTHOR, _crash())
    assert first.stored and again.duplicate
    assert alerts.kinds == ["crash"]
    assert alerts.alerts[0][0].category is FeedbackCategory.CRASH


async def test_a_demo_fires_nothing(world: World, alerts: RecordingAlerts) -> None:
    with pytest.raises(DemoError):
        await world.service.create(DEMO, _create())
    assert (await world.service.report_crash(DEMO, _crash())).stored is False
    assert alerts.alerts == []


async def test_replies_status_changes_comments_and_votes_fire_nothing(world: World, alerts: RecordingAlerts) -> None:
    row = await world.file(AUTHOR)
    alerts.alerts.clear()
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Looking.", status=S.IN_PROGRESS))
    await world.service.update(ADMIN, row.id, FeedbackUpdate(status=S.READY))
    await world.service.comment(MEMBER, row.id, FeedbackCommentCreate(body="Same here"))
    await world.service.vote(MEMBER, row.id)
    assert alerts.alerts == []


async def test_a_members_rework_tells_after_the_report_moved(world: World, alerts: RecordingAlerts) -> None:
    row = await _answered(world, alerts)
    sent = await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body="Still slow."))
    [(alert, seen)] = alerts.alerts
    assert (alert.kind, alert.feedback_id, alert.actor_id, alert.category) == (
        "rework",
        row.id,
        AUTHOR.id,
        FeedbackCategory.OTHER,
    )
    assert seen is sent and sent.status is S.OPEN
    assert world.repository.calls[-1] == "apply"


async def test_a_rework_whose_move_fails_tells_nobody(
    world: World, alerts: RecordingAlerts, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await _answered(world, alerts)

    async def refuse(*_args: Any, **_kwargs: Any) -> MemoryFeedbackRow:
        raise RuntimeError("the write failed")

    monkeypatch.setattr(world.repository, "apply", refuse)
    with pytest.raises(RuntimeError):
        await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body="Still slow."))
    assert alerts.alerts == []


async def test_an_admins_own_rework_lands_in_rework_and_tells_nobody(world: World, alerts: RecordingAlerts) -> None:
    row = await _answered(world, alerts, ADMIN)
    sent = await world.service.rework(ADMIN, row.id, FeedbackReworkCreate(body="Not on the iPad."))
    assert sent.status is S.REWORK
    assert alerts.alerts == []


async def test_a_service_without_alerts_is_silent(alerts: RecordingAlerts) -> None:
    """Kurvenschmiede's loop builds its service with ``alerts=None``."""
    loop = World()
    row = await loop.file(AUTHOR)
    await loop.service.report_crash(AUTHOR, _crash())
    await loop.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Done.", status=S.DONE))
    await loop.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body="Again."))
    assert loop.service.alerts is None and alerts.alerts == []


async def test_the_alert_names_the_rows_company_where_rows_carry_one(world: World, alerts: RecordingAlerts) -> None:
    """kastlan's row carries ``company_id``; the alert reads it and the row's category."""
    row = await _answered(world, alerts)
    row.category = FeedbackCategory.IDEA
    real_apply = world.repository.apply

    class CompanyRow:
        """kastlan's row: the kit's columns and ``company_id``."""

        def __init__(self, inner: MemoryFeedbackRow) -> None:
            self.inner = inner
            self.company_id = 3

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    async def apply_with_company(*args: Any, **kwargs: Any) -> Any:
        return CompanyRow(await real_apply(*args, **kwargs))

    world.repository.apply = apply_with_company  # type: ignore[method-assign]
    await world.service.rework(AUTHOR, row.id, FeedbackReworkCreate(body="Again."))
    [(alert, _row)] = alerts.alerts
    assert (alert.company_id, alert.category) == (3, FeedbackCategory.IDEA)


def test_the_actor_is_never_told_and_each_admin_once() -> None:
    assert admin_alert_recipients([2, 5, 2, 7], actor_id=5) == [2, 7]
    assert admin_alert_recipients([2, 5], actor_id=None) == [2, 5]
    assert admin_alert_recipients([], actor_id=1) == []


@pytest.mark.parametrize(("kind", "at_once"), [("report", True), ("rework", True), ("crash", False)])
def test_a_crash_is_mailed_only_in_a_bundle(kind: Any, at_once: bool) -> None:
    alert = FeedbackAlert(kind=kind, feedback_id=1, category=None, actor_id=1)
    assert mails_at_once(alert) is at_once


# --- helpers ---------------------------------------------------------------------------------


def _create() -> Any:
    from tests.feedback.test_service_public import Create

    return Create(title="Sync is slow", body="b")


async def _answered(world: World, alerts: RecordingAlerts, person: Any = AUTHOR) -> MemoryFeedbackRow:
    row = await world.file(person)
    status = S.NEEDS_LIVE_TEST if person is ADMIN else S.DONE
    await world.service.reply(ADMIN, row.id, FeedbackReplyCreate(body="Fixed.", status=status))
    world.clear()
    alerts.alerts.clear()
    return row
