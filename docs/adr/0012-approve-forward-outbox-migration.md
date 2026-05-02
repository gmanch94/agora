# ADR 0012 — Migrate APPROVE forward to the outbox pattern

**Status:** Proposed
**Date:** 2026-05-02
**Supersedes (in part):** ADR-0011 §"Why APPROVE stays inline"

## Context

ADR-0011 migrated SHIP, RETURN, and the APPROVE / SHIP compensators to
the commit-then-enqueue outbox pattern. APPROVE's *forward* step
stayed inline against `TransactionAgent.submit_to_supplier` because
the saga ledger needs the supplier-assigned `reshare_id` stamped onto
its forward-event payload — downstream SHIP and RETURN read it back
via `_derive_extras` (`api/app.py:115-155`).

Concretely, today's `approve_forward` (`saga/flows.py:100-124`):

```python
result = await tx.submit_to_supplier(idempotency_key=..., ...)
return StepResult(
    state_after=LifecycleState.APPROVED,
    payload={"reshare_id": result.reshare_id, ...},
    ...
)
```

This is the only step that synchronously calls the supplier inside
the ledger transaction. Every other ReShare-touching forward returns
an `OutboxIntent` and lets `OutboxWorker` drive the wire.

The split has three consequences worth fixing:

1. **Inconsistency.** Future contributors writing new flows ask "do I
   call inline or enqueue?" The inline-only-for-APPROVE rule is a
   special case driven by data flow, not by design intent.
2. **Latency on the approve hot path.** Staff click → ledger tx
   blocks on supplier round-trip. A slow supplier directly slows the
   staff console; the rest of the lifecycle is non-blocking.
3. **Compensator asymmetry.** APPROVE's *compensator* is already
   migrated (it enqueues `cancel_request`); the forward isn't. Easy
   to misread the pattern when extending.

ADR-0011 explicitly named two future-work options for closing this
gap:

- (A) Introduce `LifecycleState.APPROVING` so the saga ledger
  reflects "intent committed, supplier not yet confirmed".
- (B) Have `OutboxWorker` write back an `observation` event carrying
  ReShare's response, and teach `_derive_extras` /
  `find_committed_forward` to read it.

This ADR picks one and defines the migration steps.

## Decision

**Adopt option (A) — introduce `LifecycleState.APPROVING` as an
explicit intermediate state.** APPROVE forward becomes pure: it
returns an `OutboxIntent` and advances the saga to `APPROVING`. The
worker drains the row, calls `submit_to_supplier`, and on success
appends an `OBSERVATION` event carrying `reshare_id` *and* a
follow-up forward-style transition to `APPROVED`.

### Why option A over option B

Option B (observation-event projection) is closer to the existing
mechanics — no new lifecycle state, no Alembic migration of an enum
domain. It loses on three counts:

| Criterion | Option A: APPROVING state | Option B: observation projection |
|---|---|---|
| Saga `current_state` semantics | Honest — "we asked, no answer yet" | Lying — saga shows `APPROVED` even when the wire call hasn't fired or has dead-lettered |
| Staff console UX | New badge ("awaiting supplier ack") trivially exposed | Staff must read the outbox table separately to see if the wire call landed |
| Compensator logic | `find_committed_forward(APPROVE)` keeps its current contract | `find_committed_forward` must learn to consult observations too |
| Replay safety | Same mechanism as today (UNIQUE on idempotency_key) | Adds a second source of truth (observation events) that must be read consistently |
| ISO 18626 alignment | Maps cleanly to `Requested` (intent sent, no `WillSupply` yet) | Conflates `Requested`, `WillSupply`, and `ExpectToSupply` |
| Rollback if migration is wrong | Drop the new state, revert flow | Strip projection logic from `_derive_extras`, hope no compensator already relied on it |

Option B's only real edge is "no DB migration" — a rounding error
relative to Alembic friction. Option A is the conservative pick.

### What changes

#### 1. `models/lifecycle.py`

Add the state. `APPROVING` is **not** terminal.

```python
class LifecycleState(str, Enum):
    SUBMITTED = "submitted"
    ROUTED = "routed"
    APPROVING = "approving"   # NEW
    APPROVED = "approved"
    SHIPPED = "shipped"
    RETURNED = "returned"
    CANCELLED = "cancelled"
    UNFILLED = "unfilled"
    DISPUTED = "disputed"
```

ISO 18626 mapping: `APPROVING ↔ Requested`. PRD-01 update needed.

#### 2. `saga/flows.py` — `approve_forward` becomes pure

```python
async def approve_forward(ctx: SagaContext) -> StepResult:
    supplier = ctx.extras.get("chosen_supplier")
    if not supplier:
        raise ValueError("ctx.extras['chosen_supplier'] is required")
    return StepResult(
        state_after=LifecycleState.APPROVING,
        payload={"supplier_symbol": supplier},
        rationale=(
            f"Saga moved to Approving; submit-to-supplier enqueued "
            f"for asynchronous delivery via outbox worker (supplier={supplier})."
        ),
        outbox=[
            OutboxIntent(
                target="reshare",
                idempotency_key=ctx.idempotency_key,
                payload={
                    "action": "send_request",
                    "args": {
                        "request_payload": {  # same shape as today
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
```

Note the forward payload no longer contains `reshare_id` — the
supplier hasn't given us one yet. The downstream observation event
carries it.

#### 3. `saga/outbox.py` — handler writes back

`make_reshare_handler` must learn to project the
`SubmitResult` (which carries `reshare_id`, `supplier_symbol`,
`state`, `iso_message_id`) onto the saga ledger after a successful
`send_request` call. Two options:

- **3a (preferred)** — extend the handler signature to accept an
  optional `on_success: Callable[[dict, str, Any], Awaitable[None]]`
  and have the lifespan wire one that uses a fresh `Coordinator` to
  append an observation event + advance state to `APPROVED`.
- 3b — give the worker first-class knowledge of "post-success
  projection" via a new `OutboxOutcome` callback.

3a keeps the worker target-agnostic. The projection is wired
target-by-target at lifespan time, mirroring how `make_reshare_handler`
already lives next to its target.

The projection writes:

```python
NewSagaEvent(
    saga_id=...,
    kind=EventKind.OBSERVATION,
    step=StepName.APPROVE,
    state_before=LifecycleState.APPROVING,
    state_after=LifecycleState.APPROVED,
    actor="agent:outbox-worker",
    idempotency_key=f"approve-ack-{outbox_row_id}",
    payload={
        "reshare_id": result.reshare_id,
        "supplier_symbol": result.supplier_symbol,
        "iso_state": result.state,
    },
    outcome=StepOutcome.COMMITTED,
    rationale="Supplier acknowledged via ReShare; saga advanced to Approved.",
)
```

The deterministic `approve-ack-{row_id}` key keeps the projection
replay-safe even if the worker crashes between the supplier call and
the ledger write.

#### 4. `api/app.py` — `_derive_extras` reads observations

`reshare_id` now lives on the OBSERVATION event, not the FORWARD.
Extend the `EventKind.FORWARD` branch to also accept
`EventKind.OBSERVATION` for `step == APPROVE`:

```python
if ev.kind == EventKind.FORWARD or (
    ev.kind == EventKind.OBSERVATION and ev.step == StepName.APPROVE
):
    if payload.get("reshare_id"):
        extras["reshare_id"] = payload["reshare_id"]
    ...
```

The compensator branch keeps its existing semantics
(`pop("reshare_id", None)` on the APPROVE compensator); the
observation event is bookkeeping, not a logical step the
compensator reverses.

#### 5. SHIP gate logic

Today the API treats SHIP as approvable from `APPROVED`. After this
change, SHIP must wait for `APPROVED` — which now requires the
supplier ack to land. Two practical consequences:

- Staff console must surface `APPROVING` saga state and disable the
  "Approve for shipping" button until the ack lands.
- The happy-path demo (`demos/happy_path.py`) needs to drain the
  outbox between `/sagas/{id}/approve` and `/sagas/{id}/approve` for
  SHIP. Either explicit `await worker.drain_until_empty()` between
  steps, or `await asyncio.sleep(...)` if the lifespan worker is
  running.

#### 6. Alembic migration

A new revision for `LifecycleState.APPROVING`. Storage is `VARCHAR`
already (no enum domain), so the migration is a no-op at the DB level
— but we ship the revision anyway so:

- The schema-version column reflects the lifecycle change.
- Downstream consumers reading the value get a clear marker for
  when the new state appeared.

#### 7. `_APPROVABLE_STEPS` stays unchanged

`StepName.APPROVE` is already approvable. The state-after of its
forward becomes `APPROVING` instead of `APPROVED`, but the gate
endpoint logic doesn't care. The post-ack transition to `APPROVED` is
the worker's projection, not a gated step.

### Out of scope

- Migrating SHIP and RETURN forwards similarly. They don't return
  data the ledger needs downstream, so the existing pattern is fine.
- Multi-worker safety. `SELECT ... FOR UPDATE SKIP LOCKED` remains
  flagged in CLAUDE.md — orthogonal to this ADR.
- Real Okapi token auth for `HttpReShareClient`. Same.

## Consequences

**Positive**
- Every forward step is now pure with respect to external systems.
- `saga.current_state` honestly represents "we asked but haven't
  heard back" via the new `APPROVING` state — meaningful in a system
  whose entire correctness story is the human-in-loop ledger.
- Staff console can show "awaiting supplier ack" without inspecting
  outbox rows.
- APPROVE forward latency drops to a DB write; the supplier
  round-trip is moved off the staff hot path.
- New flows have one rule: return an `OutboxIntent` for any external
  call. No more "is APPROVE special?" lookup.

**Negative**
- One more `LifecycleState`. UI / demo / docs all touch it.
- Worker now has a second responsibility (call + project) for the
  reshare target. The `on_success` plumbing is small but not zero.
- Tests that previously asserted on `MockReShareClient` recorded
  calls inside the synchronous APPROVE path must now drain the
  outbox first, like SHIP and RETURN tests already do.
- If the worker dead-letters the `send_request` enqueue, the saga
  sits in `APPROVING` until staff intervene. This is the **desired**
  behaviour — it's the same posture as a recall dead-letter today —
  but it moves the failure mode from "approve endpoint 5xx" (staff
  retries) to "saga stuck in APPROVING" (staff inspects outbox).
  The staff console must surface dead-letter rows to make this
  recoverable.

## Migration plan (implementation steps)

1. Add `LifecycleState.APPROVING`, update PRD-01 mapping table.
2. Alembic revision (no-op DDL, just a marker).
3. Extend `make_reshare_handler` with `on_success` (or land 3b).
4. Rewrite `approve_forward` to return an `OutboxIntent`.
5. Teach `_derive_extras` to read OBSERVATION events for APPROVE.
6. Update `_APPROVABLE_STEPS` doc-only — set unchanged.
7. Update `demos/happy_path.py` to drain the outbox between approve
   and ship.
8. Update tests:
   - `test_coordinator.py` — APPROVE forward now ends in `APPROVING`.
   - `test_api.py` — new test for the `APPROVING → APPROVED`
     transition driven by the worker.
   - `test_outbox.py` — new test for the projection callback.
9. Remove the "APPROVE forward stays inline" entry from CLAUDE.md
   known-gaps; replace with a note about the staff-console
   `APPROVING` handling.

Each step is a separate small PR. The Alembic revision can land first
(harmless if the lifecycle state is unused). The flow rewrite +
projection should land together.

## Alternatives reconsidered

| Alternative | Reason rejected |
|---|---|
| Option B — observation-event projection only | Saga `current_state` lies during the in-flight window; staff console UX worse; replay logic gets a second source of truth |
| Status quo (APPROVE inline forever) | Violates "every forward is pure" rule from ADR-0011; latency on the staff hot path; pattern asymmetry confuses new contributors |
| Two-phase commit between DB and ReShare | mod-rs has no XA support — same reason ADR-0011 rejected it |
| Synchronous polling loop after enqueue | Defeats the point of moving the call off the request path |

## Open questions

- Should the `APPROVING → APPROVED` projection be its own
  `EventKind` ("PROJECTION"?) or piggyback on `OBSERVATION`? This ADR
  picks `OBSERVATION` for minimal-change. A future refactor can
  separate them if more steps adopt the same pattern.
- Worker projection-write failure: if the supplier call succeeds but
  the projection write fails (DB hiccup), the row is marked
  delivered but the saga stays in `APPROVING`. Mitigation: keep the
  `mark_delivered` write inside the same session as the projection
  write so the two commit atomically. Implementation detail for the
  flow PR; flagged here for visibility.
