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

# Audit 2026-05-09 #15: ``extras`` was previously ``dict[str, Any]``
# which let attacker-controlled content land in saga step inputs —
# template-fragment ``chosen_supplier`` strings into Jinja, surprise
# ``loan_period_days`` integers into timedelta math, etc. Tight pydantic
# typing here is the load-bearing primary defense; the typed model also
# documents which extras the API actually consumes (the unconstrained
# dict was a forever-grow surface).


class StepExtras(BaseModel):
    """Strict shape for ApprovalBody / CompensateBody ``extras``.

    Every saga step that takes input from the request body's ``extras``
    is enumerated here. Unknown keys are rejected by ``extra="forbid"``
    so a future addition gets a typed field rather than slipping in via
    ``dict[str, Any]``. All values are validated for shape AND length —
    string fields use a tight regex to refuse control characters /
    template fragments / path-traversal segments at the API boundary.
    """

    model_config = {"extra": "forbid"}

    chosen_supplier: str | None = Field(
        default=None,
        max_length=64,
        # ISIL-symbol-friendly: alphanumerics + dash + dot + slash
        # (some symbols use "US-CST/Y" style). Refuses HTML / quotes /
        # template syntax / path separators.
        pattern=r"^[A-Za-z0-9.\-/]{1,64}$",
        description="ISIL agency symbol picked by ROUTE forward.",
    )
    reshare_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]{1,128}$",
        description="Supplier-assigned patron-request id from ReShare.",
    )
    loan_period_days: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="Loan window for SHIP forward; bounded to a year.",
    )
    extension_days: int | None = Field(
        default=None,
        ge=1,
        le=180,
        description="Renewal extension; matches RenewBody.extension_days.",
    )


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

    step: str = Field(max_length=32, description="Saga step the approval applies to")
    actor: str = Field(
        max_length=64, description="Staff identifier (e.g. 'staff:alice@org')"
    )
    rationale: str = Field(max_length=1024)
    extras: StepExtras | None = Field(
        default=None,
        description=(
            "Step-specific inputs. Merged on top of values derived from prior "
            "committed forward events. Use this for the first ROUTE call to "
            "supply ``chosen_supplier``."
        ),
    )


class RejectionBody(BaseModel):
    step: str = Field(max_length=32)
    actor: str = Field(max_length=64)
    rationale: str = Field(max_length=1024)


class CompensateBody(BaseModel):
    """Staff-initiated compensator invocation."""

    step: str = Field(max_length=32)
    actor: str = Field(max_length=64)
    rationale: str = Field(max_length=1024)
    extras: StepExtras | None = Field(
        default=None,
        description="Optional override for derived extras (rare).",
    )


class StepRunResponse(SagaEventOut):
    """Response shape for endpoints that execute a saga step.

    Identical to ``SagaEventOut``; named distinctly so OpenAPI surfaces
    a clear "this is the event we just appended" return type.
    """


class OverrideBody(BaseModel):
    """Staff-initiated override to resolve a DISPUTED saga.

    Allowed targets: ``cancelled`` or ``unfilled``.  Writes a ledger
    OBSERVATION (``step=resolve``) with ``outcome=committed`` so
    ``saga.current_state`` advances atomically.  No outbox dispatch —
    any open ILS loans must be settled out-of-band by staff.
    """

    target_state: str = Field(
        max_length=32,
        description="Terminal state to force the saga into ('cancelled' or 'unfilled').",
    )
    actor: str = Field(max_length=64, description="Staff identifier (e.g. 'staff:alice@org')")
    rationale: str = Field(
        max_length=1024,
        description="Mandatory reason recorded on the ledger event.",
    )


class RenewBody(BaseModel):
    """Patron renewal request payload.

    Extends the loan by ``extension_days`` days from today. Staff commits
    the gate and runs the RENEW forward in one transaction. The saga stays
    at RECEIVED; the new due date lands on the ledger event payload.
    """

    actor: str = Field(max_length=64, description="Staff or patron identifier")
    rationale: str = Field(max_length=1024)
    extension_days: int = Field(
        default=28,
        ge=1,
        le=180,
        description="Number of days to extend the loan from today.",
    )


class DiscoverBody(BaseModel):
    """Optional payload for ``POST /sagas/{id}/discover``.

    All fields are optional — discovery defaults to running as the
    ``"agent:discovery"`` actor against the saga's stored request.
    """

    actor: str = Field(
        default="agent:discovery",
        max_length=64,
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
