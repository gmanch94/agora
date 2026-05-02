# Agora Runbook

Operational reference for the Agora ILL prototype. Covers bring-up,
day-to-day operation (outbox, overdue scan, gate workflow), and
incident triage. Pair with `CLAUDE.md` for invariants and `docs/adr/`
for design rationale.

> **Scope.** Research prototype, not production. No auth. No real
> peers. Postgres + Mock ReShare client by default. See
> `docs/adr/0007-fedramp-deferred.md`.

---

## 1. Bring-up

### 1.1 First-time install

```bash
# 1. Python venv (Windows path; adjust on Linux)
.venv/Scripts/python.exe -m pip install --upgrade pip wheel

# 2. Project + dev extras + sqlite driver (used by tests)
.venv/Scripts/python.exe -m pip install -e ".[dev]" aiosqlite

# 3. (Optional) Postgres sandbox
docker compose up -d postgres
```

`docker-compose.yml` ships Postgres only on port **5433** (not 5432, to
avoid colliding with a host install). FOLIO/ReShare itself is brought
up on demand from the upstream `reshare-docker` recipe — until then
all ReShare traffic uses the in-process `MockReShareClient`. See
ADR-0009.

### 1.2 Environment variables

Every setting lives in `src/agora/config.py` and reads from `.env` or
the process env. Defaults target local dev (Postgres on `localhost:5433`).

| Var                                 | Default                                                | Notes                                                      |
| ----------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------- |
| `AGORA_ENV`                         | `dev`                                                  | Free-form tag in logs.                                     |
| `AGORA_LOG_LEVEL`                   | `INFO`                                                 | Standard logging level.                                    |
| `AGORA_API_HOST` / `AGORA_API_PORT` | `0.0.0.0` / `8000`                                     | uvicorn bind.                                              |
| `AGORA_DB_URL`                      | `postgresql+asyncpg://agora:agora@localhost:5433/agora` | Tests override to `sqlite+aiosqlite:///:memory:`.          |
| `AGORA_DB_POOL_SIZE`                | `10`                                                   |                                                            |
| `RESHARE_BASE_URL`                  | `""`                                                   | Empty → mock client. Non-empty triggers real HTTP client.  |
| `RESHARE_TENANT`                    | `consortium-a`                                         | Maps to mod-rs `X-Okapi-Tenant`.                           |
| `RESHARE_USER` / `RESHARE_PASSWORD` | `""`                                                   | HTTP Basic for dev; production needs Okapi token.          |
| `NCIP_BASE_URL`                     | `""`                                                   | Mock-only today.                                           |
| `SRU_LOC_URL`                       | `https://lx2.loc.gov/voyager`                          | Library of Congress SRU.                                   |
| `SRU_TIMEOUT_SECS`                  | `5.0`                                                  |                                                            |
| `SAGA_STALL_TIMEOUT_SECS`           | `600`                                                  | Reserved for future stall detection.                       |
| `OUTBOX_RETRY_MAX_ATTEMPTS`         | `10`                                                   | Beyond this → `dead_letter`.                               |
| `AGORA_OUTBOX_WORKER_ENABLED`       | `true`                                                 | Set `0` to suppress lifespan-spawned worker (tests, etc.). |
| `AGORA_OUTBOX_POLL_INTERVAL_SECS`   | `1.0`                                                  | Worker poll interval.                                      |

`.env.example` in the repo lists the same set with safe defaults.

### 1.3 Schema

The Alembic head migration creates every table the app needs. For
fresh databases:

```bash
.venv/Scripts/python.exe -m alembic upgrade head
```

Tests bypass Alembic and call `Base.metadata.create_all()` against an
in-memory SQLite engine (see `tests/conftest.py`). The Alembic path
itself has only been exercised against SQLite; first run against real
Postgres is still pending — flagged in `CLAUDE.md`.

### 1.4 Smoke tests

Run after install or on any pull:

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy                # uses pyproject files = ["src"]
.venv/Scripts/python.exe -m agora.demos.happy_path
```

The demo runs the full lifecycle Submitted → Routed → Approved →
Shipped → Returned against in-memory SQLite + `MockReShareClient` and
prints the resulting ledger. If any step prints an error, do not
serve the API.

### 1.5 Serve the API

```bash
.venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

`create_app()`'s lifespan spawns the outbox worker as an
`asyncio.Task` and cancels it on shutdown. See § 3 below.

---

## 2. Lifecycle & gate workflow

### 2.1 States

```
Submitted → Routed → Approved → Shipped → Returned
```

Compensator targets per step are tabled in PRD
`docs/prd/01-lifecycle-and-states.md`. **Every forward step requires
a committed gate event** (`Coordinator.run_forward` raises
`GateRequiredError` otherwise). Agents are advisory — staff click
commits the gate.

### 2.2 API surface

| Endpoint                         | Effect                                                         |
| -------------------------------- | -------------------------------------------------------------- |
| `GET /health`                    | Liveness + version.                                            |
| `POST /requests`                 | Patron submit. Creates saga + first SUBMIT forward event.      |
| `GET /sagas`                     | List active + recent sagas.                                    |
| `GET /sagas/{id}`                | Full event timeline for a saga.                                |
| `POST /sagas/{id}/approve`       | Commit gate **and** run the forward step in one transaction.   |
| `POST /sagas/{id}/reject`        | Mark a pending gate `failed` (no forward runs).                |
| `POST /sagas/{id}/compensate`    | Run compensator for a previously committed forward.            |

### 2.3 What `/approve` derives vs requires

For each step the request body carries `step`, `actor`, `rationale`,
and an optional `extras` dict. The handler derives missing inputs
from the prior committed forwards:

| Step       | Required `extras` (if not derivable)               | Derived from                                  |
| ---------- | -------------------------------------------------- | --------------------------------------------- |
| `route`    | `chosen_supplier`                                  | — (first step where staff picks a supplier)   |
| `approve`  | none                                               | `chosen_supplier` from ROUTE forward          |
| `ship`     | none                                               | `reshare_id` from APPROVE forward             |
| `return`   | none                                               | `reshare_id` from APPROVE forward             |

Missing required `extras` → 400 with the missing key in `detail`.
Approving an unapprovable step (`submit`, compensator-only steps) → 400.
Unknown saga → 404.

### 2.4 Compensate semantics

`POST /sagas/{id}/compensate` looks up the most recent committed
forward for the named step (`SagaLedger.find_committed_forward`) and
runs the paired compensator. Compensating a step that never ran
returns **409** with `"no committed forward"` in `detail` (not 500 —
the ledger refuses, the API translates).

Compensators may enqueue outbox work (e.g. APPROVE compensator
enqueues `cancel_request`); see § 3.3.

---

## 3. Outbox worker

### 3.1 Where it runs

The FastAPI lifespan in `src/agora/api/app.py` spawns
`OutboxWorker.run_forever` as an `asyncio.Task` named
`agora.outbox.worker`. Cancellation is awaited cleanly on shutdown.

Disable for local debugging:

```bash
AGORA_OUTBOX_WORKER_ENABLED=0 .venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

The demo (`agora.demos.happy_path`) does **not** spawn the worker;
it calls `worker.drain_until_empty()` between lifecycle steps. That
is intentional — the demo has to be deterministic.

### 3.2 What it dispatches

Outbox rows have `(target, idempotency_key, payload, status,
attempts, scheduled_for, last_error, delivered_at)`. The worker reads
`status='pending' AND scheduled_for <= now()`, ordered by schedule
time, in batches of 50.

Today the only registered handler is `target='reshare'`, dispatched
by `make_reshare_handler(client)` to `MockReShareClient` /
`HttpReShareClient`. Payload shape:

```json
{
  "action": "send_request | cancel_request | confirm_shipment | confirm_return | recall_request",
  "args":   { "...": "method kwargs minus idempotency_key" }
}
```

`idempotency_key` always comes from the outbox row, never from the
payload — that's what makes the dispatch replay-safe even if the
worker crashes after the wire call but before
`outbox_mark_delivered` commits.

### 3.3 Which saga steps go through the outbox

| Step          | Forward                              | Compensator                          |
| ------------- | ------------------------------------ | ------------------------------------ |
| `submit`      | ledger only                          | ledger only                          |
| `route`       | ledger only                          | ledger only                          |
| `approve`     | **inline `submit_to_supplier`** ¹    | outbox `cancel_request`              |
| `ship`        | outbox `confirm_shipment`            | outbox `recall_request` ²            |
| `return`      | outbox `confirm_return`              | ledger only (DISPUTED)               |

¹ APPROVE forward calls ReShare inline because the saga ledger needs
the returned `reshare_id` stamped onto its forward-event payload —
SHIP/RETURN read it back via `_derive_extras`. Migration to outbox
needs either an `APPROVING` intermediate state or a worker-written
observation event. Future ADR. See `CLAUDE.md` known-gaps.

² `HttpReShareClient.recall_request` raises `ClientError` until the
mod-rs recall mapping is verified against a live tenant. Under the
outbox pattern this surfaces as a `dead_letter` row for staff review
— exactly the signal we want. The mock client succeeds, keeping demo
+ tests green. See ADR-0011.

### 3.4 Backoff & dead-letter

On handler exception:
- `attempts += 1`
- `last_error = str(exc)[:2048]`
- `scheduled_for = now() + base_backoff_secs * 2**(attempts-1)`
  (`base_backoff_secs = 60` by default)
- when `attempts >= max_attempts` (default `10`): `status = 'dead_letter'`
  and the row is no longer picked up.

Worker logs at WARN with `outbox.retry_scheduled` and at ERROR with
`outbox.dead_letter`. Cumulative retry window with defaults: ~17 hours.

### 3.5 Operational queries

Pending vs delivered vs dead-letter snapshot:

```sql
SELECT status, count(*) FROM outbox GROUP BY status;
```

Recent dead-letters with cause:

```sql
SELECT id, target, idempotency_key, attempts, last_error
FROM outbox
WHERE status = 'dead_letter'
ORDER BY id DESC
LIMIT 20;
```

Stuck-pending (scheduled in past, attempts > 0):

```sql
SELECT id, target, attempts, scheduled_for, last_error
FROM outbox
WHERE status = 'pending'
  AND scheduled_for < now()
ORDER BY scheduled_for
LIMIT 50;
```

### 3.6 Multi-worker safety

The current poll uses no row-level lock. Running two workers in
parallel against the same DB **will double-deliver**. Postgres can
fix this with `SELECT ... FOR UPDATE SKIP LOCKED`; SQLite cannot. Out
of scope for the prototype — flagged in `CLAUDE.md` and the
`outbox.py` module docstring.

For now: one API process per DB.

---

## 4. Overdue scanner

### 4.1 What it does

`OverdueScanner.scan()` (in `src/agora/agents/tracking.py`) walks
sagas in `current_state='shipped'`, reads the `due_at` stamped on
each saga's most recent committed SHIP forward, and appends an
**OBSERVATION event** when `due_at < now`. It does *not* change
lifecycle state — that decision belongs to staff. The observation
surfaces as a badge in the staff console.

### 4.2 Idempotency

The observation idempotency key is `f"overdue-{saga_id}"`. Re-running
the scan is safe: the saga ledger's `UNIQUE(idempotency_key)`
constraint absorbs the duplicate — `ledger.append` returns the
existing row, and the scanner reports `newly_recorded=False`.

### 4.3 Schedule

There is no built-in cron in the prototype. To run periodically pick
one of:

```bash
# One-off
.venv/Scripts/python.exe -c "import asyncio; from sqlalchemy.ext.asyncio import async_sessionmaker; from agora.saga.db import get_engine; from agora.agents.tracking import OverdueScanner; asyncio.run(OverdueScanner(async_sessionmaker(bind=get_engine(), expire_on_commit=False)).scan())"

# Production placeholder: add a second asyncio.Task in the FastAPI
# lifespan that loops `scan()` + `asyncio.sleep(interval)`. The
# interval var (e.g. AGORA_OVERDUE_SCAN_SECS) is not yet defined in
# config.py — mirror the outbox worker pattern in src/agora/api/app.py
# when wiring this up. Tracked in CLAUDE.md gaps.
```

Test coverage lives in `tests/test_tracking.py` (deterministic clock
via `now_fn` injection).

---

## 5. Replay & idempotency

### 5.1 Keys

All keys are ULIDs minted by `new_idempotency_key(prefix=...)`
(`src/agora/saga/idempotency.py`). The prefix is for grep-ability in
logs only — the ULID itself guarantees uniqueness.

Conventions used today:

| Prefix       | Where minted                                      |
| ------------ | ------------------------------------------------- |
| `submit-`    | first SUBMIT forward (patron-side endpoint)       |
| `route-`     | ROUTE step (`/approve` or demo)                   |
| `approve-`   | APPROVE step                                      |
| `ship-`      | SHIP step                                         |
| `return-`    | RETURN_ITEM step                                  |
| `comp-`      | compensator events                                |
| `gate-`      | open/commit gate events                           |
| `overdue-`   | overdue observation (deterministic, no ULID)      |

### 5.2 Where uniqueness lives

- `saga_event.idempotency_key` is `UNIQUE`. Duplicate insert during
  `SagaLedger.append` is caught inside a savepoint
  (`begin_nested()`), the existing row is returned, and the caller's
  outer transaction is **not** rolled back. This is the core
  replay-safety mechanism — see § 5.3 below.
- `outbox.idempotency_key` is also `UNIQUE`. The outbox worker passes
  the row's key into the handler so a wire retry after a crash
  reaches the remote target with the same key.
- `inbox.message_id` is the PK on the inbox table; `inbox_record`
  no-ops on duplicates.

### 5.3 Why mod-rs is not in the loop

`HttpReShareClient` does not honour `Idempotency-Key` headers — mod-rs
predates the convention. Replay-safety lives entirely in the saga
ledger's `UNIQUE` constraint, not on the wire. If you replay an
APPROVE that already committed, the ledger insert fails the
uniqueness check, the savepoint rolls back, and the prior ReShare
call is *not* re-issued.

### 5.4 Manual replay

Replays are normally automatic (worker crash + restart). To force a
replay of one step during incident response:

1. Look up the existing event: `SELECT * FROM saga_event WHERE saga_id = $1 AND step = $2 ORDER BY seq DESC LIMIT 1;`
2. Re-issuing the same `idempotency_key` is a no-op.
3. To genuinely re-run the step (e.g. the previous attempt was
   logically wrong), append a **compensator** first, then a fresh
   forward with a new idempotency key. Never edit a committed event
   — the ledger is append-only and that is the invariant.

---

## 6. Dead-letter triage

When an outbox row hits `status='dead_letter'`:

1. **Find it**: see § 3.5 query.
2. **Read `last_error`**. Mod-rs auth/permission errors and
   `recall_request` (no first-class action) are the two known
   surfaces today.
3. **Decide**: is the saga state actually wrong, or is it the wire
   call that failed?
   - *Saga state is right, wire flaked*: re-queue. Update the row to
     `status='pending'`, bump `scheduled_for` to now, leave
     `attempts` as-is so the next failure dead-letters again
     quickly.
     ```sql
     UPDATE outbox
     SET status = 'pending', scheduled_for = now(), last_error = NULL
     WHERE id = $1;
     ```
   - *Saga state is wrong*: append the appropriate compensator via
     `POST /sagas/{id}/compensate`. The dead-letter row stays as an
     audit record; do not delete it. The compensator may enqueue a
     fresh outbox row with a new idempotency key.
4. **Never edit `idempotency_key`**. That's the fingerprint — change
   it and you've lost the replay-safety claim.

---

## 7. Common failures

### 7.1 GPG pinentry hangs on commit

The repo signs commits. If pinentry times out, the commit hangs.
Symptoms: `git commit` blocks indefinitely; no editor opens.

Fix: kill the hung process, then either restart the GPG agent
(`gpgconf --kill gpg-agent`) or ask before bypassing. Do **not**
pass `--no-gpg-sign` without explicit user approval — see CLAUDE.md.

### 7.2 `Unused "type: ignore" comment` from mypy

Means a previous error was fixed but the suppressor was left behind.
Remove the `# type: ignore[...]` line and re-run. The mypy config
sets `warn_unused_ignores = true` (via `--strict`); CI will keep
catching these.

### 7.3 Tests pass but `httpx.AsyncClient` doesn't see lifespan

`httpx.ASGITransport` does **not** fire FastAPI lifespan events. Any
test that needs the outbox worker running has to enter the lifespan
manually:

```python
async with app.router.lifespan_context(app):
    ...
```

See `tests/test_api.py::test_outbox_worker_starts_and_stops_with_lifespan`.

### 7.4 `cache_clear` after `monkeypatch.setenv`

`get_settings()` is `@lru_cache`. Tests that flip env vars must call
`get_settings.cache_clear()` after the `monkeypatch.setenv` and again
in `finally:` to avoid leaking the cached settings into neighbouring
tests.

### 7.5 SQLite `BIGINT` PK doesn't autoincrement

Use the `_bigint_pk()` helper from `src/agora/saga/db.py` for any new
autoincrement PK column. SQLite only auto-increments columns typed
as `INTEGER PRIMARY KEY`; `BIGINT` columns silently store NULL.

### 7.6 UUID binding fails on SQLite

Use the `_PortableUUID` TypeDecorator (Postgres native UUID, SQLite
`CHAR(36)`). The stdlib `sqlite3` driver does not bind `uuid.UUID`
instances directly.

### 7.7 `GateRequiredError` on a forward step

The forward step has no committed gate event. Either the `/approve`
call was skipped, or the gate was rejected. Inspect the saga
timeline (`GET /sagas/{id}`) for a missing or `failed` gate event on
that step.

### 7.8 `ReShareClient.recall_request` always errors

Expected on `HttpReShareClient`. mod-rs has no first-class recall
action; the call raises `ClientError`. The mock client succeeds. In
production this surfaces as a dead-letter on the SHIP compensator —
correct behaviour until the recall mapping is confirmed against a
live tenant.

### 7.9 Postgres connection refused after `docker compose up`

Port 5433, not 5432 (avoids host-Postgres collision). Wait for the
healthcheck to pass:

```bash
docker compose ps postgres
# Look for STATUS = "healthy"
```

---

## 8. Invariants — never violate

These are the non-negotiables. Re-read before any change to the
saga or coordinator code.

1. **Saga ledger is the source of truth.** `saga.current_state` is a
   denormalised projection. Never trust it over the event stream.
2. **Append uses a savepoint.** Duplicate idempotency-key insert must
   roll back the savepoint, not the caller's outer transaction.
3. **Forward step requires a committed gate.** No exceptions. Agents
   are advisory.
4. **Compensators only run against committed forwards.** Look up via
   `find_committed_forward` first.
5. **Idempotency keys are ULIDs.** Never reuse, never edit, never
   delete a committed event.
6. **All datetimes are timezone-aware UTC.** Use `datetime.now(UTC)`.
7. **ReShare is wrapped, not reimplemented.** ISO 18626 wire-level
   correctness lives in mod-rs. Validate XML against the published
   XSD before going live with real peers.

---

## 9. References

- `CLAUDE.md` — invariants, known gaps, behavioural rules
- `docs/prd/` — product requirements
- `docs/adr/` — architecture decisions (ReShare wrap, saga ledger,
  outbox commit-then-enqueue, ULID keys, etc.)
- `docs/architecture.md` — Mermaid diagram
- `src/agora/api/app.py` — FastAPI factory + lifespan
- `src/agora/saga/outbox.py` — worker module docstring
- `src/agora/saga/flows.py` — forward+compensator pairs
