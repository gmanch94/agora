# Next session resume note

**Last updated:** 2026-05-07 (PRs #133-#139 merged; master at 503 collected / 492 pass + 11 skipped / 99% coverage).

## Repo state

- `master` clean at `5e77360` (PR #139 — `security_scan.py` Windows path-normalization).
- Test count **503 collected** on master (492 pass + 11 skipped env-gated).
- ADR count: **17** (ADR-0017 documents `renew_request` sandbox gap).
- Overall coverage: **99%** (`pytest --cov=src/agora`).
- LLM routing baseline: **top-1 1.0000 / mean Spearman 1.0000** (40 scenarios, gemini-2.5-flash).
- Security audit: **bandit 0 / pip-audit 0 / detect-secrets 0** (post #139 — script no longer false-positives on Windows).

## PRs this session (in order)

| PR | Branch | Title | Status |
|----|--------|-------|--------|
| #133 | `docs/prd-refresh-2026-05-07` | docs(prd): refresh all 7 PRDs against code (post #100/#101/#102/#116/#117) | Merged |
| #134 | `fix/renew-portal-blockers` | fix(renew,portal): close three ship-blockers from post-#117 strict review | Merged |
| #135 | `chore/next-session-post-134` | chore: update NEXT_SESSION.md after PRs #133-#134 | Merged |
| #136 | `docs/lessons-post-134` | docs(lessons): capture three post-#134 lessons | Merged |
| #137 | `fix/portal-requests-sql-filter` | fix(portal): SQL-side patron_id filter — patrons with sagas outside the top-200 now show | Merged |
| #138 | `docs/stale-check-post-117` | docs: stale-check sweep — 11 drift fixes across 4 files (post #100-#134) | Merged |
| #139 | `fix/security-audit-script-windows` | fix(security-audit): two false-positive sources in bundled detect-secrets filter | Merged |

### Notable code changes landed

- **#134**: `renew_forward` validates `extension_days ∈ [1, 180]` (single chokepoint for JSON + HTMX); `_portal_due_date` is compensator-aware (renew stack push/pop); patron-id 404 dropped from `portal_saga_detail` (saga UUID is the secret, patron-id is a UX label). Lessons in `docs/lessons.md` 2026-05-07 entries.
- **#137**: `portal_requests` filters via SQL JSON path `Saga.request_payload['patron']['patron_id'].astext == patron_id` so the LIMIT 200 caps the patron's rows, not the table's. Closes the post-#134 advisor-leftover false-negative.
- **#138**: README ADR count fixed (16 → 17), broken ADR-0016 link fixed, RENEW + portal coverage added to runbook + solution API tables and architecture.md state machine + layer cake.
- **#139**: `security_scan.py` filter now normalises Windows backslashes to forward slashes for baseline lookup, and skips `.secrets.baseline` itself in scan-result post-processing. Aligns local audit with CI behaviour.

## What to do at session start

```bash
git checkout master && git pull

# Verify
.venv/Scripts/python.exe -m pytest tests/ -q          # expect 492 pass, 11 skip (503 collected)
.venv/Scripts/python.exe -m ruff check src tests      # clean
.venv/Scripts/python.exe -m mypy --strict             # clean
.venv/Scripts/python.exe .claude/skills/security-audit/scripts/security_scan.py .  # 0 findings
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

### Code-only backlog
*(Empty.)* The post-#134 advisor leftover (`portal_requests` 200-row Python-side filter) closed in #137 via SQL-side JSON-path filter. No open code-only backlog at the prototype's scale.

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
- **`portal_requests` filters SQL-side via JSON path (post-#137).** `Saga.request_payload['patron']['patron_id'].astext == patron_id` compiles cross-DB via `_json_type` (`JSONB().with_variant(JSON(), "sqlite")`). Don't refactor back to "load 200, filter Python-side" — patrons with older sagas would silently disappear.
- **`security_scan.py` baseline filter is path-normalised + skips the baseline file (post-#139).** detect-secrets reports OS-native separators; baseline is forward-slash. Don't break the `lookup_key.replace("\\\\", "/")` line or the baseline-file-skip without re-running on Windows + Linux to verify both.
- **`SagaEvent` requires `id: int` and `iso_message_id: str | None`** when constructed directly in unit tests (PR #129).
- **Always branch + PR, never commit directly to master.**
- **GCP ADC for LLM eval refresh:** needs all of `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION=us-central1` + `AGORA_ROUTING_LLM_ENABLED=1` + `AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash` + `AGORA_ROUTING_LLM_TIMEOUT_SECS=30`. Without `GOOGLE_GENAI_USE_VERTEXAI=true` SDK silently falls back to API-key auth and 401s every call.

## Resume protocol

- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- `scripts/sync_doc_counts.py --fix` after test count changes.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
