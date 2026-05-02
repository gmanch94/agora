---
name: lifecycle-extend
description: Add a new state or step to the Agora ILL lifecycle without breaking saga invariants. Use when the user asks to add a state (e.g. "Held", "Renewed", "Lost"), add a forward+compensator pair, or extend the state machine. Walks through every file that must change in lockstep and writes the skeletons.
---

# lifecycle-extend

Adding a lifecycle state in Agora touches **6 files in lockstep**. Miss
one and you get a saga that the coordinator can't drive, a PRD that
lies, or tests that still pass for the wrong reason. This skill is the
checklist + scaffolder.

## When to invoke

- User asks to add a new lifecycle state, e.g. "add a Held state
  between Routed and Approved"
- User asks to add a new step to the state machine
- User asks to add a forward+compensator pair for a transition
- User mentions extending ISO 18626 coverage (e.g. ExpectToSupply
  handling)

## Required information from the user (ask if not given)

1. **State name** — short, lowercase, snake_case (e.g. `held`,
   `renewed`, `lost`).
2. **Step name** — verb-form (e.g. `hold`, `renew`, `mark_lost`).
3. **Where it sits in the lifecycle** — predecessor + successor states
   (e.g. "between Routed and Approved" → `predecessor=routed`,
   `successor=approved`).
4. **Forward semantics** — what call to ReShare (or what side-effect)
   the forward step performs.
5. **Compensator semantics** — what undoes it (recall? cancel? mark
   failed-terminal?). If physical reality makes it un-undoable, the
   compensator should record an observation and let staff resolve.
6. **ISO 18626 mapping** — which ISO message this represents
   (RequestingAgencyMessage / SupplyingAgencyMessage / state name).
   If unknown, flag and check `docs/prd/01-lifecycle-and-states.md`.
7. **Human gate?** — yes by default (see ADR-0005). Confirm.

## Files to update (in this order)

### 1. `src/agora/models/lifecycle.py`

- Add the new value to `LifecycleState` enum.
- Add the new value to `StepName` enum.
- If the state is terminal, add it to `TERMINAL_STATES`.
- If a state ordering / valid-transition map exists, update it.

### 2. `src/agora/saga/flows.py`

- Write the forward step function (signature: `async def
  forward_<step>(ctx: SagaContext, deps) -> NewSagaEvent`).
- Write the compensator function (signature: `async def
  compensate_<step>(ctx: SagaContext, deps) -> NewSagaEvent`).
- Register the pair in `build_registry(...)` keyed by `StepName.<X>`.
- The forward returns a `NewSagaEvent` with `kind=FORWARD`,
  `step=StepName.<X>`, correct `state_before` / `state_after`,
  `actor`, `idempotency_key=ctx.idempotency_key`, and a `payload` dict
  capturing any IDs the compensator will need.
- The compensator reads those IDs from `ledger.find_committed_forward(
  saga_id, StepName.<X>)`.

### 3. `alembic/versions/<new>.py`

- If the new state changes any DB schema (rare — usually it doesn't,
  since states are strings), generate a new revision.
- If no schema change: skip this file but explicitly tell the user
  "no migration needed".

### 4. `docs/prd/01-lifecycle-and-states.md`

- Add a row to the lifecycle ↔ ISO 18626 mapping table:
  | User lifecycle | ISO 18626 state | Human gate | Saga compensator |
- Update the state-diagram block so the new state appears with its
  in/out arrows.

### 5. `tests/test_coordinator.py`

- Add a happy-path test that drives the saga through the new step
  with a committed gate and asserts the `state_after`.
- Add a gate-required test that calls `run_forward` for the new step
  WITHOUT a committed gate and asserts `GateRequiredError`.
- Add a compensator test that runs the forward, then the
  compensator, and asserts the ledger reflects both events plus the
  state-revert.

### 6. `src/agora/demos/happy_path.py`

- Add the new `StepName.<X>` to the demo's lifecycle loop so the
  end-to-end script exercises it.

### 7. `docs/architecture.md`

- Update the `stateDiagram-v2` block to include the new state +
  transitions.

## Invariants to preserve (HARD RULES — do not break)

- Forward step MUST require a committed gate event. The coordinator
  enforces this — your forward function should not bypass it.
- The compensator runs only against a committed forward step. Look it
  up via `SagaLedger.find_committed_forward()`.
- Idempotency keys are ULIDs with a step-name prefix:
  `new_idempotency_key(prefix=step.value)`.
- All datetimes use `datetime.now(UTC)` — never bare `datetime.now()`.
- The forward writes ONE event row. The compensator writes ONE event
  row. Multi-row writes inside a single step are a smell.
- If the new step calls ReShare, route through `TransactionAgent` so
  the ReShare client interface stays the seam.

## Output of the skill

After gathering requirements, produce:

1. A 1-paragraph summary of the change in plain English.
2. A unified-diff-style preview of every file edit.
3. A go/no-go question to the user.
4. On approval: apply edits, run `pytest tests/ -q` and `ruff check
   src tests`, report results.

## Don'ts

- Don't add a state without a human gate unless the user explicitly
  cites ADR-0005 and gives a reason — this is a load-bearing safety
  invariant.
- Don't combine "add new state" with unrelated refactors. Keep the
  blast radius small.
- Don't update only some of the 6+ files — the inconsistency will
  bite later. If a file truly doesn't need a change, say so out loud.
- Don't invent ISO 18626 message names. If the mapping is unclear,
  ask the user or flag for an ADR.
