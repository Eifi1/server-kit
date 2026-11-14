"""The teams' test world (``tests/teams/_teams_world.py``) as a fixture."""

from collections.abc import AsyncIterator

import pytest

from tests.teams._teams_world import TeamsWorld


@pytest.fixture
async def world() -> AsyncIterator[TeamsWorld]:
    built = TeamsWorld()
    async with built.client:
        yield built
