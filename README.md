# Agora — Agentic Inter-Library Loan (ILL) System

> Research prototype. Multi-library consortium. Agents over FOLIO/ReShare.
> Saga + idempotency. Human-approval at every state transition.

## What this is

Agora is a research prototype that puts a multi-agent orchestration layer
on top of standards-compliant ILL infrastructure. The lifecycle
**Submitted → Routed → Approved → Shipped → Returned** is driven by
agents that produce *recommendations*; humans approve every transition.
Every state change is recorded in an event-sourced saga ledger with
paired forward + compensator operations and ULID-keyed idempotency.

The standards plumbing (ISO 18626 wire protocol, NCIP, etc.) is
delegated to FOLIO's `mod-rs` / ReShare and `mod-ncip`. Agora does not
reimplement them.

## Status

**Bootstrap phase.** This repository contains the project plan, PRDs,
ADRs, and initial code scaffolding. End-to-end demo not yet runnable.

See `docs/prd/` for product requirements, `docs/adr/` for architecture
decisions, and `prompts/build-agora.md` to bootstrap a fresh dev session.

## Quick layout

```
agora/
├── prompts/             # Project bootstrap prompt
├── docs/
│   ├── prd/             # Product requirements
│   └── adr/             # Architecture decisions
├── src/agora/
│   ├── agents/          # Discovery, Routing, Policy, Transaction,
│   │                    #   Tracking, Reconciliation
│   ├── saga/            # Coordinator, ledger, steps, idempotency
│   ├── clients/         # ReShare, NCIP, SRU, OpenURL clients
│   ├── api/             # FastAPI staff console
│   ├── models/          # pydantic schemas (ISO 18626 subset)
│   ├── config.py
│   └── cli.py
├── tests/               # pytest + Hypothesis property tests
├── docker-compose.yml   # Postgres + (eventually) ReShare sandbox
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
| ISO 18626:2021 | Peer-to-peer ILL messaging | Delegated to ReShare `mod-rs` |
| NCIP / Z39.83 | Library ↔ ILS circulation | Delegated to FOLIO `mod-ncip` |
| SRU | Catalog discovery | Direct HTTP client (`agora.clients.sru`) |
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
- [ADRs](docs/adr/)
- [Bootstrap prompt](prompts/build-agora.md)

## License

Apache 2.0 (placeholder; verify before publishing).
