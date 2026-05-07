# Next session resume note

**Last updated:** 2026-05-06 (PRs #109–#110 open; PR #111 merged).

## Repo state

- `master` clean at PR #111 (`fdb2ace`), test count **361** (350 non-postgres + 6 postgres-only + 5 skipped env-gated).
- PR #109 open: `test/coverage-gaps-5` — 24 new tests (ncip, crossref, okapi_auth, outbox, ledger, tracking). Merge to bring master to **~385**.
- PR #110 open: `test/api-app-coverage` — 19 new endpoint tests for api/app.py. Based on master (needs rebase after #109). Merge to bring master to **~404**.
- ADR count: **16**.
- Overall coverage: **~90%** (`pytest --cov=src/agora`).

## PRs this session (in order)

| PR | Title | Status |
|----|-------|--------|
| #109 | test: coverage gaps batch 5 (ncip, crossref, okapi_auth, outbox, ledger, tracking) | **Open** |
| #110 | test: api/app.py coverage — 19 endpoint tests (90% → ~96%) | **Open** |
| #111 | docs: stale-check patch — APPROVING state, lifespan tasks, scanner, auth, step enum | Merged |

## What to do at session start

```
# Merge open coverage PRs first
gh pr merge 109 --squash --delete-branch
gh pr merge 110 --squash --delete-branch   # may need rebase on #109 first
git checkout master && git pull

.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_outbox_concurrent_postgres.py --ignore=tests/test_alembic_postgres.py
# expect ~385-404 pass, 5 skip (after both PRs merged)
ruff check src tests      # clean
mypy --strict             # clean
.venv/Scripts/python.exe scripts/sync_doc_counts.py --fix  # update doc counts after test changes
```

## Remaining coverage gaps (after PRs #109–#110 merged)

Overall coverage ~93-95%. Remaining modules below 100%:

| Module | Coverage | Uncovered lines | Notes |
|--------|----------|-----------------|-------|
| `api/app.py` | ~96% | 162-163, 207, 228-231, 743-744, 892, 931, 1089, 1095-1096, 1240 | FastAPI error paths partly uncovered |
| `saga/db.py` | 85% | 61, 66, 68, 73, 75, 223-230, 237, 253-255, 260-262 | ORM helper paths |
| `evals/routing.py` | 80% | 129, 215, 315-344, 415-425, 434 | Eval harness — needs careful setup |
| `agents/routing_llm_adk.py` | 72% | 157-180 | Requires real ADK/Vertex — skip |
| `agents/discovery.py` | 98% | 213, 215 | minor |
| `agents/routing.py` | 99% | 265 | minor |
| `clients/openurl.py` | 95% | 95-96 | Unreachable `except ValueError` — defensive guard |
| `saga/idempotency.py` | 99% | 169 | Postgres `FOR UPDATE SKIP LOCKED` — skip (SQLite-only) |
| `cli.py` | 0% | 7-36 | CLI module — low priority |
| `demos/happy_path.py` | 0% | 11-237 | Demo script — low priority |

**Best next PRs:**
1. `saga/db.py` 85% — 16 lines of ORM helper paths (error branches, dialect-specific paths)
2. Remaining `api/app.py` lines — `_derive_extras` compensator branches (228-231), error propagation (743-744, 892, 1089, 1095-1096)
3. `evals/routing.py` 80% — eval harness, requires careful setup

## Docs stale-check (this session)

PR #111 fixed 10 drift candidates:
- runbook §2.1: APPROVING added to state flow
- runbook §1.5 + solution §3.1: lifespan names both tasks (OutboxWorker + OverdueScanner)
- solution agents table: "no cron yet" → "asyncio task, 300 s interval"
- architecture.md layer cake: "future HTMX/React" → "HTMX/Jinja2, ADR-0015"
- prd/03: SQL step comment adds `'resolve'`
- prd/05: auth section describes optional HTTP Basic
- docs-stale-check skill: PRD inventory uses correct per-file names

## Backlog (current, prioritised)

### Sandbox-blocked
1. **NCIP live probe** — smoke test ready (`tests/test_ncip_http_smoke.py`).
   Set `AGORA_TEST_NCIP_URL` + `RESHARE_TENANT` + `NCIP_AGENCY_ID` and run:
   ```
   pytest tests/test_ncip_http_smoke.py -v
   ```

2. **WorldCat holdings lookup** — structural gap. No freely accessible
   union holdings catalog exists. Revisit when institutional OCLC access
   or a live multi-tenant pilot materialises.

### Needs ADR / design decision
- **ADR-0016 follow-up (production recall)**: Option A (ISO 18626 Cancel via
  `message` performAction) is the production path. Needs two-tenant sandbox
  and wire-level testing.

### Revisit later
- FOLIO community sandbox: folio-snapshot.dev.folio.org
- Index Data / OLE: info@indexdata.com, FOLIO Slack #reshare

## Key gotchas

- **FOLIO tenant IDs: alphanumeric only.** `consortium-a` → Postgres
  schema syntax error in mod-rs. Use `diku`.
- **HttpNcipClient source-review-only** — unverified against live mod-ncip.
- **WorldCat v1 EOL'd Dec 2024.** v2 API requires institutional OCLC subscription.
- **No open SRU union holdings catalog returns MARC 852 data.** Routes via
  `AGORA_CONSORTIUM_MEMBERS` fallback (PR #100).
- **`scripts/build_deck.py` checkmarks are line-drawn, not Unicode glyphs** —
  closed in PR #113 via `_draw_check` helper. Helvetica built-in fonts still
  lack Unicode `✓`; if you need any other glyph (e.g. arrows), draw it with
  `c.line()` or register a TTF.
- **Retry delays in HttpReShareClient tests** — 5xx/ConnectError paths trigger
  tenacity retry (3 × ~0.5s = ~1.5s per test). `test_reshare_http_client.py`
  runs ~9.5s total because of this. Acceptable.
- **`OnSuccess` and `Handler` types are positional `Callable`s** — keyword
  argument calls on them fail mypy strict. Always call positionally in tests.
- **ApprovalBody / CompensateBody require `actor` + `rationale`** — JSON
  approve/compensate tests that omit these fields get 422 not the expected error.
- **PR #110 branch (`test/api-app-coverage`) was based on an earlier master.**
  If CI shows conflicts after #109 merges, rebase before merging #110.

## Resume protocol

- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- `scripts/sync_doc_counts.py --fix` after test count changes.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
