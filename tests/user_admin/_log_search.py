"""The promises of 0.31's ``AdminActionsByEmail`` (G9), asked of the kit's two logs: the rows
on one address, an invitation's without an account among them, narrowed by the account,
newest first and paged."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from eifi1_server_kit.clock import utc_now
from eifi1_server_kit.user_admin import AdminAction
from eifi1_server_kit.user_admin.audit import admin_action_record
from eifi1_server_kit.user_admin.ports import AdminActionsByEmail


async def assert_pages_by_email(log: Any, *, actor_id: int, user_id: int) -> None:
    assert isinstance(cast(object, log), AdminActionsByEmail)
    now = utc_now()
    address, other = "search-one@example.org", "search-two@example.org"

    async def add(minutes: int, action: AdminAction, email: str, target: int | None) -> int:
        record = admin_action_record(
            action, actor_id=actor_id, target_user_id=target, target_email=email, now=now + timedelta(minutes=minutes)
        )
        return int((await log.add(record)).id)

    invited = await add(1, AdminAction.INVITE, address, None)
    resent = await add(2, AdminAction.INVITE_RESEND, address, None)
    deactivated = await add(3, AdminAction.DEACTIVATE, address, user_id)
    await add(4, AdminAction.INVITE, other, None)

    rows, total = await log.page_by_email(target_email=address, target_user_id=None, limit=2, offset=0)
    assert total == 3 and [row.id for row in rows] == [deactivated, resent]
    rows, total = await log.page_by_email(target_email=address, target_user_id=None, limit=2, offset=2)
    assert total == 3 and [row.id for row in rows] == [invited]
    rows, total = await log.page_by_email(target_email=address, target_user_id=user_id, limit=10, offset=0)
    assert total == 1 and [row.id for row in rows] == [deactivated]
    rows, total = await log.page_by_email(target_email="nobody@example.org", target_user_id=None, limit=10, offset=0)
    assert (list(rows), total) == ([], 0)
