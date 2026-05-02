# ADR 0010 — Build a saga coordinator, not adopt a workflow engine

**Status:** Accepted
**Date:** 2026-05-02

## Context

Saga semantics could be implemented by adopting a workflow engine
(Temporal, Cadence, AWS Step Functions, GCP Workflows, Airflow) or by
building a small custom coordinator over our event-sourced ledger.

For a research prototype where understanding the semantics is part of
the deliverable, dependency weight matters.

## Decision

Build a **small, explicit saga coordinator** in pure Python (~500 LoC
target) that reads/writes our `saga_event` ledger directly. No
external workflow engine.

The coordinator:
- Decides what step to schedule next based on current saga state
- Calls the appropriate agent / client function with an idempotency key
- Records the result in the ledger
- Surfaces gates (human approvals) by writing `kind=gate, outcome=pending`
  rows
- On startup, scans for stalled sagas (pending forward steps older than
  threshold) and either resumes or marks failed

## Consequences

**Positive**
- Code is explicit and auditable; no hidden engine semantics.
- Easy to test with property-based tests.
- Zero new infrastructure dependencies.
- The semantics we care about (compensator pairing, idempotency) live
  in code we read.

**Negative**
- We rebuild some primitives that engines provide (timers, retries,
  signals).
- Doesn't scale beyond single-instance prototype without more work.
  Mitigation: documented; lift to Temporal in production migration.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| Temporal.io | Powerful and well-suited but adds ops weight; we'd rather understand the primitives first |
| AWS Step Functions / GCP Workflows | Cloud lock-in; not appropriate for local-only prototype |
| Airflow | DAG-oriented; saga's compensator pattern is awkward to express |
| Reuse ReShare's internal state machine | Doesn't include our policy/discovery/approval gates |

## Migration path

Document the coordinator's contract precisely. If we later move to
Temporal, each saga step maps onto a Temporal Activity, the saga
itself onto a Workflow, gates onto signals, ledger onto Workflow
history.
