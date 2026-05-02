"""ReconciliationAgent — runs paired compensators when forward steps fail.

The agent itself is thin: it asks the coordinator to run the
compensator for a given step. The coordinator validates that a
committed forward step exists before it fires the compensator, so
this agent cannot accidentally roll back something that never happened.
"""

from __future__ import annotations

from agora.models.lifecycle import StepName
from agora.saga.context import SagaContext
from agora.saga.coordinator import Coordinator


class ReconciliationAgent:
    """Trigger compensators on demand."""

    def __init__(self, coordinator: Coordinator):
        self._coord = coordinator

    async def compensate(self, *, ctx: SagaContext, step: StepName) -> None:
        """Run the compensator paired with the most recent committed forward."""
        await self._coord.run_compensator(ctx=ctx, step=step)
