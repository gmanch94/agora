"""Saga step flows: forward + compensator pairs registered with the registry.

Every ReShare-touching forward step is now pure with respect to
external systems: the step returns an ``OutboxIntent`` on its
``StepResult`` and the coordinator enqueues it in the same transaction
as the ledger event. The outbox worker drains the row onto the wire
asynchronously. APPROVE forward used to call ``submit_to_supplier``
inline so the saga ledger could stamp the supplier-assigned
``reshare_id`` onto its forward-event payload; ADR-0012 migrated this
to a dedicated ``LifecycleState.APPROVING`` intermediate state plus a
worker projection that writes the supplier ack as an OBSERVATION
event. Net result: ``approve_forward`` is a ledger write + an outbox
row, no synchronous wire call.

The factory ``register_default_flows`` wires the global registry;
tests can call ``build_registry`` to get an isolated registry instead.

The ``transaction`` argument to ``build_registry`` /
``register_default_flows`` is currently unused — no flow calls
``TransactionAgent`` inline. It is retained as a parameter so future
flows that need an inline call (e.g. fast-path local checks) can
reach for it without re-threading dependencies through callers. See
ADR-0011 + ADR-0012.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agora.agents.transaction import TransactionAgent
from agora.models.lifecycle import LifecycleState, StepName
from agora.saga.context import SagaContext
from agora.saga.steps import (
    OutboxIntent,
    StepRegistry,
    StepResult,
    get_global_registry,
)

DEFAULT_LOAN_PERIOD_DAYS = 28


def build_registry(transaction: TransactionAgent) -> StepRegistry:
    """Construct a fresh registry wired to ``transaction``.

    Used in tests to keep isolated step registrations per test case.
    """
    reg = StepRegistry()
    _wire(reg, transaction)
    return reg


def register_default_flows(transaction: TransactionAgent) -> StepRegistry:
    """Wire flows into the global registry; idempotent across calls."""
    reg = get_global_registry()
    _wire(reg, transaction)
    return reg


def _wire(reg: StepRegistry, tx: TransactionAgent) -> None:
    # ``tx`` is retained for forward compatibility but no current flow
    # invokes it (every wire call now goes through the outbox). See
    # the module docstring.
    _ = tx

    # ----- SUBMIT --------------------------------------------------
    async def submit_forward(ctx: SagaContext) -> StepResult:
        # Submission is a pure ledger write — no external call yet.
        return StepResult(
            state_after=LifecycleState.SUBMITTED,
            payload={"submitted_by": ctx.actor},
            rationale="Patron-submitted ILL request entered the saga.",
        )

    async def submit_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        return StepResult(
            state_after=LifecycleState.CANCELLED,
            payload={"cancelled_at": "submit"},
            rationale="Saga cancelled before any peer was contacted.",
        )

    reg.register(
        name=StepName.SUBMIT,
        forward=submit_forward,
        compensator=submit_compensator,
        description="Patron submission; no external call yet.",
    )

    # ----- ROUTE ---------------------------------------------------
    async def route_forward(ctx: SagaContext) -> StepResult:
        chosen = ctx.extras.get("chosen_supplier")
        if not chosen:
            raise ValueError("ctx.extras['chosen_supplier'] is required for route step")
        return StepResult(
            state_after=LifecycleState.ROUTED,
            payload={"supplier_symbol": chosen},
            rationale=f"Routed to {chosen} per ranking.",
        )

    async def route_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        return StepResult(
            state_after=LifecycleState.SUBMITTED,
            payload={"reroute_from": fwd_payload.get("supplier_symbol")},
            rationale="Routing reverted; saga returned to Submitted for re-rank.",
        )

    reg.register(
        name=StepName.ROUTE,
        forward=route_forward,
        compensator=route_compensator,
    )

    # ----- APPROVE -------------------------------------------------
    # Per ADR-0012: the forward step is pure (state → APPROVING + one
    # OutboxIntent). The worker drains the row, calls
    # ``ReShareClient.send_request``, and the projection callback
    # writes the supplier-assigned ``reshare_id`` back as an
    # OBSERVATION event that advances the saga to APPROVED.
    # Downstream SHIP/RETURN read ``reshare_id`` via
    # ``_derive_extras`` from that OBSERVATION.
    async def approve_forward(ctx: SagaContext) -> StepResult:
        supplier = ctx.extras.get("chosen_supplier")
        if not supplier:
            raise ValueError("ctx.extras['chosen_supplier'] is required for approve step")
        return StepResult(
            state_after=LifecycleState.APPROVING,
            payload={"supplier_symbol": supplier},
            rationale=(
                f"Saga moved to Approving; submit-to-supplier enqueued "
                f"for asynchronous delivery via outbox worker "
                f"(supplier={supplier})."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "send_request",
                        "args": {
                            "request_payload": {
                                "request_id": str(ctx.request.request_id),
                                "item": ctx.request.item.model_dump(),
                                "patron": ctx.request.patron.model_dump(),
                                "requesting_library":
                                    ctx.request.requesting_library.model_dump(),
                                "type": ctx.request.request_type.value,
                            },
                            "supplier_symbol": supplier,
                        },
                    },
                )
            ],
        )

    async def approve_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        # The supplier-assigned reshare_id no longer rides on the
        # APPROVE forward payload (ADR-0012 moved it to the
        # OBSERVATION projected by the outbox worker). The API and
        # tests surface it via ``ctx.extras['reshare_id']`` —
        # ``api._derive_extras`` walks both FORWARD and OBSERVATION
        # events when assembling extras for the compensator.
        # ``fwd_payload`` is checked first for backwards-compat with
        # any historical sagas where the inline forward did stamp it.
        reshare_id = fwd_payload.get("reshare_id") or ctx.extras.get("reshare_id")
        if not reshare_id:
            # No reshare_id means the supplier ack hasn't landed yet
            # (saga still APPROVING, outbox row pending or failing) —
            # there is nothing concrete at the supplier to cancel.
            # Surface a specific error so the API turns it into a 409
            # the staff console can explain.
            raise ValueError(
                "approve compensator cannot run while supplier ack pending: "
                "no reshare_id on prior forward payload or context extras "
                "(saga is likely still APPROVING and the outbox row has "
                "not been delivered)"
            )
        return StepResult(
            state_after=LifecycleState.CANCELLED,
            payload={"reshare_id": reshare_id},
            rationale=(
                "Saga terminal Cancelled. Cancel-at-supplier enqueued for "
                "asynchronous delivery via outbox worker."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "cancel_request",
                        "args": {
                            "reshare_id": reshare_id,
                            "reason": "approval revoked",
                        },
                    },
                )
            ],
        )

    reg.register(
        name=StepName.APPROVE,
        forward=approve_forward,
        compensator=approve_compensator,
    )

    # ----- SHIP ----------------------------------------------------
    async def ship_forward(ctx: SagaContext) -> StepResult:
        reshare_id = ctx.extras.get("reshare_id")
        if not reshare_id:
            raise ValueError("ctx.extras['reshare_id'] is required for ship step")
        loan_days = int(
            ctx.extras.get("loan_period_days") or DEFAULT_LOAN_PERIOD_DAYS
        )
        shipped_at = datetime.now(UTC)
        due_at = shipped_at + timedelta(days=loan_days)
        return StepResult(
            state_after=LifecycleState.SHIPPED,
            payload={
                "reshare_id": reshare_id,
                "shipped_at": shipped_at.isoformat(),
                "due_at": due_at.isoformat(),
                "loan_period_days": loan_days,
            },
            rationale=(
                f"Saga moved to Shipped; due {due_at.date().isoformat()}; "
                "supplier mark-shipped enqueued for asynchronous delivery "
                "via outbox worker."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "confirm_shipment",
                        "args": {"reshare_id": reshare_id},
                    },
                )
            ],
        )

    async def ship_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        reshare_id = fwd_payload.get("reshare_id")
        if not reshare_id:
            raise ValueError("ship compensator missing reshare_id from forward payload")
        # NB: HttpReShareClient.recall_request currently raises (mod-rs has
        # no first-class recall action). Under the outbox pattern that
        # surfaces as a dead-letter row for staff review — exactly the
        # signal we want until the recall mapping is verified. The mock
        # client succeeds, which keeps the demo + tests green. See ADR-0011.
        return StepResult(
            state_after=LifecycleState.DISPUTED,
            payload={"reshare_id": reshare_id},
            rationale=(
                "Recall enqueued; physical item may already be in transit. "
                "Saga marked Disputed for staff intervention."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "recall_request",
                        "args": {
                            "reshare_id": reshare_id,
                            "reason": "ship-step compensator: recall",
                        },
                    },
                )
            ],
        )

    reg.register(
        name=StepName.SHIP,
        forward=ship_forward,
        compensator=ship_compensator,
    )

    # ----- RETURN --------------------------------------------------
    async def return_forward(ctx: SagaContext) -> StepResult:
        reshare_id = ctx.extras.get("reshare_id")
        if not reshare_id:
            raise ValueError("ctx.extras['reshare_id'] is required for return step")
        return StepResult(
            state_after=LifecycleState.RETURNED,
            payload={"reshare_id": reshare_id},
            rationale=(
                "Saga moved to Returned; borrower-returned message enqueued "
                "for asynchronous delivery via outbox worker."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "confirm_return",
                        "args": {"reshare_id": reshare_id},
                    },
                )
            ],
        )

    async def return_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        return StepResult(
            state_after=LifecycleState.DISPUTED,
            payload={"reshare_id": fwd_payload.get("reshare_id")},
            rationale="Return disputed; opening manual reconciliation case.",
        )

    reg.register(
        name=StepName.RETURN_ITEM,
        forward=return_forward,
        compensator=return_compensator,
    )
