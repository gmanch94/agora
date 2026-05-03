# RoutingAgent eval set

Hand-labelled benchmark for the consortium-supplier ranking task.
See **ADR-0014** (`docs/adr/0014-routing-llm-tiebreaker.md`) for the
decision context, the gating policy, and the rules-baseline floor
that future PRs must beat.

## Files

| File | What |
| ---- | ---- |
| `scenarios.json` | 20 routing situations with `expected_chosen` + `expected_ranking` ground truth. Authored by hand against PRD-02 routing semantics. |
| `baseline.json`  | Committed rules-baseline scores. **Floor** for any PR that touches `RoutingAgent.run`. PR-2 (LLM tie-breaker) must meet or exceed both metrics. |

## Run it

```bash
make eval-routing                                 # via Makefile
.venv/Scripts/python.exe -m agora.evals.routing   # direct
.venv/Scripts/python.exe -m agora.evals.routing --no-write   # print only
```

The CLI prints a per-scenario summary and rewrites `baseline.json`.

## Metrics

- **`top1_accuracy`** — fraction of scenarios where the agent's
  `chosen.symbol` equals `expected_chosen`. Binary per scenario, mean
  across the set. The signal staff sees in the console.
- **`mean_spearman`** — mean Spearman rank correlation between the
  agent's `ranked` list and `expected_ranking`. Tie-tolerant; captures
  full-ordering quality (matters when staff overrides pick #1 and
  walks the list). Skips scenarios with <2 candidates.

NDCG was considered and rejected — overkill for a 20-scenario
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

Four scenarios (`routing-013` through `routing-016`) are deliberate
**rules-baseline misses** — the `notes` field flags this. The LLM
tie-breaker (PR-2) is expected to fix them by reading `raw` metadata
the rules don't see.
