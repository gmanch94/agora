# ADR 0011 — Forward steps commit ledger then enqueue outbox

**Status:** Accepted
**Date:** 2026-05-02

## Context

Saga forward + compensator steps in `saga/flows.py` originally called
the `TransactionAgent` (and through it the `ReShareClient`) inline. The
ledger event and the wire-level ReShare call therefore happened in
two unrelated transactions: a 5xx from ReShare or a process crash
between the two left the saga ledger in a state that disagreed with
the supplier's view of the world.

The outbox table + `OutboxWorker` (`saga/outbox.py`) were stood up to
fix exactly this — commit the *intent* to call ReShare in the same DB
transaction as the ledger event, and let a separate worker drain the
outbox onto the wire. Until now the worker existed but no flow used
it; ADR-0010 deferred the cutover to its own change.

This ADR is that change.

## Decision

Forward and compensator step functions become **pure** with respect
to external systems. They do not call ReShare directly; instead they
return an `OutboxIntent` on the `StepResult` and the coordinator
enqueues it in the same transaction that appends the ledger event.

### Mechanism

1. `StepResult` grows an `outbox: list[OutboxIntent]` field
   (default empty).
2. `OutboxIntent` carries `(target, idempotency_key, payload)`.
   The convention for `target="reshare"` payload is the same one
   `make_reshare_handler` already expects:
   `{"action": "<method-name>", "args": {...kwargs...}}`.
3. `Coordinator.run_forward` and `Coordinator.run_compensator`, after
   `ledger.append` returns a *non-None* row (i.e. it was a fresh
   write, not a replayed key), iterate the result's `outbox` and call
   `outbox_enqueue(self._session, ...)`. Same session → same outer
   transaction → atomic with the ledger event.
4. Replay safety: when `ledger.append` returns `None` (idempotency-key
   collision; the prior row stands), the coordinator **skips
   enqueue**. The outbox row was already written on the original
   pass.

### What migrates now vs later

| Step                  | Migration |
|-----------------------|-----------|
| `submit` forward      | n/a — already pure (no external call) |
| `submit` compensator  | n/a |
| `route` forward       | n/a |
| `route` compensator   | n/a |
| `approve` forward     | **stays inline** (transitional) — needs `reshare_id` back from ReShare to put into the forward-event payload, which downstream `ship`/`return` derive from |
| `approve` compensator | **migrated** — fire-and-forget cancel at supplier |
| `ship` forward        | **migrated** — supplier mark-shipped, no result needed downstream |
| `ship` compensator    | **migrated** — recall (HTTP client raises today; the worker will dead-letter, which is the correct signal for staff) |
| `return` forward      | **migrated** — borrower returned, fire-and-forget |
| `return` compensator  | n/a (no external call) |

### Why APPROVE stays inline

The cleanest end-state is for *every* forward step to be pure — the
worker calls ReShare, then writes back the result (e.g. `reshare_id`)
as an `observation` event the ledger learns to project into
`saga.current_state` / derived extras. That requires either:

- An intermediate `LifecycleState.APPROVING` (saga sits there until the
  worker confirms), or
- `_derive_extras` learning to read `observation` events for
  `reshare_id`.

Both are doable but neither is in scope for this ADR. The transitional
fix is that **approve_forward keeps calling
`tx.submit_to_supplier` inline so it can stamp `reshare_id` onto the
forward event** — exactly as it does today. The other four migrated
steps don't need anything back from ReShare so they can run async.

### Optimistic state advance

After this change, `saga.current_state` advances to e.g. `SHIPPED` the
moment the gate-commit + ledger append + outbox enqueue all commit —
*before* the worker has actually told ReShare. If the worker fails
permanently (dead-letter), the saga is still `SHIPPED` in our ledger
but the supplier never heard "shipped". Resolution:

- This is a **feature** for the human-in-loop default (ADR-0005):
  the *staff member's* approval is what advances the saga; the wire
  call is plumbing.
- Dead-letter rows surface to the staff console for manual
  reconciliation. (UI is future work; the row is queryable today.)
- Compensators can still run; they have no dependency on the paired
  outbox row having drained.

## Consequences

**Positive**
- Atomic ledger + intent-to-call-supplier; no two-transaction split.
- Step functions become trivially testable — no client mocks needed
  for the four migrated steps; tests assert on the outbox row
  directly.
- Retries + dead-lettering already exist in `OutboxWorker`. We get
  exponential backoff for free.
- Worker is target-agnostic; adding NCIP later is "register a second
  handler", not "rewrite flows".

**Negative**
- Tests / demos that asserted on `MockReShareClient` recorded calls
  must explicitly drain the outbox after running the step. The
  in-memory mock does not see traffic until the worker runs.
- `saga.current_state` is now optimistic w.r.t. the wire — see above.
- Single-drainer constraint still applies (CLAUDE.md known gaps).
  Multi-worker safety needs `SELECT ... FOR UPDATE SKIP LOCKED`,
  which is Postgres-only.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-----------------|
| Two-phase commit between DB and ReShare | mod-rs has no XA support; this is the conventional outbox pattern for exactly this reason |
| Synchronous call inside ledger transaction | What we had — leaves ledger and supplier disagreeing on partial failure |
| Enqueue *after* outer transaction commits | Loses atomicity; if process dies between commit and enqueue the wire call is silently dropped |
| Migrate APPROVE too via `APPROVING` intermediate state | Worth doing later; out of scope here |

## Migration path forward (future ADR territory)

1. Introduce `LifecycleState.APPROVING` (and `SHIPPING`/`RETURNING`
   if desired) so the ledger reflects "intent committed, wire call
   not yet confirmed".
2. Have `OutboxWorker`'s success path append an `observation` event
   carrying ReShare's response. Teach `_derive_extras` and
   `find_committed_forward` to read it.
3. With those in place, migrate `approve_forward` so every forward
   step is pure.
