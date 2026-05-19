# Next session resume note

**Last updated:** 2026-05-09 EOD (audit-remediation sprint shipped via PRs #142/#143/#144; master clean).

## Repo state

- `master` at `f0e2605` (#144 — `docs(security-model): bump test count 541 -> 556`). All audit-remediation work merged via PRs #142 (scaffold), #143 (36/42 findings closed), #144 (test-count drift fix).
- No open branches locally. No outstanding PRs.
- Test count **556 collected** (550 pass + 6 postgres-only skipped). Verified at session end via `pytest --collect-only`.
- ADR count: **18** (ADR-0018 documents the multi-principal auth follow-up after the tenant-scoping stopgap landed).
- LLM routing baseline: **top-1 1.0000 / mean Spearman 1.0000** (40 scenarios, gemini-2.5-flash) — unchanged.
- Security audit: **bandit 0 / pip-audit 0 / detect-secrets 0**. mypy `--strict` clean over `src/` and `tests/`. ruff clean.
- Latest audit: `docs/security-audits/2026-05-09.md`. 36 of 42 findings closed in code; 6 documented as operator-side or scoped-out (see SECURITY_MODEL.md § 6).

## Sprint commits this session (audit remediation)

| Commit | Batch | Findings closed |
|--------|-------|-----------------|
| `eb15ed9` | 1: XML safety + outbox hardening | #4, #5, #6, #12, #18, #28, #29, #36 |
| `9b6ba41` | 2: Input validation tightening | #14, #15, #17, #19, #20, #22, #30 |
| `2ef8085` | 3: Credential / config hygiene | #7, #10, #25, #33, #34 |
| `d06adfa` | 4: Auth + tenant stopgap + portal HMAC + OkapiAuth expiry | #1, #2, #3, #11/#13, #21 |
| `512ede5` | 5: Web hardening | #8, #9, #23, #31, #38 |
| `5b16277` | 6: LLM prompt injection guard | #16 |
| `29c36fb` | 7: Tracking race + JSONB index + jitter + fail-fast renew | #27, #35, #37, #42 |
| `a6eb6fa` | 8: Network-posture docs + SECURITY_MODEL fill + Jinja XSS guard | #24, #32, #39 |
| `89ba48b` | follow-up: HTML form `actor=principal.actor` (audit #21 regression) + docs drift sweep | reviewer-flagged |
| `625631e` | follow-up: scope guards on staff HTML detail / inbox / browser views (audit #3 follow-up) | reviewer-flagged |
| `de94df1` | follow-up: audit-suite hygiene — `# nosec B311` for jitter, `pragma: allowlist secret` for docstrings | post-sprint cleanup |

### Substantive new behaviour to know about

- **Auth on JSON API.** `/sagas/*`, `/requests`, `/portal/*` now require Basic auth when `AGORA_CONSOLE_PASSWORD` is set. ADR-0007's no-auth posture is superseded by ADR-0018.
- **Tenant scoping.** `AGORA_CONSOLE_LIBRARY_SYMBOL` binds the principal to one library; saga endpoints 403 on cross-library access. `GET /sagas` SQL-filters. `POST /requests` rejects out-of-scope. Single-tenant by construction (multi-principal is the ADR-0018 follow-up).
- **Patron portal HMAC.** `AGORA_PORTAL_SIGNING_KEY` set → `/portal/*` requires `?token=<HMAC>`. Detail signs (saga_id, patron_id) AND verifies stored patron_id matches. Empty key = dev-only form-entry path.
- **OkapiAuth proactive refresh.** Login switched to `/authn/login-with-expiry`; body parses `accessTokenExpiration`; refreshes 60s before expiry. Live FOLIO probe still pending (backlog).
- **Outbox hardening:** allow-list dispatch (`_RESHARE_ACTIONS` / `_NCIP_ACTIONS`), lease-race verification (`outbox_claim_still_ours`), deterministic compensator key, fail-fast for `renew_request`.
- **XML safety:** shared `agora.clients._xml.SAFE_XML_PARSER` everywhere.
- **Input validation:** `StepExtras` typed model, `IllRequest` field-level `max_length`, server-side `request_id`, `IdempotencyConflictError` on collision.
- **LLM prompt injection guard:** `HolderCandidate.symbol` regex, `repr()`-quoted prompt rendering, allow-listed raw keys, system-prompt directive.
- **Web hardening:** CSRF (`AGORA_CSRF_ENABLED`), rate limit (`AGORA_RATE_LIMIT_ENABLED`), HTTPSRedirect in prod, security headers, `/docs` hidden in prod.
- **Credentials:** `SecretStr` for password / db_url fields; CLI redacts; `create_app` refuses dev `:agora@` default outside `AGORA_ENV=dev`; `api_host` defaults to `127.0.0.1`.
- **CI guards:** `scripts/check_template_xss_guards.py` + `tests/test_template_xss_guards.py` for Jinja autoescape bypasses.

New env vars (5 added): `AGORA_CONSOLE_LIBRARY_SYMBOL`,
`AGORA_PORTAL_SIGNING_KEY`, `AGORA_RATE_LIMIT_ENABLED` /
`AGORA_RATE_LIMIT_REQUESTS` / `AGORA_RATE_LIMIT_WINDOW_SECS`,
`AGORA_CSRF_ENABLED`. All documented in `.env.example` + runbook §
1.2.

New ADR: ADR-0018 (tenant-scoping stopgap).
New skill / script: `scripts/check_template_xss_guards.py`.

## What to do at session start

```bash
git checkout master && git pull

# Verify
.venv/Scripts/python.exe -m pytest tests/ -q          # expect 542 pass, 11 skip (553 collected)
.venv/Scripts/python.exe -m ruff check src tests scripts   # clean
.venv/Scripts/python.exe -m mypy --strict             # clean
.venv/Scripts/python.exe scripts/check_template_xss_guards.py  # OK (no XSS-guard violations)
.venv/Scripts/python.exe .claude/skills/security-audit/scripts/security_scan.py .  # 0 findings (bandit + pip-audit + detect-secrets)
```

**Audit-remediation sprint shipped.** PRs #142/#143/#144 merged. No
outstanding local commits. Backlog below is the durable list — the
"local-only commits" warning previously here is no longer applicable.

## Backlog (prioritised)

### Needs sandbox / design work
- **ADR-0017 follow-up (renew_request)**: Confirm mod-rs action for borrower-initiated renewal against a live two-tenant sandbox. Update `HttpReShareClient.renew_request` and add wire-level test.
- **ADR-0016 follow-up (production recall)**: ISO 18626 Cancel via `message` performAction. Needs two-tenant sandbox and wire-level testing.
- **Audit 2026-05-09 #11/#13 — FOLIO `/authn/login-with-expiry` probe**: Batch 4 of the audit-remediation sprint switched OkapiAuth to `/authn/login-with-expiry` and parses `accessTokenExpiration` from the JSON body. The endpoint exists per FOLIO docs but has not been verified against a live FOLIO instance. Verify: (a) endpoint returns 201 + body shape `{"accessTokenExpiration": "<iso>"}` (b) `x-okapi-token` header still set, (c) FOLIO honours the expiry as a soft limit (token continues working past expiry until 401). Tolerant body parsing falls back to legacy reactive-only refresh on shape mismatch, but live verification closes the unknown.
- **Audit 2026-05-09 #3 follow-up — multi-principal auth**: ADR-0018 documents the single-principal scoping stopgap. The proper fix is JWT (or equivalent) with a `library_symbol` claim per principal. The `ConsolePrincipal` dataclass in `src/agora/api/app.py` is the seam — the dependency function changes shape, the rest of the API stays. Adds per-staff scoping and re-opens audit #26 (PII filtering on cross-library views).
- **Audit 2026-05-09 #26 (PII filtering)**: Deferred until #3 multi-principal lands. After roles exist, `SagaDetail` should redact `patron_id` and similar fields when the caller's library doesn't own the saga.

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
