# Build Agora — Project Bootstrap Prompt

> **Purpose:** Hand this prompt to a fresh Claude Code session to load full
> project context and resume work on the Agora ILL system.

## Identity

You are working on **Agora**, an agentic Inter-Library Loan (ILL) system.

Working directory: `C:\Users\giris\Documents\GitHub\agora`

## Mission

Build a research prototype of an agentic ILL system that:

1. Drives the standard ILL lifecycle **Submitted → Routed → Approved →
   Shipped → Received → Returned** through a multi-agent orchestrator.
2. Wraps **FOLIO `mod-rs`** (ISO 18626) and **FOLIO `mod-ncip`** (Z39.83)
   for all standards-compliant wire protocols — **never reimplement the
   wire formats**.
3. Persists every state transition in an **event-sourced saga ledger**
   with paired forward + compensator operations.
4. Treats **idempotency as a first-class concern** — every external
   message has a ULID, every inbound webhook is dedup'd via inbox table,
   every outbound delivery uses outbox pattern.
5. Keeps **humans in the loop on every state transition** — agents
   produce recommendations + reasoning traces; staff click approve.

## Constraints

- **Research prototype scope** — no FedRAMP authorization, no production
  deployment, no patron payment, no multi-region, no HA. FedRAMP
  alignment is *documented* in ADRs, not implemented.
- **Multi-library consortium tenancy** — design for N libraries sharing
  the same Agora instance, routing among themselves and to external peers.
- **Stack:** Python 3.11+, Google ADK for agent orchestration, FastAPI
  for HTTP, Postgres for saga ledger, Docker Compose for ReShare sandbox.
- **No code generation that reimplements ISO 18626 XML or NCIP messages.**
  Always go through the FOLIO module's REST API.

## Where to start

1. Read **`CLAUDE.md`** — project rules + known gaps + behavioural expectations.
2. Read **`README.md`** — current build status + next milestone.
3. Read **`docs/prd/`** — product requirements, in numbered order.
4. Read **`docs/adr/`** — architecture decisions (14 records through 0014).
5. Read **`docs/lessons.md`** for accumulated gotchas before re-deriving them.
6. Read **`docs/runbook.md`** for env vars, endpoint surface, and the
   gate-workflow walk-through.

## Behavior expectations

- Every meaningful design choice → new ADR (`docs/adr/NNNN-decision.md`).
- Every code change should be reviewable in isolation; prefer small,
  composable modules over monoliths.
- Default to writing tests alongside features; saga + idempotency code
  must have property-based tests.
- Be terse in user-facing output. Verbose in code comments **only when
  the why is non-obvious**; otherwise let names speak.
- When in doubt about scope, re-read the PRDs before guessing.

## Definition of "done" for the prototype

- ✅ Full happy-path lifecycle runs end-to-end via `make demo`
  (`agora.demos.happy_path`), persisted in the saga ledger.
- ✅ Each state has a working compensator; saga + idempotency covered
  by property-based tests (`tests/test_property_saga.py`).
- ✅ Idempotency: replay any inbound msg 3× → exactly one observable
  effect (UNIQUE constraint on `saga_event.idempotency_key`).
- ✅ Discovery agent resolves DOI via CrossRef + holdings via SRU, with
  fallback diagnostics; `POST /sagas/{id}/discover` exposes it.
- ✅ Staff console (FastAPI) exposes pending approvals + reasoning
  traces — UI front-end deferred (PRD-05 explicit non-goal for the
  prototype).
- ✅ Architecture documented; 14 ADRs (0001-0014) capture the
  architectural commitments; 7 PRDs cover product scope.
- ⏳ ReShare sandbox boots via `docker compose up` against a real
  mod-rs tenant — Docker compose ships Postgres-only today; live
  mod-rs probing blocked on sandbox access (backlog #9 PR-C / PR-D).

## Out of scope (explicit)

- Implementing ISO 18626 wire protocol from scratch
- Z39.50 binary protocol (SRU only)
- FedRAMP control implementation (alignment notes only)
- Real money / billing
- Production deployment / GCP infra
- Patron-facing UI (staff console only)
