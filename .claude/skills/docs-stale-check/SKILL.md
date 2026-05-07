---
name: docs-stale-check
description: Walk every doc surface (README, CLAUDE.md, docs/prd/, docs/adr/, docs/architecture.md, docs/runbook.md, docs/solution.md, docs/lessons.md) and surface drift against current code. Use after any sizeable change to src/agora/saga/, src/agora/api/, src/agora/agents/, src/agora/models/, src/agora/config.py, Makefile, alembic/versions/, or .github/workflows/ — or when the user asks "are the docs still accurate", "check for doc drift", "stale-check the PRDs", or before tagging a release. Outputs a punch list of file:line drift candidates rather than rewriting docs autonomously.
---

# docs-stale-check

The Agora docs drift behind code. This skill makes each pass cheap by
listing every drift-prone surface, the source-of-truth file for each,
and the patterns to grep. It produces a punch list — fixes happen in
follow-up PRs.

## Doc inventory (what to walk)

Every pass must touch all of these, not just `docs/prd/`:

| Doc                                         | Why it drifts                                            |
| ------------------------------------------- | -------------------------------------------------------- |
| `README.md`                                 | Status narrative, quick layout, getting-started commands |
| `CLAUDE.md`                                 | Known-gaps list, install command, test counts            |
| `docs/prd/00-overview.md`            | Overall hypothesis, success criteria, test count         |
| `docs/prd/01-lifecycle-and-states.md`| State enum, compensator table, ISO 18626 mapping         |
| `docs/prd/02-agents.md`              | Agent inputs/outputs/tools/status                        |
| `docs/prd/03-saga-and-idempotency.md`| SQL schema comments, outbox, replay rules                |
| `docs/prd/04-discovery.md`           | DiscoveryAgent flow, client factories, env flags         |
| `docs/prd/05-staff-console.md`       | Endpoint list, auth, HTMX/Jinja2 status, override        |
| `docs/prd/06-non-functional.md`      | NFR claims (observability, reliability, FedRAMP)         |
| `docs/adr/0001-…` … `0016-…`        | Mostly stable, but Status field can drift                |
| `docs/architecture.md`                      | Mermaid diagrams (state machine, layer cake)             |
| `docs/runbook.md`                           | Env-var table, schema columns, operational steps         |
| `docs/solution.md`                          | "Open risks & gaps" table, ADR count, schema blocks      |
| `docs/lessons.md`                           | "Backlog item: ..." trailers that have been shipped      |

If a new top-level doc is added, append it here.

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
  - `.github/workflows/` (CI gates that docs may reference)

## Output discipline

This skill **produces a punch list** — one entry per drift candidate
with file:line. It does NOT rewrite docs autonomously. After the
list is in front of the user, ask if they want a follow-up pass to
revise the doc(s).

Each entry is one line:

```
<doc-path>:<line>  <category>  <claim in doc>  →  <reality in code>
```

Categories:

| Category              | Meaning                                                            |
| --------------------- | ------------------------------------------------------------------ |
| `state`               | Lifecycle state mentioned but not in `LifecycleState` enum         |
| `step`                | Step name not in `StepName` enum                                   |
| `compensator-target`  | Compensator's `state_after` disagrees with `flows.py`              |
| `endpoint`            | API path / verb mismatch with `api/app.py`                         |
| `adr-ref`             | ADR-NNNN reference doesn't resolve to a file in `docs/adr/`        |
| `make-target`         | `make X` referenced but not in `Makefile` (or target broken)       |
| `module`              | Python module path referenced but not present in `src/agora/`      |
| `env-var`             | `AGORA_*` / `RESHARE_*` etc. mentioned without matching `Settings` field |
| `schema-field`        | DB column or pydantic field listed but not in ORM/model            |
| `agent-contract`      | Agent input/output/tool claim disagrees with the agent's code      |
| `freshness-header`    | Missing or outdated `> Last reviewed against code: …` line         |
| `status-narrative`    | README/CLAUDE.md prose claim about project state ("bootstrap phase", "demo not yet runnable", test count) is wrong |
| `file-tree`           | Directory-tree code-block listing dirs/files that don't match `ls` |
| `closed-backlog`      | "Backlog item: …" / "TODO: …" / "Out of scope today" sentence describing work that has since shipped |
| `diagram-element`     | Mermaid / ASCII diagram missing a state, component, or arrow that exists in code (e.g. APPROVING state, OutboxWorker lifespan task) |
| `ci-claim`            | Doc claims "CI does X" but `.github/workflows/*.yml` doesn't       |
| `test-count`          | Doc cites N tests; actual `pytest --collect-only -q` differs       |

Group entries by file. End with a one-line count summary
(`N drift candidates across M files`).

## Drift surfaces (run all of these)

### 1. Lifecycle states & steps

**Source of truth:** `src/agora/models/lifecycle.py` —
`LifecycleState`, `StepName`, `TERMINAL_STATES`.

For each doc, grep for capitalised state nouns
(`Submitted`, `Routed`, `Approving`, `Approved`, `Shipped`, `Returned`,
`Cancelled`, `Unfilled`, `Disputed`) and any others mentioned (e.g.
fictional `Recalled`, `Reconciled`). Anything not in the enum is
drift; the inverse (a state in the enum but missing from a diagram)
is *also* drift — file under `diagram-element`.

**Past hits:**
- PRD-01 referenced `Recalled` (not a state); PRD-01 "Reconciled"
  terminal in ASCII; architecture.md state machine had both.
- `architecture.md` state diagram missed `APPROVING` after ADR-0012
  shipped (PR #17). The state existed in `lifecycle.py:27` but the
  diagram still showed `Routed → Approved` directly. Caught in PR #32.

### 2. Compensator state targets

**Source of truth:** `src/agora/saga/flows.py` — each compensator's
`state_after`. Re-derive from code on every pass; do not cache the
table here, because the skill itself drifts otherwise.

Read `flows.py` once at the start of the pass and write down the
forward → `state_after` mapping locally. Then grep for "compensator"
tables in PRD-01, architecture.md, runbook.md, solution.md and diff.

### 3. API endpoints

**Source of truth:** `src/agora/api/app.py` — all `@app.<verb>` and
`router.<verb>` decorators. Also check the lifespan block for any
documented background tasks (`asyncio.create_task(...)`).

Grep all docs for `/sagas`, `/requests`, `/health` mentions. Verify:
- No `/api` prefix (we don't use one)
- No `/override` (not implemented)
- Verbs match (POST vs GET)
- If a doc names lifespan tasks (e.g. "OutboxWorker runs every 1s"),
  cross-check task name + interval against `app.py::lifespan` and
  the relevant `AGORA_*_INTERVAL_SECS` default in `config.py`.

**Past hits:**
- PRD-05 had `/api/sagas/...` and `/override`.
- PRD-05 still references `POST /sagas/{id}/override` — never
  implemented. Either land the endpoint or rewrite the line as
  "future" (parked as known drift).

### 4. ADR references

**Source of truth:** `docs/adr/` — exactly the files that exist.

Grep every `*.md` in scope for `\b0\d{3}-` patterns. Each must map to
an existing file. Also verify topic labels (e.g. `0007` is FedRAMP,
`0008` is ULID, `0011` is outbox commit-then-enqueue, `0012` is
APPROVE-via-outbox).

Also reverse-check ADR counts: `solution.md` and `prd/00-overview.md`
quote a count (e.g. "(12 docs)") — diff against
`ls docs/adr/ | wc -l`.

**Past hit:** PRD-06 referenced ADR-0008 for FedRAMP; actually 0007.
A pre-commit hook (`.claude/hooks/check_adr_refs.py`) catches new
ADR-ref drift on edit; this skill catches anything that already
slipped in plus count drift.

### 5. `make` targets

**Source of truth:** `Makefile` (the `.PHONY:` line lists all
targets).

Grep docs for `make <something>`. Each must exist in the Makefile.
Targets that exist but reference missing modules (e.g. `make chaos`
→ `python -m agora.demos.chaos` where the module is absent) count
as drift too.

**Past hits:**
- PRD-00 success criteria referenced `make chaos` while the chaos
  demo module was unimplemented. Closed in PR #31 by removing the
  target from the Makefile + the success-criteria row.

### 6. Module references

**Source of truth:** `src/agora/`.

Grep docs for `agora.demos.<X>`, `agora.<subpkg>.<module>` patterns.
Verify each module exists. Use `Glob` against `src/agora/**/*.py`.

Also catches asyncio task-name confusion: when a doc cites
`agora.tracking.scanner` as a module path but it's actually the
asyncio task name (real module: `agora.agents.tracking`), file under
`module`.

### 7. Environment variables

**Source of truth:** `src/agora/config.py` — every `Field(..., alias=...)`.

Grep docs for `AGORA_*`, `RESHARE_*`, `NCIP_*`, `SRU_*`, `OUTBOX_*`,
`SAGA_*`, `TRACKING_*` patterns. Each must have a matching alias in
`Settings`. Also reverse-check: `runbook.md` has an env-var table —
every `Settings` field should have a row.

High-churn vars to keep an eye on:
`AGORA_OUTBOX_WORKER_ENABLED`, `AGORA_OUTBOX_POLL_INTERVAL_SECS`,
`AGORA_TRACKING_SCANNER_ENABLED`, `AGORA_TRACKING_SCAN_INTERVAL_SECS`,
`AGORA_TRACKING_RECALL_AFTER_DAYS`, `NCIP_AGENCY_ID`.

### 8. Schema fields (DB + pydantic)

**Sources of truth:**
- `src/agora/saga/db.py` (ORM)
- `src/agora/saga/idempotency.py` (status-string literals like
  `pending`, `in_flight`, `delivered`, `dead_letter`)
- `src/agora/models/` (pydantic)

Grep docs for column names in inline SQL blocks. Every column listed
must exist in the ORM model. Specifically check:

- `saga_event` columns (id, saga_id, seq, kind, step, state_before,
  state_after, actor, idempotency_key, iso_message_id, payload,
  outcome, rationale, ts)
- `outbox` columns (id, saga_id, target, idempotency_key, payload,
  status, attempts, last_error, scheduled_for, claimed_at,
  delivered_at)
- `outbox.status` enum values (`pending | in_flight | delivered |
  dead_letter`) — every doc that quotes the enum must list all four
- `outbox.target` values (`reshare | ncip` today)
- `inbox` columns (message_id, source, received_at, response)

**Past hits:**
- PRD-03 had simplified outbox schema missing `attempts`,
  `last_error`, `scheduled_for`, `dead_letter` status.
- PR #25 added `claimed_at` and `in_flight`; multiple docs missed
  the additions (closed in PR #30 + PR #32).

### 9. Agent contracts

**Source of truth:** `src/agora/agents/<name>.py`. Re-read the
relevant module per pass — do not trust the bullets below if the
file's mtime is newer than this skill.

For each agent claim in PRD-02 (Inputs / Outputs / Tools), diff
against the actual agent module:

- DiscoveryAgent: SRU-only today; CrossRef/WorldCat planned
- RoutingAgent: rules-based; no LLM call yet
- PolicyAgent: in-process rule tables
- TransactionAgent: builds intents (since ADR-0012); ReShare calls
  go through outbox worker
- TrackingAgent + OverdueScanner: deterministic two-tier keys
  (`overdue-{saga_id}`, `recall-proposed-{saga_id}`); scanner runs
  as `agora.tracking.scanner` asyncio task spawned from FastAPI
  lifespan (per CLAUDE.md known-gaps)
- ReconciliationAgent: thin wrapper over `Coordinator.run_compensator`,
  not a direct ledger writer

**Past hits:** PRD-02 had all of: WorldCat sandbox lookup,
Reconciliation as ledger-writer, TrackingAgent without
OverdueScanner. (Fixed in PR #6 but high recurrence risk.)

### 10. Freshness headers

**Source of truth:** doc convention started in PR #6 + #7.

Every revised PRD / architecture / runbook / SDD has a line
`> Last reviewed against code: YYYY-MM-DD` near the top. List any
file in scope that lacks this header so we can add it on the next
revision pass. Exempt: `docs/lessons.md` (append-only log;
optional), `docs/adr/*` (one-shot decisions; freshness lives in
Status field).

### 11. Status-narrative claims (README + CLAUDE.md prose)

**Sources of truth:**
- Test count: `pytest --collect-only -q | tail -1` (or count from
  the latest CI run)
- "Demo runnable?": existence of `src/agora/demos/happy_path.py` +
  the `make demo` target
- "What's shipped vs planned": the latest CLAUDE.md "known gaps"
  block

Phrases to grep for and verify:
- `bootstrap phase`, `not yet runnable`, `not yet wired`,
  `not implemented` (in README; often outdated)
- `\d+ tests` / `\d+ passed` (test counts go stale)
- `(prototype|MVP) demo` claims
- `Out of scope today` in runbook (re-check against shipped PRs)

**Past hit:** README said "Bootstrap phase. End-to-end demo not yet
runnable" months after `make demo` started working. Closed in PR #32.

### 12. File-tree drift

**Source of truth:** actual filesystem (`Glob` `src/agora/**`,
`docs/**`).

README's "Quick layout" block, runbook's repo-tree blocks, and any
PRD that draws a directory tree must match reality. Common misses:
- New top-level dirs (e.g. `alembic/`, `.github/workflows/`)
- New `src/agora/` subpackages (e.g. `demos/`)
- Renamed modules (e.g. `saga/steps.py` → `saga/flows.py`; today
  both exist but `flows.py` is canonical for forward+compensator
  pairs)

**Past hit:** README quick layout missed `demos/`, `logging.py`,
`py.typed`, `alembic/`, `.github/workflows/`, plus all four
top-level `docs/*.md` files. Closed in PR #32.

### 13. Closed-backlog drift in lessons.md

**Source of truth:** `docs/lessons.md` itself (search), cross-checked
against the latest CLAUDE.md known-gaps block + recent PRs in `git
log --oneline`.

Grep `docs/lessons.md` for `Backlog item:`, `not yet wired`,
`not implemented`, `Out of scope today`, `TODO:`. Each match is a
candidate — if the work has since shipped, the lesson body needs an
amendment ("Closed by PR #N") or the trailing sentence should be
struck.

**Past hit:** "Alembic path has *never* been tested against a real
Postgres in CI. ... Backlog item: stand up a real-Postgres Alembic
test." was still in the lessons doc after PR #24 shipped exactly
that. Closed in PR #32.

### 14. Diagram-element drift (Mermaid / ASCII)

**Sources of truth:**
- State diagram: `LifecycleState` enum + transitions in
  `saga/flows.py`
- Layer cake: `src/agora/api/app.py::lifespan` for what runs as a
  background task; `src/agora/saga/` for core components
- Idempotency / outbox flow: `saga/idempotency.py` +
  `saga/outbox.py`

Walk every Mermaid block in `architecture.md` (and any ASCII flow in
PRDs). For each:
- Are all enum members from `LifecycleState` represented? (Inverse
  of category 1.)
- Are all lifespan tasks shown? (Today: `OutboxWorker`,
  `OverdueScanner`.)
- Is the outbox status enum complete (`pending | in_flight |
  delivered | dead_letter`)?

**Past hit:** PR #32 layer cake added a `WORKERS` subgraph because
the original diagram showed `TX → ReShare` directly, hiding the
outbox worker that actually carries the call.

### 15. CI-claim drift

**Source of truth:** `.github/workflows/*.yml`.

Grep docs for `CI does`, `gates on`, `runs in CI`, `pre-commit`,
`triple gate`. Each claim must match one of:
- `audit.yml` — bandit + pip-audit + detect-secrets
- `postgres-tests.yml` — alembic+ORM parity, multi-worker outbox
  on `postgres:15-alpine`
- `triple-gate.yml` — pytest + ruff + mypy --strict

If a doc says CI doesn't gate something that it now does (or
vice-versa), file under `ci-claim`.

### 16. Test-count drift

**Source of truth:** `pytest --collect-only -q | tail -1`, or the
latest `triple-gate.yml` run.

Grep CLAUDE.md, README, and solution.md for `\d+ tests` / `\d+
passed`. Diff against actual count. Cheap to check, easy to forget.

## How to run

1. **Read sources of truth first** (in this order — top is most
   load-bearing):
   - `src/agora/models/lifecycle.py` (states + steps)
   - `src/agora/saga/flows.py` (compensator targets, NCIP fan-out)
   - `src/agora/saga/db.py` + `saga/outbox.py` + `saga/idempotency.py`
     (schema + status enum)
   - `src/agora/api/app.py` (endpoints + lifespan tasks)
   - `src/agora/config.py` (env-var aliases)
   - `Makefile` (dev targets)
   - `.github/workflows/*.yml` (CI claims)
   - `ls docs/adr/` for ADR count + filenames
2. **Walk the doc inventory** (top of this skill). For each doc, run
   the relevant subset of surfaces 1–16. Don't skip a doc because
   "nothing's changed there recently" — drift accumulates from code
   churn, not doc churn.
3. **Build the punch list** as you go (one line per finding,
   `<doc-path>:<line>  <category>  <claim>  →  <reality>`).
4. **Output the list grouped by file**, end with the count summary
   and category breakdown table, and ask: "Want me to revise the
   affected docs in a follow-up pass on a fresh branch?"

### Reliability notes

- **The compensator-target table and the agent-contract bullets
  inside this skill are themselves drift-prone.** Re-derive both
  from `flows.py` / `agents/<name>.py` at the start of each pass; if
  they disagree with the skill, fix the skill in the same PR as the
  doc revisions.
- **"Past hits" are not a checklist.** They're examples of patterns
  the skill should keep catching. New past hits should be appended
  as they surface, with a PR pointer.
- **`# nosec`-style pragma drift** is not in scope for this skill —
  use the `security-audit` skill for that.

## Out of scope

- Rewriting docs (deliberately separate step — drift fixes are PRs)
- Validating the actual code (use the test suite + ruff + mypy)
- ISO 18626 XSD validation (that's `iso18626-validate`)
- ReShare endpoint correctness (`reshare-probe`)
- Security pragma drift (`security-audit`)

## Pair tools

- Companion hook `.claude/hooks/check_adr_refs.py` runs on every
  Write/Edit to `docs/**/*.md` and catches new ADR-ref drift before
  it lands.
- `.claude/skills/lifecycle-extend/SKILL.md` writes lifecycle
  extensions in lockstep across docs + code so this skill has less
  drift to find later.
- `.claude/skills/security-audit/SKILL.md` covers the audit-pragma
  drift surfaces this skill deliberately ignores.
