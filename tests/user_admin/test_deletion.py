"""Deletion in two stages (§2.1, §6.4): deactivated at once, erased after the days or by an operator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eifi1_server_kit.user_admin import (
    BACKUP_RETENTION_DAYS,
    DEFAULT_DELETION_DAYS,
    LOG_RETENTION_DAYS,
    DeletionMode,
    deletion_due,
    deletion_mail_retention_note,
    deletion_schedule,
)

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def test_the_modes_and_the_defaults() -> None:
    assert [str(mode) for mode in DeletionMode] == ["after_days", "operator"]
    assert (DEFAULT_DELETION_DAYS, BACKUP_RETENTION_DAYS, LOG_RETENTION_DAYS) == (30, 7, 30)


def test_the_schedule() -> None:
    assert deletion_schedule(NOW, DeletionMode.AFTER_DAYS) == NOW + timedelta(days=30)
    assert deletion_schedule(NOW, "after_days", 7) == NOW + timedelta(days=7)
    assert deletion_schedule(NOW, "after_days", 0) == NOW
    assert deletion_schedule(NOW, DeletionMode.OPERATOR) is None, "kastlan promises no date"
    naive = datetime(2026, 10, 7, 9, 0)
    scheduled = deletion_schedule(naive, "after_days")
    assert scheduled is not None and scheduled.tzinfo is None, "the column's own form"
    with pytest.raises(ValueError):
        deletion_schedule(NOW, "never")
    with pytest.raises(ValueError, match="zero days or more"):
        deletion_schedule(NOW, "after_days", -1)


def test_due_at_the_boundary() -> None:
    requested = NOW - timedelta(days=30)
    assert deletion_due(requested, 30, NOW)
    assert not deletion_due(requested, 30, NOW - timedelta(microseconds=1))
    assert not deletion_due(None, 30, NOW), "no request, or one an admin cancelled"
    assert deletion_due(datetime(2026, 9, 7, 9, 0), 30, NOW), "naive is UTC"
    with pytest.raises(ValueError):
        deletion_due(requested, -1, NOW)


def test_a_shortened_window_never_erases_before_the_promised_date() -> None:
    requested = NOW - timedelta(days=10)
    promised = requested + timedelta(days=30)
    assert deletion_due(requested, 7, NOW), "the setting alone says yes"
    assert not deletion_due(requested, 7, NOW, scheduled_at=promised)
    assert deletion_due(requested, 7, promised, scheduled_at=promised)


def test_the_retention_note() -> None:
    """keksdose's production figures: backups and deleted files within 7 days, logs within 30."""
    assert deletion_mail_retention_note() == {"backup_days": 7, "log_days": 30}
    assert deletion_mail_retention_note(backup_days=14, log_days=90) == {"backup_days": 14, "log_days": 90}
    template = "copies in database backups and deleted files age out within {backup_days} days of the erasure, server logs within {log_days}"
    assert template.format(**deletion_mail_retention_note()).endswith(
        "within 7 days of the erasure, server logs within 30"
    )
    with pytest.raises(ValueError, match="log_days"):
        deletion_mail_retention_note(log_days=0)
