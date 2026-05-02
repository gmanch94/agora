# Build Agora — Project Bootstrap Prompt

> **Purpose:** Hand this prompt to a fresh Claude Code session to load full
> project context and resume work on the Agora ILL system.

## Identity

You are working on **Agora**, an agentic Inter-Library Loan (ILL) system.

Working directory: `C:\Users\giris\Documents\GitHub\agora`

## Mission

Build a research prototype of an agentic ILL system that:

1. Drives the standard ILL lifecycle **Submitted → Routed → Approved →
   Shipped → Returned** through a multi-agent orchestrator.
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

1. Read **`docs/prd/`** — product requirements, in numbered order.
2. Read **`docs/adr/`** — architecture decisions, in numbered order.
3. Read **`README.md`** — current build status + next milestone.
4. Check **`memory/project_agora_ill.md`** for the durable project context.

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

- ReShare sandbox boots via `docker compose up`, mod-rs reachable.
- Full happy-path lifecycle runs end-to-end against ReShare with
  human-approval mocks, persisted in saga ledger.
- Each state has a working compensator; chaos tests inject mid-saga
  failure and verify ledger ends in consistent terminal state.
- Idempotency: replay any inbound msg 3× → exactly one observable effect.
- Discovery agent can resolve OpenURL citation + return ranked supplier
  list from SRU.
- Staff console exposes pending approvals + reasoning traces.
- Architecture documented; decisions captured in ADRs.

## Out of scope (explicit)

- Implementing ISO 18626 wire protocol from scratch
- Z39.50 binary protocol (SRU only)
- FedRAMP control implementation (alignment notes only)
- Real money / billing
- Production deployment / GCP infra
- Patron-facing UI (staff console only)
