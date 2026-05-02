"""Saga step flows: forward + compensator pairs registered with the registry.

APPROVE forward keeps an inline call to ``TransactionAgent.submit_to_supplier``
because the saga ledger needs the returned ``reshare_id`` stamped onto
its forward-event payload (downstream SHIP/RETURN read it back via
``_derive_extras``). Every other ReShare-touching step has been
migrated to the outbox pattern: the step returns an ``OutboxIntent``
on its ``StepResult`` and the coordinator enqueues it in the same
transaction as the ledger event. The outbox worker drains the row
onto the wire asynchronously. See ADR-0011.

The factory ``register_default_flows`` wires the global registry;
tests can call ``build_registry`` to get an isolated registry instead.
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
    async def approve_forward(ctx: SagaContext) -> StepResult:
        supplier = ctx.extras.get("chosen_supplier")
        if not supplier:
            raise ValueError("ctx.extras['chosen_supplier'] is required for approve step")
        result = await tx.submit_to_supplier(
            idempotency_key=ctx.idempotency_key,
            request_payload={
                "request_id": str(ctx.request.request_id),
                "item": ctx.request.item.model_dump(),
                "patron": ctx.request.patron.model_dump(),
                "requesting_library": ctx.request.requesting_library.model_dump(),
                "type": ctx.request.request_type.value,
            },
            supplier_symbol=supplier,
        )
        return StepResult(
            state_after=LifecycleState.APPROVED,
            payload={
                "reshare_id": result.reshare_id,
                "supplier_symbol": result.supplier_symbol,
                "iso_state": result.state,
            },
            iso_message_id=result.iso_message_id,
            rationale=f"Approved; ReShare id {result.reshare_id} at supplier {supplier}.",
        )

    async def approve_compensator(ctx: SagaContext, fwd_payload: dict[str, Any]) -> StepResult:
        reshare_id = fwd_payload.get("reshare_id")
        if not reshare_id:
            raise ValueError("approve compensator missing reshare_id from forward payload")
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
