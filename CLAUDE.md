# CLAUDE.md — Agora ILL Project Context

Agentic Inter-Library Loan (ILL) prototype wrapping FOLIO/ReShare for
the standards layer (ISO 18626, NCIP, SRU, OpenURL). Multi-library
**consortium** model. **Human approval at every state transition**
(default-deny autonomy). Research prototype — FedRAMP alignment-noted
only, not authorized.

Lifecycle: **Submitted → Routed → Approved → Shipped → Returned** with
saga compensators paired to every forward step.

## Quick start

```bash
# Setup (one-time)
.venv/Scripts/python.exe -m pip install -e ".[dev]" aiosqlite

# Verify
.venv/Scripts/python.exe -m pytest tests/ -q              # 20 tests
.venv/Scripts/python.exe -m ruff check src tests          # lint
.venv/Scripts/python.exe -m agora.demos.happy_path        # end-to-end demo

# Serve API
.venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

## Stack

- Python 3.11+ (built/tested on 3.14.3)
- FastAPI + pydantic v2 + pydantic-settings
- SQLAlchemy 2.x async + asyncpg (Postgres) / aiosqlite (tests)
- Alembic migrations
- pytest + pytest-asyncio (`asyncio_mode = "auto"`) + Hypothesis
- ruff (lint+format), mypy (aspirational, not gating)
- Google ADK optional via `[adk]` extra
- structlog + tenacity + httpx + lxml + python-ulid

## Repo layout

```
agora/
├── prompts/build-agora.md       # Bootstrap prompt for fresh sessions
├── docs/
│   ├── prd/  (7 docs)           # Product requirements
│   ├── adr/  (10 docs)          # Architecture decisions
│   └── architecture.md          # Hand-drawn Mermaid diagram
├── alembic/versions/            # DB migrations
├── src/agora/
│   ├── agents/                  # 6 advisory agents (discovery, routing,
│   │                            #   policy, transaction, tracking,
│   │                            #   reconciliation)
│   ├── api/app.py               # FastAPI staff console
│   ├── clients/                 # ReShare / NCIP / SRU / OpenURL
│   ├── demos/happy_path.py      # End-to-end runnable demo
│   ├── models/                  # pydantic domain models
│   ├── saga/                    # ledger, coordinator, idempotency,
│   │                            #   flows (forward+compensator pairs)
│   ├── cli.py / config.py / logging.py
├── tests/                       # 20 tests (unit + property + e2e)
├── docker-compose.yml           # Postgres-only sandbox
├── Makefile / pyproject.toml
```

## Architecture invariants

**Saga ledger is source of truth.** `saga.current_state` is a
denormalised projection — never trust it over the event stream.

**Append always uses a savepoint** (`begin_nested()` in
`saga/ledger.py`). Duplicate idempotency-key insert MUST NOT roll back
the caller's outer transaction — only the savepoint.

**Forward step requires a committed gate event.** `Coordinator.run_forward`
raises `GateRequiredError` if no committed gate exists for the step.

**Idempotency keys are ULIDs** with semantic prefix
(`route_01HXY...`). UNIQUE constraint on `saga_event.idempotency_key`
makes replay safe — duplicate insert returns the prior row, not an
error.

**Agents are advisory.** They produce recommendations + rationale.
Staff click in the console commits the gate. Agent never auto-commits.

**Compensators run only against committed forward steps.** Look up via
`SagaLedger.find_committed_forward(step)` before issuing a compensator.

**ReShare is wrapped, not reimplemented.** ISO 18626 wire-level
correctness lives in mod-rs; we drive it via REST. Method names map to
ISO 18626 message types — see table in `clients/reshare.py`.

## Conventions

- All datetime is timezone-aware UTC (`datetime.now(UTC)`).
- Database UUID columns use `_PortableUUID` TypeDecorator (Postgres
  native UUID / SQLite CHAR(36)).
- BIGINT autoincrement PKs use `_bigint_pk()` helper (BigInteger on
  Postgres, Integer on SQLite — required for SQLite rowid).
- Idempotency keys created via `new_idempotency_key(prefix=...)`.
- New saga steps: define forward + compensator in `saga/flows.py` and
  register in `build_registry()`. Add to `StepName` enum in
  `models/lifecycle.py`.
- New ADRs: copy a recent template in `docs/adr/`, increment number,
  write Status / Context / Decision / Consequences sections.

## Known gaps (do not silently fix — flag to user)

- `HttpReShareClient` paths and action vocabulary verified against
  mod-rs master (UrlMappings.groovy, PatronRequestController.groovy,
  Actions.groovy, ModuleDescriptor-template.json — see module
  docstring). Remaining unverified surface: (a) the create-request
  body shape (binds to the `PatronRequest` Grails domain class —
  caller's `request_payload` is passed through verbatim with the
  supplier merged under `supplyingInstitutionSymbol`), (b) response
  field names beyond `id`/`hrid`/`state`, (c) the recall-request
  mapping — mod-rs has no first-class action so `recall_request`
  raises `ClientError` until confirmed against a live tenant. Auth
  uses HTTP Basic (dev path); production needs Okapi token flow.
  mod-rs does not honour `Idempotency-Key` — replay-safety lives in
  the saga ledger's UNIQUE constraint, not the wire.
- NCIP fan-out is wired on SHIP and RETURN forwards (fire-and-forget,
  borrower-side ILS): `ship_forward` emits a second `target="ncip"`
  intent for `check_out`, `return_forward` emits one for `check_in`.
  NCIP outcomes do **not** gate saga state — failure surfaces as a
  stuck outbox row for staff review, the saga continues. The two
  intents per step share a base `ctx.idempotency_key`; the NCIP row
  is suffixed `:ncip` because `outbox.idempotency_key` is UNIQUE
  across all targets (see `saga/db.py`). Approximations documented
  in `saga/flows.py` SHIP comment block: (a) `item_id = reshare_id`
  because IllRequest has no real ILS barcode today; (b) `check_out`
  fires on supplier-shipped because there is no RECEIVED state — a
  future borrower-receipt confirmation flow should re-anchor it.
  Compensator-side NCIP rollback is **not** wired: SHIP-step rollback
  is ambiguous (item may still be in transit; patron may never have
  received it) so a real recall flow needs RECEIVED + receipt
  confirmation before deciding whether to issue a compensating
  `check_in`. The NCIP HTTP/SOAP client itself remains a mock —
  `MockNcipClient` for prototype/tests; real `mod-ncip` integration
  is still future work.
- TrackingAgent: `OverdueScanner.run_forever` now runs as a background
  task spawned from the FastAPI lifespan (asyncio task name
  `agora.tracking.scanner`; module is `agora.agents.tracking`),
  polling at `AGORA_TRACKING_SCAN_INTERVAL_SECS` (default 300s). Each
  pass scans shipped sagas past `due_at` and writes deterministic
  OBSERVATION events — UNIQUE constraint absorbs replay. Two-tier
  emission, both advisory only (no outbox, no state change, no
  auto-compensator dispatch — ADR-0005): tier-1 `overdue-{saga_id}`
  fires on the first scan past `due_at`; tier-2
  `recall-proposed-{saga_id}` fires on the first scan where
  `days_overdue >= AGORA_TRACKING_RECALL_AFTER_DAYS` (default 14)
  and carries `suggested_action: "compensate_ship"` plus the
  `reshare_id` for the staff console to render as a CTA pointing at
  `POST /sagas/{id}/compensate`. Recorded `days_overdue` is a
  point-in-time snapshot — UI computes "currently N days" from
  `due_at` + render clock. Auto-recall and a dedicated RECALLING
  lifecycle state are explicit non-goals; staff still clicks.
  Disable via `AGORA_TRACKING_SCANNER_ENABLED=0`. Single drainer
  assumed (same caveat as outbox worker).
- Outbox is wired into flows for every ReShare-touching step
  including APPROVE forward (ADR-0011 + ADR-0012). APPROVE forward
  is now pure: it returns an `OutboxIntent` for `send_request` and
  advances the saga to `LifecycleState.APPROVING`; the outbox
  worker drains the row, calls the supplier, and the projection
  callback (`make_reshare_on_success`) writes an OBSERVATION
  carrying `reshare_id` that advances the saga to `APPROVED`.
  Downstream SHIP/RETURN consume `reshare_id` via `_derive_extras`,
  which now reads APPROVE OBSERVATION events as well as FORWARD
  events. The compensator-during-APPROVING window (staff hits
  `/compensate` while the supplier ack is still pending) is
  rejected with a 400 — there is no `reshare_id` to cancel against.
  The projection runs **inside the same session** as
  `outbox_mark_delivered`, so the OBSERVATION write and the
  delivered flag commit atomically; a failed projection re-queues
  the row for retry without leaving the saga half-advanced. The
  API process spawns `OutboxWorker.run_forever` as an
  `asyncio.Task` from the FastAPI lifespan (`create_app`) via
  `_build_outbox_worker`, polling at
  `AGORA_OUTBOX_POLL_INTERVAL_SECS` (default 1.0s) and cancelled on
  shutdown. Disable with `AGORA_OUTBOX_WORKER_ENABLED=0`. The
  worker still assumes a single drainer per DB; multi-worker
  safety needs `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres-only).
- `POST /sagas/{id}/approve` and `POST /sagas/{id}/compensate` are
  wired end-to-end (commit gate + run forward / run compensator in
  one transaction). Step inputs (`chosen_supplier`, `reshare_id`) are
  derived from prior committed forwards; the request body's `extras`
  field overrides where derivation is impossible (e.g. first ROUTE).
- Alembic migration never tested against real Postgres — only SQLite
  via `Base.metadata.create_all()`.
- mypy `--strict` runs clean against `src/` AND `tests/` (configured
  in `pyproject.toml` with `files = ["src", "tests"]`). Package ships
  a `py.typed` marker so downstream consumers pick up the inline
  types.
- `pyproject.toml` declares `requires-python = ">=3.11"` but built on
  3.14.3.

## Test commands

```bash
.venv/Scripts/python.exe -m pytest tests/ -q                          # all tests
.venv/Scripts/python.exe -m pytest tests/test_coordinator.py -q       # one file
.venv/Scripts/python.exe -m pytest -k "contu" -q                      # by keyword
.venv/Scripts/python.exe -m pytest --hypothesis-show-statistics       # property tests
```

## Behavioural rules for Claude in this repo

- **Never auto-commit forward saga steps without a committed gate.**
  This breaks the human-in-loop invariant.
- **Never amend git commits unless the user explicitly asks.** Create
  a new commit instead. (User has GPG signing on; pinentry can hang —
  if it times out, ask before bypassing.)
- **Never skip hooks** (`--no-verify`, `--no-gpg-sign`) without
  explicit permission.
- **When adding DB columns or tables**, write a new Alembic revision
  in `alembic/versions/` AND update the ORM in `saga/db.py`. Do not
  rely on `create_all()` for production — only tests.
- **When extending the lifecycle**, update: `models/lifecycle.py`
  (`LifecycleState` + `StepName`), `saga/flows.py` (forward +
  compensator), `tests/test_coordinator.py` (happy path + gate-required),
  and the ISO 18626 mapping table in PRD `01-lifecycle-and-states.md`.
- **Preserve advisory-agent contract**: agents return a recommendation
  object with `rationale` field. They never write to the saga ledger
  directly.
- **Standards conformance is non-negotiable on the wire**: any code
  that produces ISO 18626 XML must validate against the published XSD
  before we go live with real peers. Today we delegate this to ReShare
  — keep it that way unless an ADR says otherwise.
- **Track lessons learned in `docs/lessons.md`.** When a PR finishes,
  ask: *did anything bite me that wasn't obvious from the spec?* If
  yes, append a dated paragraph to the relevant section, citing the
  PR/commit. Lessons are not ADRs — no decision is being made. They
  are concrete gotchas tied to a code location so the next session
  doesn't relearn them. See `docs/lessons.md` for format and existing
  entries.
