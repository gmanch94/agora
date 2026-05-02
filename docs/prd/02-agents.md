# PRD 02 — Agents

> Last reviewed against code: 2026-05-02.

All agents are **advisory** in the prototype: they emit a recommendation
+ reasoning trace into the staff console. Staff commit by clicking
approve, which fires the actual forward step.

## DiscoveryAgent

**Job:** Resolve a citation/OpenURL context object into a candidate
item + holder list.

**Inputs:** OpenURL ContextObject, free-text citation, or ISBN/OCLC#.

**Outputs:** `{ item_metadata, candidate_holders: [{symbol, holdings_status, distance, preferred}] }`

**Tools (today):**
- SRU client (LoC). Implemented (`src/agora/clients/sru.py`).
- OpenURL parser. Implemented (`src/agora/clients/openurl.py`, KEV only).

**Tools (planned, not yet wired):**
- WorldCat sandbox lookup, DOI → CrossRef, OCLC# → WorldCat.

**Failure modes:** zero holders → `DiscoveryRecommendation.diagnostics`
records `"zero holders matched; saga will be Unfilled"`; staff sees
the empty candidate list and can mark Unfilled.

## RoutingAgent

**Job:** Rank candidate suppliers from DiscoveryAgent against
consortium policy.

**Inputs:** candidate holders, request metadata, consortium policy table
(SLA tier, reciprocity, cost, lender load).

**Outputs:** ordered list of `(supplier_symbol, score, rationale)`.

**Tools:** Postgres consortium policy reads, LLM reasoning over a
templated prompt for tie-breaking.

**Constraint:** rationale must be human-readable, ≤3 sentences. This
is what staff will see when approving.

## PolicyAgent

**Job:** Pre-flight legal & budget checks before approval.

**Inputs:** request, patron history, copyright ledger.

**Outputs:** `{ pass: bool, flags: [...], rationale }`. Flags include
CONTU rule-of-5 violation, patron eligibility, budget cap, embargoed
material.

**Tools:** Postgres rule tables, copyright ledger queries.

**Hard rules:** if any hard flag (copyright violation, patron suspended),
agent returns `pass: false` and forward step is blocked even if staff
clicks approve. Staff can override only with manual reason.

## TransactionAgent

**Job:** Drive ReShare REST API to send/receive ISO 18626 messages.

**Inputs:** approved request + target state.

**Outputs:** ReShare-side request id, ISO 18626 message id, observed
state.

**Tools:** ReShare mod-rs REST client (`agora.clients.reshare`).

**Idempotency:** every call carries an outbox-generated key; ReShare
side dedups on its own request id.

## TrackingAgent

**Job:** Append observations to the saga ledger (e.g. due-date set,
overdue warning) without changing lifecycle state.

**Inputs:** active saga rows in `Shipped` state.

**Outputs:** OBSERVATION events appended via
`Coordinator.record_observation`. Lifecycle stays put; staff decides
whether to escalate.

**Implementation:**
- `TrackingAgent.observe(Observation)` — manual entry point for
  callers who want to push an observation.
- `OverdueScanner.scan()` — periodic sweep over sagas in `shipped`
  whose `due_at` (stamped onto the SHIP forward payload) has passed.
  Records one OBSERVATION per saga with deterministic
  idempotency key `f"overdue-{saga_id}"`; the saga ledger's UNIQUE
  constraint absorbs duplicates so re-running the scan is safe.

**Status:** core scanner implemented (`src/agora/agents/tracking.py`).
**Cron / lifespan loop not yet wired** — production deployment needs
a second `asyncio.Task` in the FastAPI lifespan that calls `scan()`
on a schedule (mirror the outbox-worker pattern). Tracked in
`CLAUDE.md` known-gaps.

## ReconciliationAgent

**Job:** Trigger paired compensators on demand.

**Implementation today:** thin wrapper around
`Coordinator.run_compensator` (`src/agora/agents/reconciliation.py`).
The agent itself does **not** write to the saga ledger or call
ReShare — the coordinator does both. This keeps the human-in-loop
invariant clean: any compensator firing is a deliberate call, not an
agent autopilot.

**Critical (enforced by the coordinator):** the compensator runs
only after `SagaLedger.find_committed_forward(step)` returns a
committed forward event; otherwise `CoordinatorError` (mapped to
**409** by the API). Replay-safe via the idempotency key on the
COMPENSATOR ledger row.

**Status:** thin happy-path wrapper exists. Lacks a
failure-classification policy (which compensator to fire when, e.g.
on outbox dead-letter vs staff request). Future ADR.

## Coordination

Agents do **not** call each other directly. The orchestrator (single
saga coordinator service) reads the saga ledger, decides which agent
to invoke for the next step, and writes the agent's recommendation
back. Staff approval triggers the next forward step. This keeps the
control flow auditable and replayable.
