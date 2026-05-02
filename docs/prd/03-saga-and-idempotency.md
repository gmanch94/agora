# PRD 03 — Saga & Idempotency

## Saga model

Every ILL request is a **saga**: a sequence of forward steps with
paired compensators, persisted in an event-sourced ledger.

### Ledger schema (Postgres)

```sql
CREATE TABLE saga_event (
    id              BIGSERIAL PRIMARY KEY,
    saga_id         UUID NOT NULL,
    seq             INT NOT NULL,
    kind            TEXT NOT NULL,  -- 'forward' | 'compensator' | 'gate' | 'observation'
    step            TEXT NOT NULL,  -- 'submit'|'route'|'approve'|'ship'|'return'|...
    state_before    TEXT NOT NULL,
    state_after     TEXT NOT NULL,
    actor           TEXT NOT NULL,  -- 'agent:routing' | 'staff:user@org' | 'system'
    idempotency_key TEXT NOT NULL,
    iso_message_id  TEXT,           -- ISO 18626 messageId, if any
    payload         JSONB NOT NULL,
    outcome         TEXT NOT NULL,  -- 'committed' | 'failed' | 'pending'
    rationale       TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (saga_id, seq),
    UNIQUE (idempotency_key)
);

CREATE INDEX idx_saga_event_saga ON saga_event(saga_id, seq);
```

Append-only. The current state of a saga = projection over its events.

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

- Re-running a forward step with the same `idempotency_key`: return the
  prior committed result; do not re-execute.
- Compensators idempotent the same way.
- A saga can be reconstructed from `saga_event` alone; no other
  authoritative state exists in the prototype.

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

### Outbox pattern (outbound)

Every outbound state change writes to outbox + business state in the
same DB transaction. A worker reads outbox and emits to external
system. Retries safe because external side dedups on
`idempotency_key`.

```sql
CREATE TABLE outbox (
    id              BIGSERIAL PRIMARY KEY,
    saga_id         UUID NOT NULL,
    target          TEXT NOT NULL,  -- 'reshare' | 'ncip' | ...
    idempotency_key TEXT NOT NULL UNIQUE,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    delivered_at    TIMESTAMPTZ
);
```

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
