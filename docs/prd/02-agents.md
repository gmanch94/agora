# PRD 02 — Agents

> Last reviewed against code: 2026-05-04 (post PRs #89/#90 — NCIP item-barcode
> + override endpoint; PR-2b RoutingAgent LLM track shipped — real
> `AdkLlmTiebreaker` adapter via ADK `LlmAgent` + Gemini Flash +
> factory `get_llm_tiebreaker()` + four `AGORA_ROUTING_LLM_*` env vars
> + sibling `.github/workflows/routing-eval-floor.yml` CI gate).

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
  (`src/agora/clients/crossref.py`, PR-A) AND wired into
  `DiscoveryAgent.run` (PR-B). When the patron supplies a DOI and
  a CrossRef client is configured, the agent confirms identity via
  CrossRef and seeds the SRU search with the confirmed ISBN/ISSN
  (CrossRef-confirmed values take precedence over the request's
  own — patron typos for DOI-paste flows). CrossRef hiccups (404 /
  5xx / network) downgrade to diagnostics; SRU still runs against
  the request's own identifiers. Sequential pipeline — there is no
  candidate-list merge because CrossRef returns no holdings. See
  PRD-04 for the full flow.

**Tools (planned, not yet wired):**
- WorldCat: structural gap — no freely accessible union holdings API
  exists (OCLC v1 EOL'd Dec 2024; v2 requires paid subscription; open
  SRU targets carry bib-only MARCXML, no MARC 852). When SRU returns
  no holdings, `DiscoveryAgent` falls back to `AGORA_CONSORTIUM_MEMBERS`
  and synthesises `HolderCandidate(status="unverified_holdings")` per
  member (PR #100). Revisit if institutional OCLC access materialises.

**Failure modes:** zero holders → `DiscoveryRecommendation.diagnostics`
records `"zero holders matched; saga will be Unfilled"`; staff sees
the empty candidate list and can mark Unfilled.

## RoutingAgent

**Job:** Rank candidate suppliers from DiscoveryAgent against
consortium policy.

**Inputs:** candidate holders, request metadata, consortium policy table
(SLA tier, reciprocity, cost, lender load).

**Outputs:** ordered list of `(supplier_symbol, score, rationale)`.

**Tools (today):** rules-only deterministic weighted-sum scoring in
`src/agora/agents/routing.py` (consortium membership 0.5, discovery
`preferred_score` 0.2, holding status 0.2, proximity 0.1). The rules
pick is repeatable and offline-runnable; happy-path regression pinned
by `tests/test_agents.py::test_routing_picks_consortium_available_first`.

**Tools (seam shipped — PR-2a):** the LLM tie-breaker integration
point is in place but no LLM is wired yet. `RoutingAgent.__init__`
takes an optional `llm_tiebreaker: LlmTiebreaker | None = None`
kwarg; when configured, the agent calls `llm_tiebreaker.resolve()`
on near-ties (top-2 score gap ≤ ε, default 0.03 via
`AGORA_ROUTING_TIEBREAK_EPSILON`; tightened from 0.05 in #51 / #7c
so `routing-009` skips the LLM — rules already get it right).
`MockLlmTiebreaker` ships in the same module for tests. Failure paths (raise / abstain / unknown
symbol) all fall back to the rules pick + a diagnostic in the
rationale — `RoutingAgent` never re-raises out to its caller because
of the LLM (advisory-only invariant per ADR-0005). See
`tests/test_routing_tiebreaker.py` for the six-case behavioural
matrix.

**Tools (PR-2b shipped):** real `AdkLlmTiebreaker` in
`src/agora/agents/routing_llm_adk.py` implementing the Protocol from
PR-2a. Built on ADK `LlmAgent` + `InMemoryRunner` (ADR-0003), Gemini
Flash via Vertex AI by default (`AGORA_ROUTING_LLM_MODEL`),
`temperature=0` pinned, structured output via
`output_schema=TiebreakDecisionSchema` (defined in
`src/agora/agents/routing_tiebreak_prompt.py` — same module as the
prompt template, kept separate from the adapter so prompt-wording
diffs stand alone). Per-call timeout via `asyncio.wait_for` —
defaults to 30s (`AGORA_ROUTING_LLM_TIMEOUT_SECS`; bumped from 5s — Gemini 2.5 cold-start exceeds the old default). Lazy
`google.adk` import in `__init__` so a base install (no `[adk]`
extra) doesn't crash. Factory at
`agora.agents.factories.get_llm_tiebreaker()` returns `None`
unless `AGORA_ROUTING_LLM_ENABLED=1`. CI floor gate at
`.github/workflows/routing-eval-floor.yml` runs the harness in
`--rules-only --check-floor` mode (no GCP secrets in CI) — catches
rules-engine regressions; PR-review catches LLM-quality regressions.
PR-2b shipped (#51) hits the ADR-0014 ceiling: top-1 19/20 (0.9500),
mean Spearman 0.8889 against `gemini-2.5-flash`, by fixing the three
true-tie inversion scenarios (`routing-013`, `014`, `016`);
`routing-015` stays out-of-scope (rules score gap 0.46 — not a
tie).

**Tie-breaker, not replacement** — rules keep deciding the bulk;
LLM fires only on near-ties. See **ADR-0014** for the decision and
the gating policy.

**Eval harness:** `src/agora/evals/routing.py` runs any RoutingAgent
variant against the 20 hand-labeled scenarios in
`evals/routing/scenarios.json` and scores it on top-1 accuracy +
mean Spearman rank correlation. Invoke with `make eval-routing`.
Two committed baselines (split in #50): the rules-baseline floor
in `evals/routing/baseline-rules.json` (top-1 0.8000, mean Spearman
0.5556) is the regression guard the CI gate enforces, and
`evals/routing/baseline.json` is the LLM-augmented baseline
(top-1 0.9500, mean Spearman 0.8889 post #51) that PR-review reads
to catch LLM-quality regressions. Four scenarios
(`routing-013..016`) are deliberate rules-baseline misses encoding
metadata-only signals — those are exactly the cases the LLM is
hired to decide.

**Constraint:** rationale must be human-readable, ≤3 sentences. This
is what staff will see when approving. The LLM tie-breaker prompt
will enforce the same 3-sentence limit (ADR-0014); the rules
baseline already satisfies it.

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
