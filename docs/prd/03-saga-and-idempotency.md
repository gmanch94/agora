# PRD 03 — Saga & Idempotency

> Last reviewed against code: 2026-05-04 (post PRs #89/#90 — NCIP item-barcode
> + override endpoint + StepName.RESOLVE).

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
    step            VARCHAR(32) NOT NULL,          -- forwards: 'submit'|'route'|'approve'|'ship'|'receive'|'return'  // compensators / branches: 'cancel'|'reroute'|'revoke'|'recall'|'dispute'
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

Every wire-touching saga step returns one or more `OutboxIntent`
rows on its `StepResult`. The coordinator writes the `saga_event`
row **and** the outbox rows in the same DB transaction (see
ADR-0011, extended by ADR-0012 to cover APPROVE — see § APPROVE
through outbox below). The outbox worker drains them onto the wire
asynchronously.

```sql
CREATE TABLE outbox (
    id              BIGINT PRIMARY KEY,           -- _bigint_pk()
    saga_id         UUID NOT NULL,
    target          VARCHAR(64) NOT NULL,         -- 'reshare' | 'ncip'
    idempotency_key VARCHAR(64) NOT NULL UNIQUE,
    payload         JSONB NOT NULL,
    status          VARCHAR(16) NOT NULL,         -- 'pending' | 'in_flight' | 'delivered' | 'dead_letter'
    attempts        INT NOT NULL DEFAULT 0,
    last_error      VARCHAR(2048),
    scheduled_for   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    claimed_at      TIMESTAMPTZ                   -- multi-worker lease (PR #25)
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
`dead_letter` for staff triage. **Multi-worker safe on Postgres
via `outbox_claim`** (PR #25): `SELECT ... FOR UPDATE SKIP LOCKED`
acquires disjoint row sets, flips claimed rows to
`status='in_flight'` with `claimed_at=now()`, and commits.
Orphan recovery sweeps `in_flight` rows whose `claimed_at` is older
than `claim_lease_secs` (default 600s) back to `pending`. The
`with_for_update` hint is only emitted on Postgres; SQLite
serializes writers naturally. Verified by
`tests/test_outbox_concurrent_postgres.py`.

### APPROVE through the outbox (via `APPROVING`)

ADR-0012 closed the APPROVE-inline gap (PR #17). APPROVE forward is
now pure: it returns an `OutboxIntent` for `send_request` and
advances the saga to `LifecycleState.APPROVING`. The outbox worker
drains the row, calls the supplier, and the projection callback
(`make_reshare_on_success`) writes an OBSERVATION carrying
`reshare_id` that advances the saga to `APPROVED`. Downstream
SHIP/RETURN consume `reshare_id` via `_derive_extras` in
`api/app.py`, which now reads APPROVE OBSERVATION events as well
as FORWARD events. The projection runs **inside the same session**
as `outbox_mark_delivered`, so the OBSERVATION write and the
delivered flag commit atomically; a failed projection re-queues the
row for retry without leaving the saga half-advanced. Hitting
`/compensate` during the APPROVING window (supplier ack still
pending) returns 400 — there is no `reshare_id` to cancel against.

### RECEIVE + RETURN through the outbox (NCIP fan-out)

`SHIPPED → RECEIVED → RETURNED` are the borrower-side leg. Both
forwards emit two outbox intents in one step (one per target):

| Forward  | `target='reshare'` intent              | `target='ncip'` intent                          |
| -------- | -------------------------------------- | ----------------------------------------------- |
| RECEIVE  | (none — supplier-side stays `Loaned`)  | `check_out` against the borrower's local ILS    |
| RETURN   | `confirm_return` (ItemReturned)        | `check_in` against the borrower's local ILS    |

Because `outbox.idempotency_key` is `UNIQUE` across all targets, the
NCIP row carries a `:ncip` suffix on `ctx.idempotency_key` so it
collides with neither the bare-key reshare row nor a replay of the
same step. Convention: when a step emits both a reshare and an NCIP
intent the reshare row takes the bare key and the NCIP row takes
`:ncip`. RECEIVE has only one intent (the NCIP `check_out`) and
keeps the suffix for consistency.

**NCIP `check_out` is anchored on RECEIVE forward** (re-anchored
from SHIP — see `docs/lessons.md` § Saga / ledger; CLAUDE.md
"Known gaps" carries the canonical prose). The
patron's ILS record reflects the loan from the moment they
physically take custody, not from supplier shipment. Trade-off: a
saga whose patron never confirms receipt will never have a
`check_out` dispatched; the TrackingAgent tier-3 watch
(`receipt-unconfirmed-{saga_id}`) surfaces this to staff. `due_at`
still anchors to `shipped_at` because the loan-period clock is a
supplier-side commitment that starts at shipment, and an
unconfirmed-receipt saga still needs an overdue threshold.

**NCIP outcomes do not gate saga state.** Failure surfaces as a
stuck outbox row for staff review; the saga continues. The NCIP
HTTP/SOAP client ships as `HttpNcipClient` (PR #98, wired PR #99;
source-review-only against mod-ncip master; live mod-ncip probe
still pending — see NEXT_SESSION.md § Backlog). `MockNcipClient`
remains the default for prototype / tests.

### SHIP compensator (post-RECEIVE re-anchor)

The SHIP compensator emits a single ReShare `recall_request` in
either branch (saga at `SHIPPED` or post-`RECEIVED`) and lands in
`DISPUTED`. The `current_state` check survives only as state-aware
rationale text; functionally both branches enqueue the same recall.

- At `SHIPPED` no ILS loan was ever opened (RECEIVE forward never
  ran) — recall is the only correct action.
- At `RECEIVED` the patron physically holds the book so the loan
  correctly reflects current custody; the eventual return flow owns
  `check_in`.

The earlier state-aware NCIP rollback (PR #37, idempotency-key
suffix `:ncip-rollback`) compensated for an upstream design tension
that the re-anchor removed. Logged in `docs/lessons.md` § Saga /
ledger as a generalised lesson: re-anchoring a side effect can
obsolete prior state-aware logic.

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
