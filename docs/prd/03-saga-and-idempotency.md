# PRD 03 — Saga & Idempotency

> Last reviewed against code: 2026-05-02.

## Saga model

Every ILL request is a **saga**: a sequence of forward steps with
paired compensators, persisted in an event-sourced ledger.

### Ledger schema

Authoritative DDL lives in `src/agora/saga/db.py` and the Alembic
migration in `alembic/versions/`. Conceptual shape (Postgres-flavoured;
SQLite tests use portable types via `_PortableUUID`, `_bigint_pk`,
`_json_type` helpers):

```sql
CREATE TABLE saga_event (
    id              BIGINT PRIMARY KEY,            -- _bigint_pk(): BIGINT on PG, INTEGER on SQLite
    saga_id         UUID NOT NULL REFERENCES saga(id) ON DELETE CASCADE,
    seq             INT  NOT NULL,
    kind            VARCHAR(32) NOT NULL,          -- 'forward' | 'compensator' | 'gate' | 'observation'
    step            VARCHAR(32) NOT NULL,          -- 'submit'|'route'|'approve'|'ship'|'return'|'cancel'|'reroute'|'revoke'|'recall'|'dispute'
    state_before    VARCHAR(32) NOT NULL,
    state_after     VARCHAR(32) NOT NULL,
    actor           VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(64)  NOT NULL,
    iso_message_id  VARCHAR(128),
    payload         JSONB NOT NULL,
    outcome         VARCHAR(16) NOT NULL,          -- 'pending' | 'committed' | 'failed' | 'skipped'
    rationale       VARCHAR(2048),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_saga_event_seq  UNIQUE (saga_id, seq),
    CONSTRAINT uq_saga_event_idem UNIQUE (idempotency_key)
);

CREATE INDEX ix_saga_event_saga ON saga_event(saga_id, seq);
```

Append-only. The current state of a saga = projection over its
events. The lightweight `saga` row carries `current_state` as a
denormalised projection for cheap UI reads — the ledger is the
source of truth.

### Forward + compensator pairs

Each lifecycle step is a `(forward_fn, compensator_fn)` pair. Both
take the saga context and idempotency key. Compensator is invoked
only if the forward step has `outcome='committed'` and
reconciliation is triggered.

Pseudocode:

```python
@step
def approve(ctx, idem):
    # forward
    msg = reshare.send_request(ctx.request, idempotency_key=idem)
    return {"iso_message_id": msg.id, "supplier": ctx.supplier}

@approve.compensator
def revoke(ctx, idem, fwd_payload):
    return reshare.cancel_request(
        fwd_payload["iso_message_id"], idempotency_key=idem
    )
```

### Replay rules

- Re-running a forward step with the same `idempotency_key` returns
  the prior committed row; the duplicate insert hits
  `UNIQUE(idempotency_key)` inside a `begin_nested()` savepoint and
  the savepoint rolls back without affecting the caller's outer
  transaction.
- Compensators are idempotent the same way.
- A saga can be reconstructed from `saga_event` alone.

### Append discipline (`SagaLedger.append`)

1. Look up the parent saga; refuse the write if the saga is in a
   terminal state and the event is anything other than a benign
   OBSERVATION.
2. Compute next `seq` (max + 1).
3. Insert inside a savepoint. On `IntegrityError`, look up by
   `idempotency_key`: if found, return the existing row (replay);
   otherwise re-raise (genuine concurrent-writer collision on
   `(saga_id, seq)`).
4. On a fresh COMMITTED forward, promote `saga.current_state`.

## Idempotency

### Key generation

- **ULIDs** for new keys (sortable, unique, 128-bit). Generated at the
  saga coordinator before any side-effecting call.
- Idempotency key threads from coordinator → agent → external client →
  external system.

### Inbox pattern (inbound)

Every inbound message (ISO 18626 from peer, NCIP from ILS, ReShare
webhook) goes through:

```
1. Receive request
2. Extract message_id (or hash body if absent)
3. Look up in inbox table; if seen → return prior response
4. Else: process, write outcome, then return
```

Inbox table:

```sql
CREATE TABLE inbox (
    message_id  TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    response    JSONB
);
```

### Outbox pattern (outbound) — commit-then-enqueue

Every wire-touching saga step (except APPROVE forward — see § APPROVE
exception below) returns one or more `OutboxIntent` rows on its
`StepResult`. The coordinator writes the `saga_event` row **and**
the outbox rows in the same DB transaction (see ADR-0011). The
outbox worker drains them onto the wire asynchronously.

```sql
CREATE TABLE outbox (
    id              BIGINT PRIMARY KEY,           -- _bigint_pk()
    saga_id         UUID NOT NULL,
    target          VARCHAR(64) NOT NULL,         -- 'reshare' | (future) 'ncip'
    idempotency_key VARCHAR(64) NOT NULL UNIQUE,
    payload         JSONB NOT NULL,
    status          VARCHAR(16) NOT NULL,         -- 'pending' | 'delivered' | 'dead_letter'
    attempts        INT NOT NULL DEFAULT 0,
    last_error      VARCHAR(2048),
    scheduled_for   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ
);
```

**Replay-safety lives in two UNIQUE constraints** —
`saga_event.idempotency_key` and `outbox.idempotency_key`. mod-rs
itself ignores `Idempotency-Key` headers; we cannot rely on the
external side for dedup. (The `HttpReShareClient` still passes the
header for handlers that do honour it.)

**Worker.** `OutboxWorker.run_forever` (`src/agora/saga/outbox.py`)
is spawned as an `asyncio.Task` from the FastAPI lifespan, polling
at `AGORA_OUTBOX_POLL_INTERVAL_SECS` (default 1.0s) and cancelled
on shutdown. Disable with `AGORA_OUTBOX_WORKER_ENABLED=0`. Backoff
is exponential (`base_backoff_secs=60`, `2**attempts`) and rows that
hit `OUTBOX_RETRY_MAX_ATTEMPTS` (default 10) are flipped to
`dead_letter` for staff triage. **Single-drainer-per-DB assumption**
— no row-level lock today; multi-worker safety needs
`SELECT … FOR UPDATE SKIP LOCKED` (Postgres-only).

### APPROVE forward — the inline exception

APPROVE forward still calls `TransactionAgent.submit_to_supplier`
inline because the saga ledger needs the returned `reshare_id`
stamped onto its forward-event payload — SHIP and RETURN forwards
read it back via `_derive_extras` in `api/app.py`. Migrating APPROVE
to full outbox needs either an `APPROVING` intermediate state or a
worker-written observation event. Future ADR. Tracked in
`CLAUDE.md` known-gaps.

## Properties to verify

1. **Replay-safety**: forward step run N times with same key → 1
   committed event in ledger.
2. **Compensator-symmetry**: for any committed forward step, running its
   compensator results in a state where no externally-observable side
   effect of the forward step persists (modulo physical reality).
3. **Crash-safety**: kill the coordinator mid-step; restart →
   uncommitted forward steps either complete or are abandoned with the
   compensator running. No "ghost" external effects without a ledger
   entry.
4. **Linearizability per saga**: events in `saga_event` for one
   `saga_id` are totally ordered by `seq`.

Property tests (Hypothesis) cover these directly.
