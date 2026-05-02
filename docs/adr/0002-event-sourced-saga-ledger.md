# ADR 0002 — Event-sourced saga ledger as system-of-record

**Status:** Accepted
**Date:** 2026-05-02

## Context

ILL is a long-running, multi-party, partial-failure-prone workflow.
The user explicitly required:
- compensating transactions for rollback
- idempotency everywhere

Both are classic saga-pattern requirements. We need a durable record
that survives process restarts, supports replay, and is auditable for
regulators and staff disputes.

## Decision

Use an **append-only, event-sourced ledger in Postgres** as the
authoritative system-of-record for every saga. Each forward step,
compensator, gate (human approval), and observation gets one immutable
row in `saga_event`. Current state of any saga is a projection over its
events. No row is ever updated or deleted in the prototype.

Schema and replay rules are specified in `docs/prd/03-saga-and-idempotency.md`.

## Consequences

**Positive**
- Crash-safe: any process restart can rebuild state by replaying events.
- Audit-ready: every transition + actor + reason persisted forever.
- Compensator correctness is enforced — compensator only runs if its
  paired forward event has `outcome=committed`.
- Idempotency keys live in the same table → unique constraint catches
  duplicate writes at the DB level.

**Negative**
- More storage than a current-state table (acceptable for prototype).
- Projecting state on every read is slower than reading a `current_state`
  column. Mitigation: optional materialized view per saga, refreshed on
  insert.
- Schema migrations on event payloads must be additive; versioning
  responsibilities are with each step's payload schema.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| Mutable `request` table with status column + audit log | Audit log can drift from state; classic source of bugs |
| Use Temporal.io workflow engine | Powerful but heavy; we'd rather understand our saga semantics first |
| Use Kafka as ledger | Operational complexity; ReShare already runs Kafka internally — keep our own state in Postgres for clarity |
| AWS Step Functions / GCP Workflows | Cloud lock-in; not appropriate for local-only prototype |

## Notes for production migration

- Add a `current_state` materialized view for hot-path reads.
- Add partitioning on `saga_event` by month after volume scales.
- Replace synchronous outbox worker with a CDC pipeline (Debezium → Kafka).
- Consider event schema registry for payload versioning.
