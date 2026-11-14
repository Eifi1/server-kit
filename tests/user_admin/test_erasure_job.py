"""``erase_due_accounts``: the ``after_days`` job, one account per transaction (user-admin §6.4)."""

import logging

import pytest

from eifi1_server_kit.user_admin.service import ErasureRun, erase_due_accounts


async def test_each_account_is_its_own_transaction_and_a_failure_holds_back_no_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    asked: list[int] = []

    async def erase_one(user_id: int) -> bool:
        asked.append(user_id)
        if user_id == 2:
            raise RuntimeError("the database went away")
        return user_id != 3

    with caplog.at_level(logging.ERROR, logger="eifi1_server_kit.user_admin"):
        run = await erase_due_accounts([1, 2, 3, 4], erase_one)
    assert asked == [1, 2, 3, 4]
    assert run == ErasureRun(erased=(1, 4), skipped=(3,), failed=(2,))
    assert "erasing account 2 failed" in caplog.text


async def test_nothing_due_is_an_empty_run() -> None:
    async def erase_one(user_id: int) -> bool:
        raise AssertionError("never asked")

    assert await erase_due_accounts([], erase_one) == ErasureRun()
