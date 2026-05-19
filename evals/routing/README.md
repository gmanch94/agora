# RoutingAgent eval set

Hand-labelled benchmark for the consortium-supplier ranking task.
See **ADR-0014** (`docs/adr/0014-routing-llm-tiebreaker.md`) for the
decision context, the gating policy, and the rules-baseline floor
that future PRs must beat.

## Files

| File | What |
| ---- | ---- |
| `scenarios.json` | 48 routing situations with `expected_chosen` + `expected_ranking` ground truth. Authored by hand against PRD-02 routing semantics. |
| `baseline-rules.json` | Committed **rules-only** scores (top-1 0.9375 / mean Spearman 0.8667, 48 scenarios). The CI floor at `.github/workflows/routing-eval-floor.yml` enforces these — any PR that drops below either metric on the rules path fails. |
| `baseline.json`  | Committed **LLM-augmented** scores. After this PR: placeholder rules-only numbers (0.9375/0.8667 over 48 scenarios); refresh with `make eval-routing --llm` (requires GCP ADC) to get the LLM-augmented baseline. Prior LLM baseline (40 scenarios): top-1 0.9500 / mean Spearman 0.8889 against `gemini-2.5-flash`. |

## Run it

```bash
make eval-routing                                            # rules-only via Makefile
.venv/Scripts/python.exe -m agora.evals.routing              # direct, rules-only
.venv/Scripts/python.exe -m agora.evals.routing --no-write   # print only
.venv/Scripts/python.exe -m agora.evals.routing --llm        # LLM-augmented (rewrites baseline.json)
.venv/Scripts/python.exe -m agora.evals.routing --rules-only --check-floor   # what CI runs
```

`--llm` requires bound GCP ADC + `aiplatform.googleapis.com` enabled
on the project + Vertex AI Studio click-through enablement, plus the
correct API model id (the Studio display label is **not** the API
id; e.g. Studio shows "gemini-3.1-flash-lite-preview" but the public
API takes `gemini-2.5-flash`). Rerun with
`AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash AGORA_ROUTING_LLM_TIMEOUT_SECS=30`
— the config default `gemini-2.0-flash` 404s under current Vertex
enablement, and the default 5s timeout can be too tight for cold
start.

The CLI prints a per-scenario summary and rewrites whichever baseline
matches the run mode (`baseline-rules.json` for `--rules-only`,
`baseline.json` for `--llm`).

## Metrics

- **`top1_accuracy`** — fraction of scenarios where the agent's
  `chosen.symbol` equals `expected_chosen`. Binary per scenario, mean
  across the set. The signal staff sees in the console.
- **`mean_spearman`** — mean Spearman rank correlation between the
  agent's `ranked` list and `expected_ranking`. Tie-tolerant; captures
  full-ordering quality (matters when staff overrides pick #1 and
  walks the list). Skips scenarios with <2 candidates.

NDCG was considered and rejected — overkill for a 40-scenario
prototype set; graded relevance labels would require a second
labeller pass.

## Editing scenarios

Adding or relabelling a scenario MUST be paired with re-running the
harness and re-committing `baseline.json`. The PR description MUST
explain why the new baseline is acceptable. ADR-0014 spells out the
gating policy.

Each scenario has:

```json
{
  "id": "routing-NNN",
  "description": "one-sentence summary",
  "candidates": [
    { "symbol": "MEM-A", "is_consortium_member": true,
      "status": "available", "preferred_score": 0.5,
      "distance_km": 200.0, "raw": { ... } }
  ],
  "expected_chosen": "MEM-A",
  "expected_ranking": ["MEM-A", "..."],
  "notes": "why this answer; flag if rules baseline is wrong"
}
```

`expected_ranking` MUST be a permutation of the candidate symbols —
the harness fails loudly on mismatch. `expected_chosen` MUST be
either `null` (empty candidate list) or a member of `candidates`.

Three scenarios (`routing-013`, `routing-014`, `routing-016`) are deliberate
**rules-baseline misses** — the `notes` field flags this. The LLM
tie-breaker (PR-2, shipped #48-#51) reads `raw` metadata the rules don't see;
post #51 prompt + ε tuning all three are fixed by the LLM:

| Scenario | Rules gap | LLM picks | Status |
|---|---|---|---|
| `routing-013` | 0.00 (true tie) | correct | ✅ fixed by LLM |
| `routing-014` | 0.00 (true tie) | correct | ✅ fixed by LLM |
| `routing-015` | 0.46 (not a tie) | n/a — LLM doesn't fire below ε=0.03 | ✅ fixed by rules (format-affinity PR) |
| `routing-016` | 0.00 (true tie) | correct | ✅ fixed by LLM |

The 20 new scenarios (`routing-021` through `routing-040`, this PR) are all
**rules-correct** — they exercise coverage gaps in the existing set
(large consortia, boundary distances, preferred_score extremes, status
hierarchy isolation, cross-term interactions) without introducing new
LLM-only scenarios. Rules top-1 improves from 0.85 (17/20) to 0.9250 (37/40).

`baseline.json` (LLM-augmented) needs refreshing over the full 40-scenario set:
```
AGORA_ROUTING_LLM_ENABLED=1 AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash \
AGORA_ROUTING_LLM_TIMEOUT_SECS=30 \
.venv/Scripts/python.exe -m agora.evals.routing --llm
```
