"""The admin router's test worlds (``tests/user_admin/_admin_world.py``) as fixtures."""

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from eifi1_server_kit.user_admin import DeletionMode
from tests.auth._world import POLICY
from tests.user_admin._admin_world import AdminWorld


@pytest.fixture
async def world() -> AsyncIterator[AdminWorld]:
    built = AdminWorld()
    async with built.client:
        yield built


@pytest.fixture
async def cutoff_world() -> AsyncIterator[AdminWorld]:
    """An app that keeps the ``iat`` cut-off and demos (keksdose, Kurvenschmiede)."""
    built = AdminWorld(cutoff=True)
    async with built.client:
        yield built


@pytest.fixture
async def operator_world() -> AsyncIterator[AdminWorld]:
    """An app whose operator erases (Kurvenschmiede, kastlan's platform), mail on the console."""
    built = AdminWorld(policy=replace(POLICY, deletion_mode=DeletionMode.OPERATOR), mail_backend="console")
    async with built.client:
        yield built
