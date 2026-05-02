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

- `HttpReShareClient` endpoint paths/payloads are educated guesses
  — see comment in `clients/reshare.py`. Verify against running
  ReShare before driving real traffic.
- TrackingAgent is a stub (no overdue-detection cron yet).
- NCIP client is mock-only.
- Outbox **worker** is implemented (`saga/outbox.py`: `OutboxWorker`,
  `make_reshare_handler`) but is **not yet wired into saga flows** —
  forward steps still call ReShare inline via `TransactionAgent`.
  Migration to "commit ledger then enqueue" is its own ADR / change.
  Worker assumes a single drainer; multi-worker safety needs
  `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres-only).
- `POST /sagas/{id}/approve` and `POST /sagas/{id}/compensate` are
  wired end-to-end (commit gate + run forward / run compensator in
  one transaction). Step inputs (`chosen_supplier`, `reshare_id`) are
  derived from prior committed forwards; the request body's `extras`
  field overrides where derivation is impossible (e.g. first ROUTE).
- Alembic migration never tested against real Postgres — only SQLite
  via `Base.metadata.create_all()`.
- mypy installed but not run end-to-end (Protocol covariance issues
  with mock clients).
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
