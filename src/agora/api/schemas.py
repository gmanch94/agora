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
    step: str = Field(description="Saga step the approval applies to")
    actor: str = Field(description="Staff identifier (e.g. 'staff:alice@org')")
    rationale: str


class RejectionBody(BaseModel):
    step: str
    actor: str
    rationale: str


class CompensateBody(BaseModel):
    step: str
    actor: str
    rationale: str
