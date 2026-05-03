# PRD 02 — Agents

> Last reviewed against code: 2026-05-03 (post tier-2 recall-proposed
> + tier-3 receipt-unconfirmed scanner emissions and FastAPI-lifespan
> wiring; CrossRef client landed PR-A, agent integration queued
> PR-B).

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
- CrossRef client (DOI → bibliographic identity). Implemented
  (`src/agora/clients/crossref.py`, PR-A) but not yet wired into
  `DiscoveryAgent.run`; PR-B fans out the agent to both clients
  with merge-rank.

**Tools (planned, not yet wired):**
- WorldCat sandbox (OCLC# → holdings), consortium-union SRU.

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

**Hard flags (today, advisory only):** `PolicyDecision.hard_flags`
exposes the subset of flags marked `is_hard=True` (e.g.
`patron_suspended`, `contu_violation`). The coordinator does **not**
auto-block on hard flags today — the saga has no hard-fail surface
and staff approval remains the override (consistent with ADR-0005
default-deny autonomy: agents recommend, staff commit). The
`/sagas/{id}/override` endpoint sketched for a hard-fail flow is
deferred — see PRD-05 for the revisit triggers.

**Future:** if a hard-fail flow lands, the coordinator would consult
`hard_flags` before opening the APPROVE gate, surface the flags in
the staff console, and require an explicit `/override` POST with a
typed reason that persists in the ledger.

## TransactionAgent

**Job:** Drive ReShare REST API to send/receive ISO 18626 messages.

**Inputs:** approved request + target state.

**Outputs:** ReShare-side request id, ISO 18626 message id, observed
state.

**Tools:** ReShare mod-rs REST client (`agora.clients.reshare`).

**Idempotency:** every call carries an outbox-generated key; ReShare
side dedups on its own request id.

## TrackingAgent

**Job:** Append advisory OBSERVATION events to the saga ledger when a
shipped saga crosses an interesting threshold. **No outbox writes,
no state changes, no auto-compensator dispatch** (ADR-0005). Staff
read the observation in the console and decide whether to escalate
(e.g. click `/sagas/{id}/compensate` to fire the SHIP recall).

**Inputs:** active saga rows where `current_state == SHIPPED`. The
SQL filter is the authoritative "patron has not yet confirmed
receipt" signal for tier-3.

**Outputs:** three OBSERVATION kinds, each with a deterministic
idempotency key so re-running the scan is safe (the saga ledger's
`UNIQUE(idempotency_key)` constraint absorbs duplicates and returns
the existing row):

| Tier | Key | Trigger | Threshold env var (default) |
| ---- | --- | ------- | --------------------------- |
| 1 — overdue            | `overdue-{saga_id}`            | `now > due_at` (loan-clock time)                                              | n/a                                                       |
| 2 — recall proposed    | `recall-proposed-{saga_id}`    | `days_overdue >= threshold` once tier-1 has fired                             | `AGORA_TRACKING_RECALL_AFTER_DAYS` (14)                   |
| 3 — receipt unconfirmed| `receipt-unconfirmed-{saga_id}`| `now - shipped_at >= threshold` AND saga still at `SHIPPED` (no RECEIVE yet)  | `AGORA_TRACKING_UNCONFIRMED_RECEIPT_AFTER_DAYS` (7)       |

Tier-3 keys off **transit time**, not loan-clock time, and fires
*independently* of tier-1/2 — a saga can be flagged
"patron forgot to confirm" while `due_at` is still in the future.
Tier-2 carries `suggested_action: "compensate_ship"` plus the
`reshare_id` for the staff console to render as a CTA pointing at
`POST /sagas/{id}/compensate`. Tier-3 carries no `suggested_action`
field — staff console surfaces it as a "chase patron" hint without
an in-saga CTA. Recorded `days_overdue` and `days_since_shipped` are
point-in-time snapshots; the UI computes "currently N days" from the
base timestamp + render clock.

**Implementation:**
- `TrackingAgent.observe(Observation)` — manual entry point for
  callers who want to push an ad-hoc observation.
- `OverdueScanner.scan()` — single sweep that pre-computes both
  metrics per saga then runs three independent tier blocks. See
  `src/agora/agents/tracking.py`.
- `OverdueScanner.run_forever()` — periodic loop calling `scan()`
  at `AGORA_TRACKING_SCAN_INTERVAL_SECS` (default 300s).

**Status:** scanner wired into the FastAPI lifespan as an
`asyncio.Task` named `agora.tracking.scanner` (see PR #19 for tier-1/2
+ lifespan task; PR #39 for tier-3). Disable with
`AGORA_TRACKING_SCANNER_ENABLED=0`. Multi-scanner safe by
construction: the three deterministic keys per saga collide on
`UNIQUE(saga_event.idempotency_key)`; concurrent scans are wasteful
but never incorrect. Auto-recall and a dedicated `RECALLING`
lifecycle state are explicit non-goals; staff still clicks.

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
