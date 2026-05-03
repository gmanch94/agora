# ADR 0014 — RoutingAgent LLM-augmented tie-breaker

**Status:** Accepted
**Date:** 2026-05-03 (revised same day post PR-2a seam landing —
implementation note split into PR-2a / PR-2b; `routing-015` scope
finding documented)

## Context

`RoutingAgent` today uses a deterministic weighted-sum scoring
function over four signals — consortium membership (0.5),
discovery-emitted `preferred_score` (0.2), holding status (0.2), and
proximity (0.1) — and picks the top-scoring candidate. The full code
is `src/agora/agents/routing.py`; today's behaviour is pinned by a
single happy-path test
(`tests/test_agents.py::test_routing_picks_consortium_available_first`).

This worked while every signal staff cared about was already in the
feature set. Three real-world signals are not, and they all show up
once you start hand-labelling routing decisions:

1. **Consortium SLA tier (`raw.sla_tier`).** Two tied consortium
   members where one ships in 24h and the other in 5 days — staff
   always picks the 24h one. Rules can't read `raw.sla_tier`.
2. **Reciprocity balance (`raw.reciprocity_balance`).** Routing the
   same lender repeatedly accumulates a debt the consortium contract
   eventually rebalances. Staff distributes load away from
   in-debt members. Rules don't track lend history.
3. **Format / delivery affinity (`raw.holds_format`,
   `raw.delivery`).** An article request requesting electronic
   delivery should prefer a digital holder over a print-only one,
   even when that means going outside the consortium. Rules don't
   read format affinity.
4. **Historical reliability (`raw.on_time_rate`).** Two tied
   consortium members where one fulfils 95% on time and the other
   60% — staff picks the reliable one. Rules don't read history.

Out of the 20 hand-labelled scenarios in `evals/routing/scenarios.json`,
four (`routing-013` through `routing-016`) encode exactly these
disagreements. The committed rules baseline misses all four:

```
top-1 accuracy:   0.8000  (16 / 20)
mean Spearman:    0.5556  (18 contributing scenarios; 14 at +1.0, 4 at -1.0)
```

These numbers are the floor that any future LLM-augmented routing PR
has to beat. They live in `evals/routing/baseline.json` so a
regression shows up in the diff.

## Decision

Add an LLM-augmented **tie-breaker** to `RoutingAgent`, **not** a full
replacement.

The rules baseline keeps making the deterministic pick for the bulk
of scenarios; the LLM only fires when the top-2 candidates are within
an ε of each other (ε threshold to be tuned in PR-2 against the eval
set). PRD-02 RoutingAgent already commits to "LLM reasoning over a
templated prompt for tie-breaking" — this ADR aligns with that
constraint rather than expanding scope.

The LLM call sees the candidate list (including each candidate's
`raw` metadata) plus the request item, and emits a single
`(chosen_symbol, rationale)` pair. The prompt enforces a hard
constraint of "rationale ≤3 sentences" matching the existing PRD-02
contract.

**Eval-gated rollout.** PR-2 (the prompt + LLM call wiring) is
mergeable only if its eval scores meet **both**:

- `top1_accuracy >= 0.8000` (i.e. matches the rules baseline floor)
- `mean_spearman >= 0.5556`

PR-2 will plausibly exceed both because the eval set is loaded with
exactly the cases the LLM was hired to decide. If it doesn't, that's
information — the prompt or the metric needs work before merging.

**Determinism for eval.** All eval invocations use `temperature=0`
and single-run scoring. If a future config wants `temperature>0`,
the eval invocation MUST switch to multi-run median scoring (median
across N=5 runs) to keep the floor meaningful. PR-2 inherits this
constraint.

**LLM provider.** Deferred to PR-2 prompt-build. Whatever provider
is chosen, the call is mediated through ADK (ADR-0003).

**Fallback.** LLM call unavailable, timeout, or rate-limited →
`RoutingAgent` returns the rules-baseline result and a diagnostic.
The agent does **not** raise; routing must always produce *some*
ordering for staff to review (advisory-only — ADR-0005).

**Invariants pinned (must hold across all PRs in this track).**

- `RoutingAgent.run` async signature unchanged: `(candidates) -> RoutingRecommendation`.
- `rationale` ≤3 sentences (PRD-02).
- Advisory-only — agent never writes to the saga ledger; coordinator
  decides when to invoke it (ADR-0005).
- Empty `candidates` → `chosen=None`, empty `ranked`, no LLM call.

## Consequences

**Positive**

- **Bounded improvement gate.** PR-2/3 can't merge without
  numerically beating the floor written in this ADR. Vaporware
  blocked.
- **Tie-breaker scope keeps the rules path deterministic** for the
  large majority of cases — replay-safe routing recommendations stay
  cheap and offline-runnable.
- **Eval set + harness is reusable** across future agent intelligence
  experiments (RoutingAgent v2, DiscoveryAgent ranking, etc.). The
  scenario format is JSON-only and framework-agnostic.
- **Rationale chain stays auditable.** When the LLM fires, its
  rationale is recorded in the saga ledger as part of the routing
  recommendation; when it doesn't, the rules-baseline rationale is
  recorded. Either way staff can see why pick #1 was pick #1.

**Negative**

- **20 hand-labelled scenarios from one author is brittle.** The
  ground-truth in `routing-013..016` is an opinionated read of
  consortium policy, not a real workflow trace. Upgrade path: when
  the ReShare sandbox produces real traces, replay them and re-label
  via a second annotator pass. Until then, pin the brittleness as a
  known limitation.
- **LLM cost amortises only on near-ties.** Worst case (every
  scenario near-ε) the agent calls the LLM on every routing
  recommendation. Mitigation: tune ε in PR-2 against the eval set
  to keep call volume low; instrument the agent so we can read the
  hit-rate in production.
- **Rules baseline can drift if scenarios change.** If a future PR
  adds scenarios that happen to invert rules picks, the floor
  collapses without a PR explicitly relaxing it. Mitigation: changing
  `scenarios.json` MUST be paired with a re-run + re-commit of
  `baseline.json`, and the PR description MUST explain why the new
  baseline is acceptable. The eval-gating CI check (PR-2) will catch
  silent regressions.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-----------------|
| Full LLM replacement (LLM picks every saga, no rules baseline) | Non-determinism risk on the bulk of cases where rules already get it right. Harder to falsify against rules. PRD-02 already commits to tie-breaker scope. |
| Hand-tune the rules' weights against the eval set | Still misses the 4 scenarios where the relevant signal lives in `raw` metadata that the score function doesn't read. Adding new features (`raw.sla_tier` etc.) into the score function would work for these cases but combinatorially explodes for future signals — the LLM amortises that. |
| Per-consortium policy rules engine | Combinatorial. Each consortium's policy adds a new rule table and ranking. The LLM tie-breaker reads the consortium policy from the prompt context and amortises this. |
| Skip the LLM entirely; mark routing as "good enough" | Would silently leave the 4 known-bad scenarios unaddressed. Eval set is exactly the artifact we'd want if we ever did chase this — building it once, gating once, costs ≤1 session. |

## Implementation note

The original sketch had PR-2 ship the seam + the prompt + the ADK
adapter + the eval rerun + the CI gate as one PR. That bundle has
since been **split into PR-2a (seam) and PR-2b (LLM)** for three
concrete reasons:

1. The eval rerun requires actually calling a real LLM. Without that
   call the baseline doesn't move and the gate is unchanged — so
   "seam + adapter without eval rerun" is functionally equivalent to
   shipping just the seam.
2. The real ADK call needs a credential + quota project (the GCP
   warning at session bootstrap flags the quota project as missing).
   That's a config problem, not a code problem, and it shouldn't
   block a code-only PR.
3. Prompt design is the hard part of this work and benefits from
   being its own PR with its own iteration cycle. Splitting lets
   PR-2a stand on a fully-mocked test surface while PR-2b iterates
   against real LLM outputs.

### PR-1 (shipped) — eval scaffolding

`evals/routing/{scenarios,baseline}.json`,
`src/agora/evals/routing.py`, this ADR. Rules-baseline floor pinned;
no change to `RoutingAgent`.

### PR-2a (this PR) — pluggable seam

- `LlmTiebreaker` Protocol + `TiebreakDecision` dataclass +
  `MockLlmTiebreaker` test double in `src/agora/agents/routing.py`.
- `RoutingAgent.__init__` gains optional `llm_tiebreaker=` kwarg and
  optional `epsilon=` override; `run` gains optional `item=` kwarg
  for patron-side metadata.
- ε exposed via `Settings.routing_tiebreak_epsilon` /
  `AGORA_ROUTING_TIEBREAK_EPSILON` (default 0.05).
- Six-case test matrix in `tests/test_routing_tiebreaker.py`:
  rules-only path / wide-gap-no-call / within-ε-call /
  exception-fallback / unknown-symbol-fallback / abstain-fallback.
- **No change to `evals/routing/baseline.json`** — the rules
  baseline floor is what PR-2b has to beat, so we hold it byte-stable
  through PR-2a. The eval harness produces an unchanged report.
- **No prompt template, no real LLM adapter, no CI gate change.**

### PR-2b (next) — prompt + ADK adapter + eval rerun + CI gate

- `AdkLlmTiebreaker` adapter (or whatever the eventual concrete
  implementation is named) implementing the Protocol shipped in
  PR-2a. Determinism pin: `temperature=0`, single-run scoring.
- Prompt template that takes `list[HolderCandidate]` + optional
  `ItemMetadata` and returns a `TiebreakDecision`-shaped JSON
  response constrained to a candidate symbol or null. The prompt
  enforces ≤1-sentence rationale (so the agent's composed rationale
  stays ≤3 sentences total — see PRD-02).
- Eval rerun against the LLM-augmented agent; commit the new
  `baseline.json` if it beats the floor.
- CI hook running `python -m agora.evals.routing --no-write` and
  diffing scores vs `baseline.json`. Regression = red CI.

### Scope finding from PR-2a: scenario `routing-015` is out-of-scope

The advisor's discriminator-constraint check (computed at PR-2a
implementation time) revealed that `routing-015` has a rules-baseline
top-2 score gap of **0.46**. The LLM tie-breaker, by design, only
fires when the gap is below ε. With ε=0.05 (or any sane near-tie
threshold) the LLM will never see `routing-015`, so PR-2b cannot
fix it via the tie-breaker mechanism even with a perfect prompt.

The other three inversion scenarios (`routing-013`, `014`, `016`)
have score gap 0.0 — true ties — and remain in scope. PR-2b is
expected to lift top-1 from 16/20 (0.8000) to 19/20 (0.9500) by
fixing those three; `routing-015` stays a baseline miss.

Two paths forward, both deferred:

- (A) Relabel `routing-015` so its expected pick becomes the rules
  pick (MEM-A), reducing the inversion set to three scenarios. Honest
  but loses signal.
- (B) Extend RoutingAgent scope to support an "always-on" advisory
  call when the request item carries delivery-format affinity that
  rules can't read. Bigger ADR; revisit once PR-2b has shipped and
  there's data on whether real consortium routing benefits from the
  format-affinity case.

Neither is in PR-2a or PR-2b's scope. The case stays in
`scenarios.json` as documentation that the metric is honest about a
known limitation.

### Harness is not part of `triple-gate` CI yet

`make eval-routing` writes `evals/routing/baseline.json` locally;
PR-2b will add the regression check at the same time as the new
baseline.
