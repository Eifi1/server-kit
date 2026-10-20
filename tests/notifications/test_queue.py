"""The queue: held pushes, bundled mails, the flush and the tick (notifications §10.4, 0.16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eifi1_server_kit.notifications import (
    MAIL_LATE_DAYS,
    PUSH_TTL,
    QUEUE_CLAIM_TIMEOUT,
    QUEUE_MAX_ATTEMPTS,
    QUEUE_RETENTION,
    FlushResult,
    NotificationType,
    PushData,
    PushPayload,
    QueueChannel,
    QueueService,
    notifications_tick,
)
from eifi1_server_kit.notifications.testing import InMemoryNotificationQueue, RecordingFlushPort

NOW = datetime(2026, 10, 19, 22, tzinfo=UTC)
END = NOW + timedelta(hours=8)
RECEIVED = NotificationType("feedback_received", app=True, email=True, bundle=timedelta(minutes=15))
COMMENT = NotificationType("feedback_comment", app=None, email=True)


def _push(title: str, body: str, tag: str, *, inbox: bool = True) -> PushPayload:
    return PushPayload(
        title=title, body=body, tag=tag, data=PushData(url="/feedback?row=1", type="scan_done", inbox=inbox)
    )


def _service(*, sends_ok: bool = True) -> tuple[QueueService, InMemoryNotificationQueue, RecordingFlushPort]:
    queue = InMemoryNotificationQueue()
    port = RecordingFlushPort(sends_ok=sends_ok)
    return QueueService(queue, port), queue, port


async def test_a_held_push_waits_for_the_end_of_quiet_hours() -> None:
    service, queue, port = _service()
    assert await service.hold_push(7, "scan_done", "scan:1", _push("Scan", "Done", "scan:1"), not_before=END, now=NOW)
    queue.commit()
    assert await service.flush(NOW + timedelta(hours=1)) == FlushResult()
    assert port.pushes == []
    assert await service.flush(END) == FlushResult(sent=1)
    (user_id, payload) = port.pushes[0]
    assert user_id == 7
    assert payload.model_dump(mode="json") == {
        "title": "Scan",
        "body": "Done",
        "tag": "scan_done:held",
        "data": {"url": "/feedback?row=1", "type": "scan_done", "inbox": True},
    }
    assert await service.flush(END + timedelta(minutes=15)) == FlushResult(), "sent once"


async def test_enqueue_is_idempotent_per_user_type_channel_and_event() -> None:
    service, queue, _port = _service()
    payload = _push("Scan", "Done", "scan:1")
    assert await service.hold_push(7, "scan_done", "scan:1", payload, not_before=END, now=NOW)
    assert not await service.hold_push(7, "scan_done", "scan:1", payload, not_before=END, now=NOW)
    queue.commit()
    assert not await service.hold_push(7, "scan_done", "scan:1", payload, not_before=END, now=NOW)
    assert await service.hold_push(8, "scan_done", "scan:1", payload, not_before=END, now=NOW)
    assert len(queue.rows) + len(queue.pending) == 2


async def test_a_rollback_queues_nothing() -> None:
    """Enqueue is a write in the event's own transaction."""
    service, queue, port = _service()
    await service.queue_mail(RECEIVED, 7, "feedback:1", {"number": 1}, now=NOW)
    queue.rollback()
    assert await service.flush(NOW + timedelta(days=1)) == FlushResult()
    assert port.mails == []


async def test_a_types_held_pushes_go_as_one() -> None:
    service, queue, port = _service()
    for number in (1, 2, 3):
        payload = _push(f"Scan {number}", f"Done {number}", f"scan:{number}", inbox=number != 3)
        more = "+{n} weitere"
        await service.hold_push(
            7, "scan_done", f"scan:{number}", payload, not_before=END, now=NOW + timedelta(minutes=number), more=more
        )
    await service.hold_push(7, "digest", "digest:1", _push("Digest", "Week", "digest:1"), not_before=END, now=NOW)
    queue.commit()
    assert await service.flush(END) == FlushResult(sent=4)
    by_tag = {payload.tag: payload for _user, payload in port.pushes}
    assert sorted(by_tag) == ["digest:held", "scan_done:held"], "one push per person and type"
    collapsed = by_tag["scan_done:held"]
    assert (collapsed.title, collapsed.body, collapsed.data.type) == ("Scan 3", "+2 weitere", "scan_done")
    assert collapsed.data.inbox is False, "the newest's"
    assert by_tag["digest:held"].body == "Week", "one item keeps its own body"


async def test_held_words_must_name_the_count() -> None:
    service, _queue, _port = _service()
    with pytest.raises(ValueError, match=r"\{n\}"):
        await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW, more="more")


async def test_an_event_key_is_checked() -> None:
    service, _queue, _port = _service()
    with pytest.raises(ValueError, match="queue event"):
        await service.queue_mail(RECEIVED, 7, "no spaces please", {}, now=NOW)


async def test_a_push_no_longer_wanted_is_dropped() -> None:
    service, queue, port = _service()
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    queue.commit()
    port.wanted[(7, "scan_done", "push")] = False
    assert await service.flush(END) == FlushResult(dropped=1)
    assert port.pushes == [] and next(iter(queue.rows.values())).dropped == "unwanted"


async def test_a_push_still_in_moved_quiet_hours_is_held_again() -> None:
    service, queue, port = _service()
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    queue.commit()
    later = END + timedelta(hours=1)
    port.wanted[(7, "scan_done", "push")] = later
    assert await service.flush(END) == FlushResult(left=1)
    row = next(iter(queue.rows.values()))
    assert (row.not_before, row.claimed_at, row.attempts) == (later, None, 0), "a deferral is no attempt"
    del port.wanted[(7, "scan_done", "push")]
    assert await service.flush(later) == FlushResult(sent=1)


async def test_a_push_held_past_its_ttl_is_dropped() -> None:
    service, queue, port = _service()
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    queue.commit()
    assert await service.flush(NOW + timedelta(seconds=PUSH_TTL + 1)) == FlushResult(dropped=1)
    assert port.pushes == [] and next(iter(queue.rows.values())).dropped == "expired"


async def test_mails_join_the_open_bundle_and_go_as_one() -> None:
    service, queue, port = _service()
    await service.queue_mail(RECEIVED, 7, "feedback:1", {"number": 1}, now=NOW)
    await service.queue_mail(RECEIVED, 7, "feedback:2", {"number": 2}, now=NOW + timedelta(minutes=10))
    queue.commit()
    await service.queue_mail(RECEIVED, 7, "feedback:3", {"number": 3}, now=NOW + timedelta(minutes=14))
    queue.commit()
    assert {row.not_before for row in queue.rows.values()} == {NOW + timedelta(minutes=15)}
    assert await service.flush(NOW + timedelta(minutes=14)) == FlushResult()
    assert await service.flush(NOW + timedelta(minutes=15)) == FlushResult(sent=3)
    ((user_id, type_id, items, key),) = port.mails
    assert (user_id, type_id) == (7, "feedback_received")
    assert [item.payload["number"] for item in items] == [1, 2, 3]
    assert key == f"queue:{items[0].id}:3"
    # The bundle is closed: the next item opens a new one.
    await service.queue_mail(RECEIVED, 7, "feedback:4", {"number": 4}, now=NOW + timedelta(minutes=20))
    queue.commit()
    assert max(row.not_before for row in queue.rows.values()) == NOW + timedelta(minutes=35)


async def test_a_type_without_a_bundle_goes_at_the_next_flush() -> None:
    service, queue, port = _service()
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    queue.commit()
    assert await service.flush(NOW) == FlushResult(sent=1)
    assert len(port.mails) == 1


async def test_a_failed_send_is_tried_again_after_the_claim_timeout_and_dropped_after_three() -> None:
    service, queue, port = _service(sends_ok=False)
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    queue.commit()
    assert await service.flush(NOW) == FlushResult(left=1)
    assert await service.flush(NOW + timedelta(minutes=5)) == FlushResult(), "still claimed"
    second = NOW + QUEUE_CLAIM_TIMEOUT + timedelta(minutes=1)
    assert await service.flush(second) == FlushResult(left=1)
    third = second + QUEUE_CLAIM_TIMEOUT + timedelta(minutes=1)
    assert await service.flush(third) == FlushResult(dropped=1)
    row = next(iter(queue.rows.values()))
    assert (row.attempts, row.dropped) == (QUEUE_MAX_ATTEMPTS, "failed")
    keys = {key for *_rest, key in port.mails}
    assert keys == {f"queue:{row.id}:1"}, "the idempotency key is stable across a resend"


async def test_a_claim_that_died_is_reclaimed_and_counted() -> None:
    """A run that died after its third claim: the next claim drops it ``failed``."""
    service, queue, port = _service()
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    queue.commit()
    for attempt in range(QUEUE_MAX_ATTEMPTS):
        moment = NOW + attempt * QUEUE_CLAIM_TIMEOUT * 2
        assert len(await queue.claim(moment, 10, moment)) == 1
    later = NOW + QUEUE_CLAIM_TIMEOUT * 10
    assert await service.flush(later) == FlushResult(dropped=1)
    assert port.mails == []


async def test_a_mail_owed_past_its_window_is_dropped_late() -> None:
    service, queue, port = _service()
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    queue.commit()
    assert await service.flush(NOW + timedelta(days=MAIL_LATE_DAYS, minutes=1)) == FlushResult(dropped=1)
    assert next(iter(queue.rows.values())).dropped == "late" and port.mails == []


async def test_the_tick_flushes_then_purges_old_finished_rows() -> None:
    service, queue, port = _service()
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    queue.commit()
    assert await notifications_tick(service, NOW) == FlushResult(sent=1)
    await service.queue_mail(COMMENT, 7, "comment:2", {"number": 2}, now=NOW + QUEUE_RETENTION)
    queue.commit()
    result = await notifications_tick(service, NOW + QUEUE_RETENTION + timedelta(seconds=1))
    assert result == FlushResult(sent=1, purged=1)
    assert [row.event for row in queue.rows.values()] == ["comment:2"]
    assert queue.commits[-1] == "purge" and len(port.mails) == 2


async def test_a_retried_tick_job_sends_nothing_twice() -> None:
    """keksdose runs the tick inside its product-metrics job (``--max-retries=1``): a retry
    after the tick ran, or after it died mid-send, never sends an item a second time."""
    service, queue, port = _service()
    await service.queue_mail(COMMENT, 7, "comment:1", {"number": 1}, now=NOW)
    await service.hold_push(7, "scan_done", "scan:1", _push("Scan", "Done", "scan:1"), not_before=NOW, now=NOW)
    queue.commit()
    assert await notifications_tick(service, NOW) == FlushResult(sent=2)
    assert await notifications_tick(service, NOW + timedelta(minutes=1)) == FlushResult(), "the retry"
    await service.queue_mail(COMMENT, 7, "comment:2", {"number": 2}, now=NOW)
    queue.commit()
    assert len(await queue.claim(NOW, 10, NOW - QUEUE_CLAIM_TIMEOUT)) == 1  # the run that died mid-send
    assert await notifications_tick(service, NOW + timedelta(minutes=2)) == FlushResult(), "still claimed"
    assert (len(port.mails), len(port.pushes)) == (1, 1)


def test_a_flush_claims_one_row_or_more() -> None:
    with pytest.raises(ValueError, match="1 row or more"):
        QueueService(InMemoryNotificationQueue(), RecordingFlushPort(), limit=0)


async def test_a_port_that_takes_now_gets_the_flushs_now() -> None:
    """N8 (0.16.1): ``still_wanted(..., now=)`` judges quiet hours at the flush's own time."""
    service, queue, port = _service()
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    await service.queue_mail(RECEIVED, 7, "fb:1", {}, now=NOW)
    queue.commit()
    late = END + timedelta(hours=1)
    assert await service.flush(late) == FlushResult(sent=2)
    assert port.checked_at == [late, late]


class _PortWithoutNow(RecordingFlushPort):
    """A port written before 0.16.1: ``still_wanted`` takes no ``now``."""

    async def still_wanted(self, user_id: int, type_id: str, channel: QueueChannel) -> bool | datetime:  # type: ignore[override]
        return False


async def test_a_port_without_now_keeps_working() -> None:
    queue = InMemoryNotificationQueue()
    service = QueueService(queue, _PortWithoutNow())
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    queue.commit()
    assert await service.flush(END) == FlushResult(dropped=1)


class _PortWithKeywords(RecordingFlushPort):
    async def still_wanted(  # type: ignore[override]
        self, user_id: int, type_id: str, channel: QueueChannel, **context: datetime
    ) -> bool | datetime:
        self.checked_at.append(context["now"])
        return True


async def test_a_port_taking_keywords_gets_now_too() -> None:
    queue = InMemoryNotificationQueue()
    port = _PortWithKeywords()
    service = QueueService(queue, port)
    await service.hold_push(7, "scan_done", "scan:1", _push("a", "b", "c"), not_before=END, now=NOW)
    queue.commit()
    assert await service.flush(END) == FlushResult(sent=1) and port.checked_at == [END]
