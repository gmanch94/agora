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

RECEIVE and RETURN forward each emit a ``target="ncip"`` intent
against the borrower's local ILS (``check_out`` on RECEIVE,
``check_in`` on RETURN); RETURN additionally emits a
``target="reshare"`` ``confirm_return`` intent. SHIP forward emits
only the ReShare ``confirm_shipment`` — the borrower-side NCIP
``check_out`` was re-anchored from SHIP to RECEIVE so the patron's
ILS record reflects an outstanding loan from the moment the patron
*physically receives* the item, not the moment the supplier ships it.
NCIP dispatch is fire-and-forget — its outcome does not gate saga
state — so no projection callback is registered for
``target="ncip"``; failure surfaces as a stuck outbox row for staff
review. SHIP compensator emits a single ReShare ``recall_request`` in
either branch (saga at SHIPPED or post-RECEIVE): with NCIP
``check_out`` anchored to RECEIVE, neither branch needs a
compensating ``check_in`` — at SHIPPED no ILS loan was ever opened,
and at RECEIVED the patron physically holds the book so the loan
correctly reflects current custody and the eventual return flow owns
``check_in``. The ``current_state`` branch survives only as
state-aware rationale text; functionally both branches enqueue the
same recall.

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
    # Emits a single outbox intent: ReShare ``confirm_shipment`` for
    # the consortium peer. Borrower-side NCIP ``check_out`` is
    # anchored on the *RECEIVE* forward, not here — see RECEIVE block
    # below for rationale.
    #
    # ``due_at`` deliberately anchors to ``shipped_at`` (not the
    # eventual borrower-receipt time): the loan-period clock is a
    # supplier-side commitment that starts at shipment, and a saga
    # whose patron never confirms receipt would otherwise never hit
    # the overdue threshold. TrackingAgent's overdue scan reads
    # ``due_at`` from this payload.
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
                ),
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
        #
        # NCIP rollback considerations after the SHIP→RECEIVE re-anchor
        # (this PR): the compensator no longer needs to issue any NCIP
        # call regardless of saga state. Walking both branches to
        # confirm:
        #
        #   * Saga at SHIPPED — RECEIVE forward never ran, so no ILS
        #     ``check_out`` was ever dispatched. Nothing for the
        #     compensator to roll back. Just enqueue the recall.
        #   * Saga at RECEIVED (or beyond) — the RECEIVE forward
        #     opened the ILS loan. The patron *physically holds* the
        #     book, so the loan record is correct as a statement of
        #     current custody. Issuing a compensating ``check_in``
        #     would lie to the ILS; the eventual return flow (or a
        #     manual reconciliation case) is responsible for the
        #     real ``check_in``.
        #
        # Both branches converge on "just recall." The
        # ``current_state`` check survives only as state-aware
        # rationale text so the staff console can render the right
        # explanation; functionally the outbox payload is identical.
        # The state-aware NCIP fan-out that lived here previously
        # (PR #37) compensated for an earlier prototype shortcut where
        # SHIP forward dispatched the NCIP ``check_out``; once the
        # anchor moved to RECEIVE that branch became dead code. See
        # docs/lessons.md § Saga / ledger for the post-mortem.
        intents: list[OutboxIntent] = [
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
        ]
        if ctx.current_state == LifecycleState.SHIPPED:
            rationale = (
                "Recall enqueued; patron never received the item, so no "
                "ILS loan exists to clear (NCIP check-out anchors to the "
                "RECEIVE forward, which has not run). Saga marked Disputed "
                "for staff intervention."
            )
        else:
            rationale = (
                "Recall enqueued; patron currently holds the item, so the "
                "ILS loan correctly reflects custody and is left in place "
                "(the eventual return flow owns check-in). Saga marked "
                "Disputed for staff intervention."
            )
        return StepResult(
            state_after=LifecycleState.DISPUTED,
            payload={"reshare_id": reshare_id},
            rationale=rationale,
            outbox=intents,
        )

    reg.register(
        name=StepName.SHIP,
        forward=ship_forward,
        compensator=ship_compensator,
    )

    # ----- RECEIVE -------------------------------------------------
    # Borrower-side confirmation that the physical item arrived. ISO
    # 18626 maps this to a ``RequestingAgencyMessage`` "ItemReceived"
    # note; the supplier-side state stays ``Loaned`` so there is no
    # peer status flip to drive — that's why no ``target="reshare"``
    # intent is emitted here.
    #
    # NCIP ``check_out`` is anchored on this step (not SHIP). Anchoring
    # at borrower-receipt rather than supplier-shipment is the
    # correct circulation-timing model: the patron's ILS record
    # reflects the loan from the moment they physically take custody,
    # not from the moment the lender ships it. This re-anchor was
    # tracked as a known-gap on the prior anchor; resolved in this PR.
    #
    # Trade-off captured here so the next reader doesn't have to
    # re-derive: a saga whose patron never confirms receipt will
    # never have a ``check_out`` dispatched. TrackingAgent can flag
    # "shipped > N days ago, no RECEIVE confirmation" but recovery
    # requires staff intervention; the current scanner does not yet
    # implement that tier-3 watch.
    #
    # ``item_id`` resolution: prefer the supplying library's ILS barcode
    # (``ctx.request.item.item_barcode``) when staff provided one at
    # request submission; fall back to ``reshare_id`` otherwise. The
    # fallback keeps the pre-existing approximation for sagas created
    # before the barcode field was added. Idempotency-key suffix
    # ``:ncip`` is uniform across all NCIP rows.
    async def receive_forward(ctx: SagaContext) -> StepResult:
        reshare_id = ctx.extras.get("reshare_id")
        if not reshare_id:
            raise ValueError("ctx.extras['reshare_id'] is required for receive step")
        item_id = ctx.request.item.item_barcode or reshare_id
        return StepResult(
            state_after=LifecycleState.RECEIVED,
            payload={"reshare_id": reshare_id},
            rationale=(
                "Borrower confirmed physical receipt; supplier still holds "
                "Loaned — saga moved to Received; borrower-side NCIP "
                "check-out enqueued for asynchronous delivery via outbox "
                "worker."
            ),
            outbox=[
                OutboxIntent(
                    target="ncip",
                    idempotency_key=f"{ctx.idempotency_key}:ncip",
                    payload={
                        "action": "check_out",
                        "args": {
                            "item_id": item_id,
                            "patron_id": ctx.request.patron.patron_id,
                        },
                    },
                ),
            ],
        )

    async def receive_compensator(
        ctx: SagaContext, fwd_payload: dict[str, Any]
    ) -> StepResult:
        # Receipt is physical — un-undoable. Compensator records the
        # contradiction and lets staff resolve via reconciliation.
        #
        # Scope note (this PR): the RECEIVE forward now opens an ILS
        # loan via NCIP ``check_out``. This compensator deliberately
        # does *not* emit a paired ``check_in`` — the saga can't tell
        # whether the dispute is about non-receipt (loan should clear)
        # or condition (loan should stay). Routing to DISPUTED for
        # staff resolution preserves the "physically un-undoable"
        # framing; a future PR may add a state-aware compensator (or
        # a `/sagas/{id}/override` endpoint) once the staff console
        # surfaces the necessary inputs. Documented in CLAUDE.md
        # known-gaps.
        return StepResult(
            state_after=LifecycleState.DISPUTED,
            payload={"reshare_id": fwd_payload.get("reshare_id")},
            rationale=(
                "Receipt disputed; physical custody contested. ILS loan "
                "recorded by RECEIVE forward is left in place — saga marked "
                "Disputed so staff can reconcile against ground truth."
            ),
        )

    reg.register(
        name=StepName.RECEIVE,
        forward=receive_forward,
        compensator=receive_compensator,
    )

    # ----- RETURN --------------------------------------------------
    # Emits two outbox intents: (1) ReShare ``confirm_return`` for the
    # consortium peer, (2) NCIP ``check_in`` against the borrower's
    # local ILS so the loan opened by RECEIVE forward clears off the
    # patron's record. ``item_id`` resolution same as RECEIVE: prefer
    # ``item_barcode`` when present, fall back to ``reshare_id``.
    async def return_forward(ctx: SagaContext) -> StepResult:
        reshare_id = ctx.extras.get("reshare_id")
        if not reshare_id:
            raise ValueError("ctx.extras['reshare_id'] is required for return step")
        item_id = ctx.request.item.item_barcode or reshare_id
        return StepResult(
            state_after=LifecycleState.RETURNED,
            payload={"reshare_id": reshare_id},
            rationale=(
                "Saga moved to Returned; borrower-returned message + "
                "borrower-side NCIP check-in enqueued for asynchronous "
                "delivery via outbox worker."
            ),
            outbox=[
                OutboxIntent(
                    target="reshare",
                    idempotency_key=ctx.idempotency_key,
                    payload={
                        "action": "confirm_return",
                        "args": {"reshare_id": reshare_id},
                    },
                ),
                OutboxIntent(
                    target="ncip",
                    idempotency_key=f"{ctx.idempotency_key}:ncip",
                    payload={
                        "action": "check_in",
                        "args": {"item_id": item_id},
                    },
                ),
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
