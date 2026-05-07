# Agora — Agentic Inter-Library Loan (ILL) System

> Research prototype. Multi-library consortium. Agents over FOLIO/ReShare.
> Saga + idempotency. Human-approval at every state transition.

> Last reviewed against code: 2026-05-05 (post PRs #41-#93 — adds
> Vertex env-routing requirement for `eval-routing --llm`,
> `sync-doc-counts` script + pytest gate as the single source of
> truth for test/ADR counts, RoutingAgent format-affinity feature
> (#79 closes routing-015), staff console UI first slice with
> HTMX + Jinja2 (ADR-0015, #80), NCIP item-barcode (#89), override
> endpoint (#90), override HTMX form (#92), saga browser (#93)).

## What this is

Agora is a research prototype that puts a multi-agent orchestration layer
on top of standards-compliant ILL infrastructure. The lifecycle
**Submitted → Routed → Approved → Shipped → Received → Returned** is
driven by agents that produce *recommendations*; humans approve every
transition. Every state change is recorded in an event-sourced saga
ledger with paired forward + compensator operations and ULID-keyed
idempotency.

The standards plumbing (ISO 18626 wire protocol, NCIP, etc.) is
delegated to FOLIO's `mod-rs` / ReShare and `mod-ncip`. Agora does not
reimplement them.

## Status

**Working prototype.** End-to-end demo runs via `make demo`
(`agora.demos.happy_path`). **492 tests** green (+6 postgres-only in CI).Saga + outbox + APPROVING-via-outbox (ADR-0012), multi-worker outbox
safety (`SELECT … FOR UPDATE SKIP LOCKED`), TrackingAgent three-tier
overdue scanner (overdue / recall-proposed / receipt-unconfirmed) wired
into the FastAPI lifespan, NCIP fan-out on RECEIVE / RETURN forwards,
DiscoveryAgent with CrossRef + SRU clients (`POST /sagas/{id}/discover`
endpoint), RoutingAgent LLM tie-breaker (ADR-0014, top-1 0.95 against
the 20-scenario eval), ISO 18626 XSD validation harness, and
Alembic-on-real-Postgres all shipped. CI gates: bandit + pip-audit +
detect-secrets, pytest + ruff + mypy --strict, alembic+ORM parity
against `postgres:15-alpine`, routing-eval rules-floor regression check.

See `docs/prd/` for product requirements, `docs/adr/` for architecture
decisions (17 ADRs through 0017), `docs/architecture.md` for the
hand-drawn diagrams, `docs/runbook.md` for operations, `docs/solution.md`
for the solution doc, `docs/lessons.md` for accumulated gotchas, and
`prompts/build-agora.md` to bootstrap a fresh dev session.

## Quick layout

```
agora/
├── prompts/             # Project bootstrap prompt
├── docs/
│   ├── prd/             # Product requirements (00-06)
│   ├── adr/             # Architecture decisions (0001-0016)
│   ├── architecture.md  # Hand-drawn Mermaid diagrams
│   ├── runbook.md       # Operations / on-call notes
│   ├── solution.md      # Solution overview
│   ├── lessons.md       # Accumulated gotchas (append-only)
│   └── standards/       # ISO 18626 XSD validator + fixtures
├── evals/routing/       # Routing tie-breaker eval scenarios + baselines
├── alembic/versions/    # DB migrations
├── scripts/             # validate_iso18626.py + tooling
├── src/agora/
│   ├── agents/          # Discovery, Routing, Policy, Transaction,
│   │                    #   Tracking (+ OverdueScanner), Reconciliation,
│   │                    #   AdkLlmTiebreaker (ADR-0014)
│   ├── saga/            # Coordinator, ledger, flows (forward+
│   │                    #   compensator pairs), steps, idempotency,
│   │                    #   outbox, db
│   ├── clients/         # ReShare (+ OkapiAuth), NCIP, SRU, CrossRef,
│   │                    #   OpenURL
│   ├── api/             # FastAPI staff console + lifespan
│   ├── demos/           # happy_path runnable end-to-end demo
│   ├── evals/           # Routing eval harness (run via make eval-routing)
│   ├── models/          # pydantic schemas (ISO 18626 subset)
│   ├── config.py / cli.py / logging.py / py.typed
├── tests/               # 492 unit + property + e2e (+6 postgres-only)├── .github/workflows/   # audit.yml, postgres-tests.yml, triple-gate.yml,
│                        #   routing-eval-floor.yml
├── docker-compose.yml   # Postgres-only sandbox today
├── Makefile
└── pyproject.toml
```

## Getting started

```bash
# 1. Set up env
cp .env.example .env
# (edit .env if needed; defaults work for local dev)

# 2. Install
make install

# 3. Bring up Postgres
make up

# 4. Run migrations
make migrate

# 5. Run tests
make test

# 6. Run the API
make api
# → http://localhost:8000/docs
```

## Standards & specs

| Standard | Role | Implementation strategy |
|---|---|---|
| ISO 18626:2021 | Peer-to-peer ILL messaging | Delegated to ReShare `mod-rs`; XSD validation harness in `scripts/validate_iso18626.py` for the day Agora emits XML directly |
| NCIP / Z39.83 | Library ↔ ILS circulation | `HttpNcipClient` (source-review-only, PR #98/#99); `MockNcipClient` default; live mod-ncip probe pending |
| SRU | Catalog discovery | Direct HTTP client (`agora.clients.sru`) |
| CrossRef REST | DOI → bibliographic record | Direct HTTP client (`agora.clients.crossref`) |
| OpenURL | Citation resolution | Pure-Python parser |
| Z39.50 (binary) | Legacy catalog discovery | **Not implemented** (see ADR-0006) |
| FedRAMP | US gov cloud security | **Alignment-noted only** (see ADR-0007) |

## Documentation index

- [PRD 00 — Overview](docs/prd/00-overview.md)
- [PRD 01 — Lifecycle & State Machine](docs/prd/01-lifecycle-and-states.md)
- [PRD 02 — Agents](docs/prd/02-agents.md)
- [PRD 03 — Saga & Idempotency](docs/prd/03-saga-and-idempotency.md)
- [PRD 04 — Discovery](docs/prd/04-discovery.md)
- [PRD 05 — Staff Console](docs/prd/05-staff-console.md)
- [PRD 06 — Non-Functional Requirements](docs/prd/06-non-functional.md)
- [Architecture diagrams](docs/architecture.md)
- [Runbook](docs/runbook.md)
- [Solution overview](docs/solution.md)
- [Lessons learned](docs/lessons.md)
- [ADRs](docs/adr/) — 16 records, latest are
  [ADR-0014 (routing LLM tie-breaker)](docs/adr/0014-routing-llm-tiebreaker.md),
  [ADR-0015 (staff console HTMX + Jinja2)](docs/adr/0015-staff-console-htmx-jinja2.md),
  and [ADR-0016 (recall via manualClose)](docs/adr/0016-recall-via-manual-close.md)
- [Bootstrap prompt](prompts/build-agora.md)

## License

Apache 2.0. See [LICENSE](LICENSE).
