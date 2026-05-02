---
name: docs-stale-check
description: Walk docs/ and surface drift against current code. Use after any sizeable change to src/agora/saga/, src/agora/api/, src/agora/agents/, src/agora/models/, src/agora/config.py, or Makefile — or when the user asks "are the docs still accurate", "check for doc drift", "stale-check the PRDs", or before tagging a release. Outputs a punch list of file:line drift candidates rather than rewriting docs autonomously.
---

# docs-stale-check

The Agora docs (`docs/prd/`, `docs/adr/`, `docs/architecture.md`,
`docs/solution.md`, `docs/runbook.md`, `CLAUDE.md`) drift behind code.
We've already shipped one PRD-wide stale-check pass and one
architecture-diagram fix. This skill makes the next pass cheap by
listing every drift-prone surface and the exact files / symbols /
patterns to grep.

## When to invoke

- User says "stale-check docs", "check doc drift", "are PRDs current",
  "audit docs", "before release, verify docs"
- Right after a sizeable change to:
  - `src/agora/saga/` (coordinator, ledger, flows, outbox, idempotency)
  - `src/agora/api/app.py` (endpoints, lifespan, schemas)
  - `src/agora/agents/` (agent contracts)
  - `src/agora/models/lifecycle.py` (state / step enums)
  - `src/agora/config.py` (env-var names + defaults)
  - `Makefile` (dev targets)
  - `alembic/versions/` (schema changes)

## Output discipline

This skill **produces a punch list** — one entry per drift candidate
with file:line. It does NOT rewrite docs autonomously. After the
list is in front of the user, ask if they want a follow-up pass to
revise the doc(s).

Each entry is one line:

```
docs/<file>:<line>  <category>  <claim in doc>  →  <reality in code>
```

Categories: `state`, `step`, `endpoint`, `adr-ref`, `make-target`,
`module`, `env-var`, `schema-field`, `agent-contract`,
`compensator-target`, `freshness-header`.

Group entries by file. End with a one-line count summary
(`N drift candidates across M files`).

## Drift surfaces (run all of these)

### 1. Lifecycle states & steps

**Source of truth:** `src/agora/models/lifecycle.py` —
`LifecycleState`, `StepName`, `TERMINAL_STATES`.

For each PRD/ADR/runbook/SDD/architecture file, grep for capitalised
state nouns (`Submitted`, `Routed`, `Approved`, `Shipped`,
`Returned`, `Cancelled`, `Unfilled`, `Disputed`) and any others
mentioned (e.g. fictional `Recalled`, `Reconciled`). Anything not
in the enum is drift.

**Past hits:** PRD-01 referenced `Recalled` (not a state); PRD-01
"Reconciled" terminal in ASCII; architecture.md state machine had
both.

### 2. Compensator state targets

**Source of truth:** `src/agora/saga/flows.py` — each compensator's
`state_after`.

Grep for "compensator" tables in PRD-01, architecture.md, SDD. Each
forward step's compensator must list the same `state_after` as the
code. Today:

| Forward | Compensator state_after |
| ------- | ----------------------- |
| Submit  | Cancelled               |
| Route   | Submitted               |
| Approve | Cancelled               |
| Ship    | Disputed                |
| Return  | Disputed                |

### 3. API endpoints

**Source of truth:** `src/agora/api/app.py` — all `@app.<verb>` and
`router.<verb>` decorators.

Grep all docs for `/sagas`, `/requests`, `/health` mentions. Verify:
- No `/api` prefix (we don't use one)
- No `/override` (not implemented)
- Verbs match (POST vs GET)

**Past hit:** PRD-05 had `/api/sagas/...` and `/override`.

### 4. ADR references

**Source of truth:** `docs/adr/` — exactly the files that exist.

Grep every `docs/**/*.md` for `\b0\d{3}-` patterns. Each must map to
an existing file. Also verify topic labels (e.g. `0007` is FedRAMP,
`0008` is ULID).

**Past hit:** PRD-06 referenced ADR-0008 for FedRAMP; actually 0007.
A pre-commit hook (`.claude/hooks/check_adr_refs.py`) catches this
on edit; this skill catches anything that already slipped in.

### 5. `make` targets

**Source of truth:** `Makefile` (the `.PHONY:` line lists all
targets).

Grep docs for `make <something>`. Each must exist in the Makefile.
Targets that exist but reference missing modules (e.g. `make chaos`
→ `python -m agora.demos.chaos` where the module is absent) count
as drift too.

**Past hit:** PRD-00 success criteria referenced `make chaos` while
the chaos demo module is unimplemented.

### 6. Module references

**Source of truth:** `src/agora/`.

Grep docs for `agora.demos.<X>`, `agora.<subpkg>.<module>` patterns.
Verify each module exists. (Use Glob + `__init__.py` presence.)

### 7. Environment variables

**Source of truth:** `src/agora/config.py` — every `Field(..., alias=...)`.

Grep docs for `AGORA_*`, `RESHARE_*`, `NCIP_*`, `SRU_*`, `OUTBOX_*`,
`SAGA_*` patterns. Each must have a matching alias in `Settings`.
Also reverse-check: if `runbook.md` has an env-var table, every
`Settings` field should have a row.

**Past hit:** none yet, but high churn risk now that
`AGORA_OUTBOX_WORKER_ENABLED` and `AGORA_OUTBOX_POLL_INTERVAL_SECS`
exist.

### 8. Schema fields (DB + pydantic)

**Source of truth:** `src/agora/saga/db.py` (ORM); `src/agora/models/`
(pydantic).

Grep docs for column names in inline SQL blocks. Every column listed
must exist in the ORM model. Specifically check:
- `saga_event` columns (id, saga_id, seq, kind, step, state_before,
  state_after, actor, idempotency_key, iso_message_id, payload,
  outcome, rationale, ts)
- `outbox` columns (id, saga_id, target, idempotency_key, payload,
  status, attempts, last_error, scheduled_for, delivered_at)
- `inbox` columns (message_id, source, received_at, response)

**Past hit:** PRD-03 had simplified outbox schema missing
`attempts`, `last_error`, `scheduled_for`, `dead_letter` status.

### 9. Agent contracts

**Source of truth:** `src/agora/agents/<name>.py`.

For each agent claim in PRD-02 (Inputs / Outputs / Tools):
- DiscoveryAgent: SRU-only today; CrossRef/WorldCat planned
- RoutingAgent: rules-based; no LLM call yet
- PolicyAgent: in-process rule tables
- TransactionAgent: wraps `ReShareClient`
- TrackingAgent + OverdueScanner: deterministic `overdue-{saga_id}`
  key; cron not wired
- ReconciliationAgent: thin wrapper over `Coordinator.run_compensator`,
  not a direct ledger writer

**Past hit:** PRD-02 had all of: WorldCat sandbox lookup,
Reconciliation as ledger-writer, TrackingAgent without
OverdueScanner. (Fixed in PR #6 but high recurrence risk.)

### 10. Freshness headers

**Source of truth:** doc convention started in PR #6 + #7.

Every revised PRD / architecture / runbook / SDD has a line
`> Last reviewed against code: YYYY-MM-DD` near the top. List any
file in `docs/` that lacks this header so we can add it on the next
revision pass.

## How to run

1. Read the source-of-truth files first (`lifecycle.py`, `flows.py`,
   `app.py`, `config.py`, `Makefile`, `db.py`).
2. For each surface above, grep / glob the docs and diff claims
   against truth.
3. Build the punch list as you go (one line per finding).
4. Output the list, end with the count summary, and ask: "Want me
   to revise the affected docs in a follow-up pass on a fresh
   branch?"

## Out of scope

- Rewriting docs (deliberately separate step — drift fixes are PRs)
- Validating the actual code (use the test suite + ruff + mypy)
- ISO 18626 XSD validation (that's `iso18626-validate`)
- ReShare endpoint correctness (`reshare-probe`)

## Pair tools

- Companion hook `.claude/hooks/check_adr_refs.py` runs on every
  Write/Edit to `docs/**/*.md` and catches new ADR-ref drift before
  it lands.
- `.claude/skills/lifecycle-extend/SKILL.md` writes lifecycle
  extensions in lockstep across docs + code so this skill has less
  drift to find later.
