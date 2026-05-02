"""Saga ledger event models.

The ledger is append-only; ``SagaEvent`` is what's read out, while
``NewSagaEvent`` is the pre-insert shape (no ``id`` or ``ts`` yet).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome


class NewSagaEvent(BaseModel):
    """Caller-side payload for appending an event to the ledger."""

    model_config = ConfigDict(frozen=True)

    saga_id: UUID
    kind: EventKind
    step: StepName
    state_before: LifecycleState
    state_after: LifecycleState
    actor: str
    idempotency_key: str
    iso_message_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    outcome: StepOutcome
    rationale: str | None = None


class SagaEvent(BaseModel):
    """A persisted event row read back from the ledger."""

    model_config = ConfigDict(frozen=True)

    id: int
    saga_id: UUID
    seq: int
    kind: EventKind
    step: StepName
    state_before: LifecycleState
    state_after: LifecycleState
    actor: str
    idempotency_key: str
    iso_message_id: str | None
    payload: dict[str, Any]
    outcome: StepOutcome
    rationale: str | None
    ts: datetime
