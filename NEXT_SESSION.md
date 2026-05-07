# Next session resume note

**Last updated:** 2026-05-07 (PRs #133 + #134 merged; master at 491 tests / 99% coverage).

## Repo state

- `master` clean at `9bc6a32` (PR #134 — RENEW/portal blocker fixes).
- Test count **491** on master (480 pass + 11 skipped env-gated). 10 new RENEW/portal regression tests landed in #134.
- ADR count: **17** (ADR-0017 documents `renew_request` sandbox gap).
- Overall coverage: **99%** (`pytest --cov=src/agora`).
- LLM routing baseline: **top-1 1.0000 / mean Spearman 1.0000** (40 scenarios, gemini-2.5-flash).

## PRs this session (in order)

| PR | Branch | Title | Status |
|----|--------|-------|--------|
| #133 | `docs/prd-refresh-2026-05-07` | docs(prd): refresh all 7 PRDs against code (post #100/#101/#102/#116/#117) | Merged |
| #134 | `fix/renew-portal-blockers` | fix(renew,portal): close three ship-blockers from post-#117 strict review | Merged |

### PR #134 substance (now landed on master)

Three real bugs caught by an advisor strict-grade pass over the RENEW + portal slice:

1. **`extension_days` validation moved into `renew_forward`** — single chokepoint serving JSON (`RenewBody`) + HTMX (`Form`). Previously `int(...) or DEFAULT` rewrote `0 → 28` silently and let `-5` through truthy (past due date). Now explicit None-fallback + `1 <= extension_days <= 180` raises ValueError.
2. **`_portal_due_date` made compensator-aware** — walks events in `seq` order maintaining a `renew_stack`; `forward.renew` pushes, `compensator.renew` pops. Previously a cancelled renewal left the portal showing the rolled-back due date.
3. **`portal_saga_detail` patron-id 404 dropped** — saga UUID is now explicitly the privacy boundary; `patron_id` is a UX label. The 404 was false reassurance because `portal_requests` accepts arbitrary patron-ids and lists their saga UUIDs anyway. PRD-05 § Patron portal documents the prototype-grade posture.

## What to do at session start

```bash
git checkout master && git pull

# Verify
.venv/Scripts/python.exe -m pytest tests/ -q  # expect 491 pass, 11 skip
.venv/Scripts/python.exe -m ruff check src tests      # clean
.venv/Scripts/python.exe -m mypy --strict             # clean
```

## Backlog (prioritised)

### Needs sandbox / design work
- **ADR-0017 follow-up (renew_request)**: Confirm mod-rs action for borrower-initiated renewal against a live two-tenant sandbox. Update `HttpReShareClient.renew_request` and add wire-level test.
- **ADR-0016 follow-up (production recall)**: ISO 18626 Cancel via `message` performAction. Needs two-tenant sandbox and wire-level testing.

### Coverage state — at the summit

| Module | Coverage | Notes |
|--------|----------|-------|
| All src/agora/* modules | **100%** locally | except idempotency.py:169 |
| `saga/idempotency.py:169` | 99% | Postgres-only `with_for_update(skip_locked=True)` — covered by CI's `postgres-tests.yml` (gated on `AGORA_TEST_DB_URL`). Don't add `# pragma: no cover` — CI honestly covers it. |

**No coverage backlog remains for code changes alone.** Future PRs that add code should land with their own tests; the green-field is fully tested.

### LLM baseline state
`evals/routing/baseline.json` is **fresh** (refreshed in PR #129). 40/40 top-1, Spearman 1.0. Don't refresh again unless rules engine or prompt template changes.

### Sandbox-blocked
1. **NCIP live probe** — smoke test ready (`tests/test_ncip_http_smoke.py`).
   Set `AGORA_TEST_NCIP_URL` + `RESHARE_TENANT` + `NCIP_AGENCY_ID` and run:
   `pytest tests/test_ncip_http_smoke.py -v`
2. **WorldCat holdings lookup** — no freely accessible union holdings catalog. Revisit when institutional OCLC access materialises.

### Code-only backlog (advisor leftovers from #134 review)
- **`portal_requests` 200-row Python-side filter** — patron with sagas outside the table-wide most-recent-200 sees an empty list (false negative). Fix: denormalised `patron_id` column + index, or paginate. Self-contained PR. Not urgent — prototype scale.

### Revisit later
- FOLIO community sandbox: folio-snapshot.dev.folio.org
- Index Data / OLE: info@indexdata.com, FOLIO Slack #reshare

## Key gotchas

- **FOLIO tenant IDs: alphanumeric only.** `consortium-a` → Postgres schema syntax error in mod-rs. Use `diku`.
- **HttpNcipClient source-review-only** — unverified against live mod-ncip.
- **WorldCat v1 EOL'd Dec 2024.** v2 API requires institutional OCLC subscription.
- **No open SRU union holdings catalog returns MARC 852 data.** Routes via `AGORA_CONSORTIUM_MEMBERS` fallback (PR #100).
- **Retry delays in HttpReShareClient tests** — 5xx/ConnectError paths trigger tenacity retry (~1.5s per test). Acceptable.
- **`OnSuccess` and `Handler` types are positional `Callable`s** — keyword argument calls fail mypy strict.
- **ApprovalBody / CompensateBody require `actor` + `rationale`** — JSON approve/compensate tests omitting these get 422.
- **RENEW uses `state_after = RECEIVED`** (same as current state). The coordinator has no forward-progress guard — intentional for renewal.
- **Portal uses `ev.step.value` string comparison** in `_portal_due_date` — avoids import issues across feature branches.
- **`_portal_due_date` is compensator-aware (post-#134)** — walks events maintaining a `renew_stack` so `forward.renew` push + `compensator.renew` pop restore the prior due date. Don't refactor back to last-write-wins.
- **Portal privacy posture (post-#134): saga UUID is the secret token.** `patron_id` query param is a UX label, not an access gate. Don't add patron-id 404s without also gating `/portal/requests` (which can't be gated without auth).
- **`SagaEvent` requires `id: int` and `iso_message_id: str | None`** when constructed directly in unit tests (PR #129).
- **Always branch + PR, never commit directly to master.**
- **GCP ADC for LLM eval refresh:** needs all of `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION=us-central1` + `AGORA_ROUTING_LLM_ENABLED=1` + `AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash` + `AGORA_ROUTING_LLM_TIMEOUT_SECS=30`. Without `GOOGLE_GENAI_USE_VERTEXAI=true` SDK silently falls back to API-key auth and 401s every call.

## Resume protocol

- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- `scripts/sync_doc_counts.py --fix` after test count changes.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
