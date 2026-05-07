# Agora Runbook

> Last reviewed against code: 2026-05-04 (post PRs #41-#90 — PR-2b
> routing-LLM adapter adds `AGORA_ROUTING_LLM_*` env vars + a sibling
> `routing-eval-floor.yml` CI workflow alongside `triple-gate` /
> `audit` / `postgres-tests`; ε retuned to 0.03 (#51); DiscoveryAgent
> wired with `POST /sagas/{id}/discover` endpoint (#46/#53); ISO
> 18626 XSD validation harness shipped (#52); Vertex env-routing
> rows added for `eval-routing --llm` and silent-fallback failure
> mode noted on `AGORA_ROUTING_LLM_ENABLED` (#75); RoutingAgent
> format-affinity feature in #79 closes `routing-015` and bumps
> the LLM-augmented baseline to 20/20; staff console UI first slice
> ships in #80 — `GET /` inbox via HTMX + Jinja2, ADR-0015;
> NCIP item-barcode wired (#89); override endpoint (#90)).

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

# 2. Project + dev extras (aiosqlite bundled in [dev] since PR #28)
.venv/Scripts/python.exe -m pip install -e ".[dev]"

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
| `AGORA_DB_URL`                      | `postgresql+asyncpg://agora:agora@localhost:5433/agora` <!-- pragma: allowlist secret --> | Tests override to `sqlite+aiosqlite:///:memory:`. Dev-default; production sets `AGORA_DB_URL`. |
| `AGORA_DB_POOL_SIZE`                | `10`                                                   |                                                            |
| `RESHARE_BASE_URL`                  | `""`                                                   | Empty → mock client. Non-empty triggers real HTTP client.  |
| `RESHARE_TENANT`                    | `consortium-a`                                         | Maps to mod-rs `X-Okapi-Tenant` (login + data requests).   |
| `RESHARE_USER` / `RESHARE_PASSWORD` | `""`                                                   | HTTP Basic for dev; reused as Okapi creds when `OKAPI_URL` is set (ADR-0013). |
| `OKAPI_URL`                         | `""`                                                   | When set, `HttpReShareClient` authenticates via FOLIO Okapi token flow (`POST {OKAPI_URL}/authn/login`) instead of HTTP Basic. See ADR-0013. |
| `NCIP_BASE_URL`                     | `""`                                                   | Empty → `MockNcipClient`. Non-empty → `HttpNcipClient` (PR #98/#99; source-review-only; live probe pending). |
| `NCIP_AGENCY_ID`                    | `AGORA-DEV`                                            | Agency symbol stamped on NCIP requests.                    |
| `AGORA_SRU_ENABLED`                 | `false`                                                | Opt-in for `agora.clients.sru.get_sru_client()`. `false` → factory returns the in-memory mock; `true` → live HTTP client against `SRU_LOC_URL`. Explicit boolean rather than URL-presence (the default URL is non-empty). |
| `SRU_LOC_URL`                       | `https://lx2.loc.gov/voyager`                          | Library of Congress SRU.                                   |
| `SRU_TIMEOUT_SECS`                  | `5.0`                                                  |                                                            |
| `AGORA_CROSSREF_ENABLED`            | `false`                                                | Opt-in for `agora.clients.crossref.get_crossref_client()`. `false` → factory returns the in-memory mock; `true` → live HTTP client against `CROSSREF_BASE_URL`. Explicit boolean rather than URL-presence (the default URL is non-empty). |
| `CROSSREF_BASE_URL`                 | `https://api.crossref.org`                             | CrossRef REST API (DOI → bibliographic record). Public, no auth. |
| `CROSSREF_TIMEOUT_SECS`             | `5.0`                                                  |                                                            |
| `CROSSREF_MAILTO`                   | `""`                                                   | When set, opts into CrossRef's polite pool with `User-Agent: Agora/0.1 (mailto:<value>)` for better rate limits. |
| `AGORA_CONSORTIUM_MEMBERS`          | `""`                                                   | Comma-separated list of in-consortium agency symbols (e.g. `MEMBER1, MEMBER2`). Threaded into `DiscoveryAgent.consortium_members` at app build time (#56). Whitespace around tokens is stripped; duplicates collapse; trailing commas are tolerated. Empty default keeps the pre-PR behaviour where no candidate was flagged in-consortium. |
| `SAGA_STALL_TIMEOUT_SECS`           | `600`                                                  | Reserved for future stall detection.                       |
| `OUTBOX_RETRY_MAX_ATTEMPTS`         | `10`                                                   | Beyond this → `dead_letter`.                               |
| `AGORA_OUTBOX_WORKER_ENABLED`       | `true`                                                 | Set `0` to suppress lifespan-spawned worker (tests, etc.). |
| `AGORA_OUTBOX_POLL_INTERVAL_SECS`   | `1.0`                                                  | Worker poll interval.                                      |
| `AGORA_TRACKING_SCANNER_ENABLED`    | `true`                                                 | Set `0` to suppress lifespan-spawned overdue scanner.      |
| `AGORA_TRACKING_SCAN_INTERVAL_SECS` | `300.0`                                                | Overdue scanner poll interval (5 min default).             |
| `AGORA_TRACKING_RECALL_AFTER_DAYS`  | `14`                                                   | Days past `due_at` before tier-2 `recall-proposed` fires.  |
| `AGORA_TRACKING_UNCONFIRMED_RECEIPT_AFTER_DAYS` | `7`                                        | Days past `shipped_at` (with no RECEIVE event) before tier-3 `receipt-unconfirmed` fires. Independent of tier-1/2; tracks transit time. |
| `AGORA_ROUTING_TIEBREAK_EPSILON`    | `0.03`                                                 | RoutingAgent LLM tie-breaker activation threshold. When the rules-baseline scoring puts the top-2 candidates within this gap, `RoutingAgent` consults the configured `LlmTiebreaker` (if any). Tuned against eval in #51 (tightened from 0.05 → 0.03 so `routing-009` skips the LLM — rules already get it right) — see ADR-0014. |
| `AGORA_ROUTING_LLM_ENABLED`         | `false`                                                | Opt-in for `agora.agents.factories.get_llm_tiebreaker()`. `false` → factory returns `None` (rules-only path). `true` → factory builds `AdkLlmTiebreaker`. Requires bound GCP ADC + Vertex AI API enabled **+ `GOOGLE_GENAI_USE_VERTEXAI=true` in the process env** (without it the SDK silently falls back to public-Gemini API-key auth and 401s every call — seam catches and runs rules-only, output looks "successful" with the wrong numbers). |
| `AGORA_ROUTING_LLM_MODEL`           | `gemini-2.5-flash`                                     | Vertex/Gemini model id for the routing tie-breaker. gemini-2.5-flash is the model used in the committed LLM-augmented baseline (top-1 0.95, post-#7c); the old default gemini-2.0-flash 404s under the current Vertex enablement. |
| `AGORA_ROUTING_LLM_TIMEOUT_SECS`    | `30.0`                                                 | Per-call timeout. Raised from 5s (too tight for Gemini 2.5 cold-start) to 30s to match the eval harness recommendation. Stuck LLM raises; the seam catches and falls back to the rules pick + diagnostic. |
| `AGORA_ROUTING_LLM_LOCATION`        | `us-central1`                                          | Vertex AI region for the `LlmAgent` runtime.               |
| `AGORA_CONSOLE_USERNAME`            | `staff`                                                | HTTP Basic username for the staff console HTML routes. Ignored when `AGORA_CONSOLE_PASSWORD` is empty. |
| `AGORA_CONSOLE_PASSWORD`            | `""`                                                   | HTTP Basic password. Empty (default) disables auth entirely — no credentials required in local dev. Set a non-empty value to enable. JSON API routes are unaffected (trusted-network assumption, ADR-0007). |
| `GOOGLE_GENAI_USE_VERTEXAI`         | `true`                                                 | Read by `google-adk` / `google-genai`, not by Agora `Settings`. Mirrors `.env.example`. **Required** when `AGORA_ROUTING_LLM_ENABLED=true` — without it the SDK routes through the public Gemini API instead of Vertex/ADC and 401s every call. |
| `GOOGLE_CLOUD_PROJECT`              | `""`                                                   | Read by `google-adk` / `google-genai`. The GCP project that hosts the bound ADC + Vertex enablement. Set to your project id (e.g. `my-project-1234`). |
| `GOOGLE_CLOUD_LOCATION`             | `us-central1`                                          | Read by `google-adk` / `google-genai`. Vertex region; should match `AGORA_ROUTING_LLM_LOCATION`. |

`.env.example` in the repo lists the same set with safe defaults.

### 1.3 Schema

The Alembic head migration creates every table the app needs. For
fresh databases:

```bash
.venv/Scripts/python.exe -m alembic upgrade head
```

Tests bypass Alembic and call `Base.metadata.create_all()` against an
in-memory SQLite engine (see `tests/conftest.py`). The Alembic path is
exercised against `postgres:15-alpine` on every CI run via
`tests/test_alembic_postgres.py` + `postgres-tests.yml` (PR #24).

### 1.4 Smoke tests

Run after install or on any pull:

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy                # uses pyproject files = ["src"]
.venv/Scripts/python.exe -m agora.demos.happy_path
```

The demo runs the full lifecycle Submitted → Routed → Approved →
Shipped → Received → Returned against in-memory SQLite +
`MockReShareClient` and prints the resulting ledger. If any step
prints an error, do not serve the API.

### 1.5 Serve the API

```bash
.venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

`create_app()`'s lifespan spawns two `asyncio.Task`s — the outbox
worker and the overdue scanner — and cancels both on shutdown. See
§ 3 and § 4 below.

---

## 2. Lifecycle & gate workflow

### 2.1 States

```
Submitted → Routed → Approving → Approved → Shipped → Received → Returned
```

(`Approving` is the in-flight intermediate while the outbox worker delivers the
`send_request` call and stamps `reshare_id`; staff cannot compensate during this
window — see ADR-0012.)

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
| `POST /sagas/{id}/discover`      | Run DiscoveryAgent against the saga's stored request; writes a ROUTE-anchored OBSERVATION (#53). Saga state unchanged. |
| `POST /sagas/{id}/override`      | Resolve a DISPUTED saga → CANCELLED or UNFILLED (PR #90). Writes a ledger OBSERVATION (`step=resolve`, `outcome=committed`); no outbox dispatch. Open ILS loans must be settled out-of-band. |
| `GET /browser`                   | Saga browser — filter all sagas by state, library, date. Read-only staff console page (PR #93). |

### 2.3 What `/approve` derives vs requires

For each step the request body carries `step`, `actor`, `rationale`,
and an optional `extras` dict. The handler derives missing inputs
from the prior committed forwards:

| Step       | Required `extras` (if not derivable)               | Derived from                                  |
| ---------- | -------------------------------------------------- | --------------------------------------------- |
| `route`    | `chosen_supplier`                                  | — (first step where staff picks a supplier)   |
| `approve`  | none                                               | `chosen_supplier` from ROUTE forward          |
| `ship`     | none                                               | `reshare_id` from APPROVE forward             |
| `receive`  | none                                               | `reshare_id` from APPROVE forward             |
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
attempts, scheduled_for, last_error, delivered_at, claimed_at)`.
Status is one of `pending | in_flight | delivered | dead_letter`;
`claimed_at` carries the multi-worker lease (PR #25). The worker
reads `status='pending' AND scheduled_for <= now()`, ordered by
schedule time, in batches of 50, claiming each via
`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres (see § 3.6).

Registered handlers: `target='reshare'`
(`make_reshare_handler(client)` → `MockReShareClient` /
`HttpReShareClient`) and `target='ncip'` (fire-and-forget,
borrower-side ILS). Payload shape:

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

| Step          | Forward                                                       | Compensator                          |
| ------------- | ------------------------------------------------------------- | ------------------------------------ |
| `submit`      | ledger only                                                   | ledger only                          |
| `route`       | ledger only                                                   | ledger only                          |
| `approve`     | outbox `send_request` → APPROVING → projection → APPROVED ¹   | outbox `cancel_request` ²            |
| `ship`        | outbox `confirm_shipment` (reshare only — NCIP `check_out` re-anchored to RECEIVE) | outbox `recall_request` ³ (no NCIP rollback in either branch ⁵) |
| `receive`     | outbox `check_out` to NCIP — borrower-receipt opens the ILS loan ⁴ | ledger only (DISPUTED) ⁶            |
| `return`      | outbox `confirm_return` (+ outbox `check_in` to NCIP)         | ledger only (DISPUTED)               |

¹ APPROVE migrated to outbox per ADR-0012 / PR #17. The forward
returns an `OutboxIntent` for `send_request` and parks the saga in
`LifecycleState.APPROVING`; the worker drains it, then the
projection callback (`make_reshare_on_success`) writes an
OBSERVATION carrying `reshare_id` that advances the saga to
APPROVED. Projection runs in the same session as
`outbox_mark_delivered` so OBSERVATION + delivered flag commit
atomically. SHIP/RETURN read `reshare_id` back via `_derive_extras`,
which reads APPROVE OBSERVATION events as well as FORWARD events.

² Compensating during the APPROVING window (ack still pending)
returns 400 — there is no `reshare_id` to cancel against.

³ `HttpReShareClient.recall_request` raises `ClientError` until the
mod-rs recall mapping is verified against a live tenant. Under the
outbox pattern this surfaces as a `dead_letter` row for staff review
— exactly the signal we want. The mock client succeeds, keeping demo
+ tests green. See ADR-0011 + ADR-0012.

⁴ RECEIVE is the borrower-side physical-receipt confirmation. ISO
18626 names this an `ItemReceived` note (the supplier-side state stays
`Loaned`, no peer status flip); on Agora's side the forward emits a
single `target="ncip"` `check_out` intent (idempotency-key suffix
`:ncip`) so the patron's local ILS opens the loan from physical
receipt rather than supplier shipment. `due_at` still anchors to
`shipped_at` because the loan-period clock is a supplier-side
commitment (a saga whose patron never confirms receipt still needs
an overdue threshold). `item_id = reshare_id` per the prototype
approximation documented in `saga/flows.py` § RECEIVE.

⁵ SHIP-compensator NCIP rollback is **gone** post NCIP-checkout
re-anchor (this PR). Both branches converge on a single ReShare
`recall_request` intent: at SHIPPED no ILS loan was ever opened
(RECEIVE forward never ran), at RECEIVED the patron physically holds
the book so the loan correctly reflects custody and the eventual
return flow owns `check_in`. The `current_state` check survives only
as state-aware rationale text on the StepResult; functionally the
outbox payload is identical. The earlier state-aware NCIP rollback
(PR #37, idempotency-key suffix `:ncip-rollback`) compensated for the
upstream design tension that the re-anchor removed — see
`docs/lessons.md` § Saga / ledger.

⁶ RECEIVE compensator is deliberately ledger-only DISPUTED even
though the forward now opens an ILS loan. The saga can't tell whether
a receipt dispute is about non-receipt (loan should clear) or
condition (loan should stay) — routing to DISPUTED preserves the
"physically un-undoable" framing for staff resolution. The `/sagas/{id}/override` endpoint is implemented (PR #90) — resolves
DISPUTED → CANCELLED or UNFILLED via a ledger OBSERVATION event.
A state-aware compensator with ILS check_in logic remains future work.

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

Multi-worker safe on Postgres via `outbox_claim` (PR #25):
`SELECT ... FOR UPDATE SKIP LOCKED` acquires disjoint row sets,
flips claimed rows to `status='in_flight'` with `claimed_at=now()`,
and commits — concurrent workers can't double-deliver. Orphan
recovery sweeps `in_flight` rows whose `claimed_at` is older than
`claim_lease_secs` (default 600s) back to `pending` so a crashed
worker doesn't strand rows. SQLite serializes writers naturally so
the same code path works in tests; the `with_for_update` hint is
only emitted on Postgres. Verified by
`tests/test_outbox_concurrent_postgres.py` (CI service container).

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

`OverdueScanner.run_forever` runs as a background `asyncio.Task`
spawned from the FastAPI lifespan in `src/agora/api/app.py` (task
name `agora.tracking.scanner`), polling at
`AGORA_TRACKING_SCAN_INTERVAL_SECS` (default 300s). Three-tier
emission per pass (all advisory, no outbox, no state change —
ADR-0005):

- **Tier-1** `overdue-{saga_id}` on the first scan past `due_at`
  (loan-clock time).
- **Tier-2** `recall-proposed-{saga_id}` on the first scan where
  `days_overdue >= AGORA_TRACKING_RECALL_AFTER_DAYS` (default 14);
  carries `suggested_action: "compensate_ship"` for the staff
  console CTA.
- **Tier-3** `receipt-unconfirmed-{saga_id}` on the first scan where
  `now - shipped_at >= AGORA_TRACKING_UNCONFIRMED_RECEIPT_AFTER_DAYS`
  (default 7) and the saga is still at SHIPPED — patron has not
  confirmed RECEIVE. Tier-3 fires *independently* of tier-1/2 (a
  saga can be tier-3 only with `due_at` still in the future).
  Tracks transit time, not loan-clock time. No `suggested_action`
  field — staff console surfaces it as a "chase patron" hint
  without an in-saga CTA. Closes the gap that PR #38 documented
  when re-anchoring NCIP `check_out` from SHIP to RECEIVE.

Disable for local debugging:

```bash
AGORA_TRACKING_SCANNER_ENABLED=0 .venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

One-off invocation (without spawning the loop):

```bash
.venv/Scripts/python.exe -c "import asyncio; from sqlalchemy.ext.asyncio import async_sessionmaker; from agora.saga.db import get_engine; from agora.agents.tracking import OverdueScanner; asyncio.run(OverdueScanner(async_sessionmaker(bind=get_engine(), expire_on_commit=False)).scan())"
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
| `receive-`   | RECEIVE step                                      |
| `return-`    | RETURN_ITEM step                                  |
| `comp-`      | compensator events                                |
| `gate-`      | open/commit gate events                           |
| `overdue-`   | overdue observation (deterministic, no ULID)      |
| `discovery-` | discovery observation from `POST /sagas/{id}/discover` (#53; fresh ULID per call) |

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
