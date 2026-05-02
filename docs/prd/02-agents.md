# PRD 02 — Agents

All agents are **advisory** in the prototype: they emit a recommendation
+ reasoning trace into the staff console. Staff commit by clicking
approve, which fires the actual forward step.

## DiscoveryAgent

**Job:** Resolve a citation/OpenURL context object into a candidate
item + holder list.

**Inputs:** OpenURL ContextObject, free-text citation, or ISBN/OCLC#.

**Outputs:** `{ item_metadata, candidate_holders: [{symbol, holdings_status, distance, preferred}] }`

**Tools:**
- SRU client (LoC, target consortium union catalog)
- OpenURL parser
- WorldCat sandbox lookup (if available)

**Failure modes:** ambiguous citation → flag for staff disambiguation;
no holders found → terminate as `Unfilled`.

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

**Job:** Watch shipped loans for due-date events, recalls, supplier
status updates.

**Inputs:** active saga rows in `Shipped` state.

**Outputs:** events appended to saga ledger; triggers compensators or
forward steps as appropriate.

**Tools:** ReShare webhook receiver, scheduled poll for due dates.

## ReconciliationAgent

**Job:** Run compensators when forward steps fail or staff requests
rollback.

**Inputs:** failed step row from saga ledger, reason.

**Outputs:** compensator execution result; updates saga ledger with
`compensated_at` and outcome.

**Tools:** ReShare cancel/recall APIs, NCIP discharge, internal saga
ledger writes.

**Critical:** never run compensator unless paired forward step has
`outcome=committed` in ledger. Replay-safe via idempotency key.

## Coordination

Agents do **not** call each other directly. The orchestrator (single
saga coordinator service) reads the saga ledger, decides which agent
to invoke for the next step, and writes the agent's recommendation
back. Staff approval triggers the next forward step. This keeps the
control flow auditable and replayable.
