"""HTTP-layer request/response schemas distinct from internal models.

Keeping API schemas separate from internal pydantic models lets us
evolve the wire shape independently — important when this prototype
is later connected to a real staff UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agora.models.candidate import HolderCandidate
from agora.models.request import IllRequest


class HealthResponse(BaseModel):
    status: str
    env: str
    version: str


class SubmitRequestResponse(BaseModel):
    saga_id: UUID
    request: IllRequest


class SagaSummary(BaseModel):
    saga_id: UUID
    request_id: UUID
    current_state: str
    iso18626_state: str | None
    created_at: datetime
    updated_at: datetime
    title: str
    requesting_library: str


class SagaEventOut(BaseModel):
    seq: int
    kind: str
    step: str
    state_before: str
    state_after: str
    actor: str
    idempotency_key: str
    iso_message_id: str | None
    payload: dict[str, Any]
    outcome: str
    rationale: str | None
    ts: datetime


class SagaDetail(BaseModel):
    saga: SagaSummary
    events: list[SagaEventOut]


class ApprovalBody(BaseModel):
    """Staff approval payload.

    The endpoint commits the gate AND runs the forward step in a single
    transaction. ``extras`` lets the caller supply step-specific inputs
    (``chosen_supplier`` for ROUTE, ``reshare_id`` for SHIP/RETURN_ITEM)
    when they cannot be derived from prior committed forward events.
    """

    step: str = Field(description="Saga step the approval applies to")
    actor: str = Field(description="Staff identifier (e.g. 'staff:alice@org')")
    rationale: str
    extras: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Step-specific inputs. Merged on top of values derived from prior "
            "committed forward events. Use this for the first ROUTE call to "
            "supply ``chosen_supplier``."
        ),
    )


class RejectionBody(BaseModel):
    step: str
    actor: str
    rationale: str


class CompensateBody(BaseModel):
    """Staff-initiated compensator invocation."""

    step: str
    actor: str
    rationale: str
    extras: dict[str, Any] | None = Field(
        default=None,
        description="Optional override for derived extras (rare).",
    )


class StepRunResponse(SagaEventOut):
    """Response shape for endpoints that execute a saga step.

    Identical to ``SagaEventOut``; named distinctly so OpenAPI surfaces
    a clear "this is the event we just appended" return type.
    """


class DiscoverBody(BaseModel):
    """Optional payload for ``POST /sagas/{id}/discover``.

    All fields are optional — discovery defaults to running as the
    ``"agent:discovery"`` actor against the saga's stored request.
    """

    actor: str = Field(
        default="agent:discovery",
        description="Actor recorded on the OBSERVATION event.",
    )


class DiscoverResponse(BaseModel):
    """Result of a discovery run.

    Mirrors ``DiscoveryRecommendation`` plus the ledger event reference
    so the staff console can pivot to the timeline. Discovery is
    advisory: no saga state changes, no outbox dispatch — just a
    ``DISCOVERY`` OBSERVATION event with the candidate list and
    rationale.
    """

    saga_id: UUID
    event: SagaEventOut
    candidates: list[HolderCandidate]
    diagnostics: list[str]
    rationale: str
