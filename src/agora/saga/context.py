"""SagaContext — bundle threaded through saga step functions.

Carries the request, current state snapshot, idempotency key, and
ledger handle. Step functions read from the context and write events
back to the ledger via the same context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agora.models.lifecycle import LifecycleState
from agora.models.request import IllRequest


@dataclass(slots=True)
class SagaContext:
    """Per-step execution context.

    Attributes:
        saga_id: Stable id for the whole saga.
        request: The full request payload.
        current_state: User-facing lifecycle state at start of step.
        idempotency_key: ULID-based key for the operation about to run.
        actor: Who is invoking the step (``"agent:routing"``,
            ``"staff:alice@example.org"``, ``"system"``, ...).
        extras: Free-form scratchpad agents can populate during a step.
    """

    saga_id: UUID
    request: IllRequest
    current_state: LifecycleState
    idempotency_key: str
    actor: str
    extras: dict[str, Any] = field(default_factory=dict)
