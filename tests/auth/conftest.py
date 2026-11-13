"""The account layer's test worlds (``tests/auth/_world.py``) as fixtures."""

from collections.abc import AsyncIterator

import pytest

from tests.auth._world import World


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = World()
    async with built.client:
        yield built


@pytest.fixture
async def cutoff_world() -> AsyncIterator[World]:
    """An app that keeps the ``iat`` cut-off and demos (keksdose, Kurvenschmiede)."""
    built = World(cutoff=True)
    async with built.client:
        yield built
