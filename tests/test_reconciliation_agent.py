"""Unit tests for ReconciliationAgent.

The agent is a thin wrapper around Coordinator.run_compensator — these
tests verify the wiring without hitting a real database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agora.agents.reconciliation import ReconciliationAgent
from agora.models.lifecycle import StepName
from agora.saga.context import SagaContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent() -> tuple[ReconciliationAgent, AsyncMock]:
    """Return an agent backed by a mock coordinator."""
    coord = MagicMock()
    coord.run_compensator = AsyncMock(return_value=None)
    return ReconciliationAgent(coord), coord.run_compensator


def _make_ctx() -> MagicMock:
    ctx = MagicMock(spec=SagaContext)
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconciliation_agent_stores_coordinator() -> None:
    """__init__ stores coordinator (line 20)."""
    coord = MagicMock()
    agent = ReconciliationAgent(coord)
    assert agent._coord is coord


@pytest.mark.asyncio
async def test_compensate_delegates_to_coordinator() -> None:
    """compensate() calls coordinator.run_compensator with correct args (line 24)."""
    agent, mock_run = _make_agent()
    ctx = _make_ctx()

    await agent.compensate(ctx=ctx, step=StepName.SUBMIT)

    mock_run.assert_awaited_once_with(ctx=ctx, step=StepName.SUBMIT)
