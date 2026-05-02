"""TrackingAgent — turns ReShare/NCIP observations into ledger events.

This is a stub for the prototype. In production it would subscribe to
ReShare webhooks and run periodic sweeps for overdue items. Here it
exposes a single ``observe`` entry point that callers (tests, the
demo) can use to inject status updates into the saga ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agora.models.lifecycle import StepName
from agora.saga.coordinator import Coordinator


@dataclass(slots=True)
class Observation:
    saga_id: UUID
    step: StepName
    payload: dict[str, Any]
    rationale: str | None = None
    actor: str = "agent:tracking"


class TrackingAgent:
    """Append observations to the saga ledger.

    Observations don't change lifecycle state — they record information
    about an in-flight saga (e.g. ``due_date_set``, ``overdue_warning``,
    ``ils_check_in``). The coordinator decides whether to escalate.
    """

    def __init__(self, coordinator: Coordinator):
        self._coord = coordinator

    async def observe(self, obs: Observation) -> None:
        await self._coord.record_observation(
            saga_id=obs.saga_id,
            step=obs.step,
            actor=obs.actor,
            payload=obs.payload,
            rationale=obs.rationale,
        )
