# SECURITY_MODEL.md

The threat model and per-invariant enforcement layer for Agora. Auto-loaded by Claude Code per `~/.claude/rules/security.md`. Update on every change to: auth provider, DB schema, API surface, file storage, role definitions.

**STATUS: filled 2026-05-09 after the audit-remediation sprint (commits b15ed9..29c36fb).**

---

## 1. Auto-generated endpoint surfaces

Endpoints exposed by the stack that we did NOT explicitly write:

| Surface                                                | Reachable with         | Notes                                                                                                                                                                          |
| ------------------------------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| FastAPI auto-docs (`/docs`, `/redoc`, `/openapi.json`) | Public unless gated    | Audit #31 — hidden when `AGORA_ENV != dev` (`docs_url=None, redoc_url=None, openapi_url=None`). Operators can opt back in for staging via `AGORA_ENV=dev`.                     |
| Static files (`/static/*`)                             | Public                 | Mounts the staff-console JS/CSS bundle. No user uploads — server-controlled content only.                                                                                      |
| `httpx` outbound clients                               | Anyone with API access | `RESHARE_BASE_URL` and `OKAPI_URL` are operator-controlled env vars (audit #32 — treat as trusted-deploy-config; misconfiguration becomes SSRF). See runbook § 9.2.            |
| Postgres (asyncpg) wire protocol                       | DB credentials         | Default `:agora@` creds refused outside `AGORA_ENV=dev` (audit #25). Connection-string redaction in CLI / model_dump (audit #10).                                              |

**Implication for FastAPI:** there's no auto-generated REST surface like PostgREST / Hasura. Attack surface = routes we write + DB credentials. The "single-path-of-control assumption" failure mode applies less here, but the "app-layer-only validation" mode still applies — pydantic at the route boundary is necessary, but DB-level CHECK constraints / NOT NULL / FK mirror critical invariants where the validator could be bypassed (e.g. `saga_event.UNIQUE(idempotency_key)` is the load-bearing replay-safety primitive, not the app-layer dedup).

---

## 2. Auth roles / principals

| Role             | Source                                                                                                                                                                              | Bypass                                                                                  | Reachable from                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| anonymous        | No `Authorization` header                                                                                                                                                           | n/a                                                                                     | `/health`, `/static/*` (read-only). Everything else 401.      |
| `staff:<u>@<lib>`| HTTP Basic (`AGORA_CONSOLE_USERNAME` / `AGORA_CONSOLE_PASSWORD` SecretStr) → `ConsolePrincipal(username, library_symbol)`. The `@<lib>` suffix is `AGORA_CONSOLE_LIBRARY_SYMBOL`.  | Empty `AGORA_CONSOLE_PASSWORD` disables auth — dev only.                                | All `/sagas/*`, `/requests`, `/ui/*`, `/portal/*`.            |
| `agent:outbox-worker` | Internal lifespan-spawned background task                                                                                                                                       | Cannot be reached over the network; loops via `OutboxWorker.run_forever`.               | Saga ledger writes via `make_reshare_on_success` projection.  |
| `agent:tracking-scanner` | Internal lifespan-spawned background task                                                                                                                                    | Cannot be reached over the network; loops via `OverdueScanner.run_forever`.             | Saga ledger writes (advisory OBSERVATION events).             |
| Patron (portal)  | Anonymous + per-saga signed URL: `?token=HMAC(saga_id, patron_id)` over `AGORA_PORTAL_SIGNING_KEY`. Without the key the discovery surface is open (form entry; dev only).         | Empty `AGORA_PORTAL_SIGNING_KEY` disables HMAC gating — dev only.                       | `/portal/requests`, `/portal/requests/{saga_id}`.             |

**Default-deny / human-approval semantics.** Every saga forward step requires a `COMMITTED` `GATE` event before `Coordinator.run_forward` will run; `GateRequiredError` is raised otherwise. The `staff:<u>@<lib>` principal commits the gate via `/approve`; agents (routing, policy, discovery, etc.) are advisory and never commit gates directly. ADR-0005 is the canonical reference. Compensators run only against committed forwards (verified via `find_committed_forward`).

**Tenant scoping.** When `AGORA_CONSOLE_LIBRARY_SYMBOL` is set, every `/sagas/*` endpoint runs `_assert_saga_in_scope(saga, principal)` and refuses (403) on cross-library access. `/sagas` SQL-filters by JSONB-path so the listing also excludes other libraries' rows. `POST /requests` rejects out-of-scope `requesting_library`. Single-tenant by construction; multi-principal model is the ADR-0018 follow-up.

**Patron PII retention (G-07, ADR-0020).** Two surfaces: a background `RetentionScanner` (toggled by `AGORA_RETENTION_ENABLED`) that scrubs borrower fields on terminal sagas past `AGORA_RETENTION_DAYS` (default 90), and ADMIN-only DSAR endpoints `GET /admin/patrons/{patron_id}/sagas` + `POST /admin/patrons/{patron_id}/forget`. Anonymisation uses HMAC-SHA256 keyed by `AGORA_PII_SCRUB_SALT` (≥ 32 chars, fail-closed at three layers: scrub call, DSAR endpoint, app boot). Scrub walks all THREE PII surfaces — `saga.request_payload`, `saga_event.payload`, `outbox.payload` — so the saga ledger and the outbox queue do not leak cleartext breadcrumbs after the retention window. DISPUTED sagas are excluded from auto-scrub (open issue, evidence required). The scrub writes a `patron_scrubbed-{saga_id}` OBSERVATION event for audit trail.

**Role-based authorisation (G-02, ADR-0019).** `ConsolePrincipal.role` is one of `viewer < approver < admin`. Mutating endpoints (`POST /requests`, `/sagas/{id}/{approve,compensate,reject,override,discover,renew}`, all `/ui/sagas/{id}/*` form POSTs) gate on `Depends(_require_role(Role.APPROVER))` — `viewer` gets 403. Read endpoints (`GET /sagas`, `GET /sagas/{id}`, HTML inbox / browser / detail views) accept any authenticated role. Role assignment via `AGORA_CONSOLE_ROLES` (`alice:admin,bob:approver,charlie:viewer`). Empty roster falls back to `approver` (back-compat); unknown usernames in a non-empty roster get `viewer` (least-privilege). Single-user limit: Basic auth pins to one `AGORA_CONSOLE_USERNAME` — multi-user RBAC arrives with G-01 OIDC.

---

## 3. Sensitive operations

| Operation                                              | Sensitive because                                                                                                              |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Saga forward step (ROUTE / APPROVE / SHIP / RECEIVE / RETURN_ITEM / CONFIRM_RETURN / RENEW) | Drives ReShare wire calls; mutates saga state; some emit ILS-side circulation events. |
| Saga compensator (ROUTE-comp / APPROVE-comp / SHIP-comp / RECEIVE-comp / RETURN_ITEM-comp / RENEW-comp)        | Issues recall / cancel / force-close at the supplier; some land patron-side ILS effects (NCIP check_in). |
| `/sagas/{id}/override` (resolve DISPUTED → CANCELLED / UNFILLED)                                                | Bypasses the normal lifecycle; only allowed from DISPUTED.                                                |
| `/sagas/{id}/discover`                                                                                          | Outbound CrossRef + SRU calls per request — burns third-party rate budget and may leak request metadata. Takes an optional body, so it is a CSRF-forgeable simple request; guarded by the `X-Agora-Admin` header (see enforcement table). |
| Patron PII writes (`request_payload['patron']`, `item_barcode`, NCIP `patron_id`)                              | Library circulation data is regulated (FERPA-adjacent in the US); leaks are reportable in many jurisdictions. |
| Outbox dispatch                                                                                                | Wire calls to ReShare / NCIP. `getattr` action injection (audit #4 / #28) closed via allow-lists.        |
| Audit log (`saga_event`)                                                                                       | Append-only by code convention; no DELETE path in app code. DB-level enforcement is a future trigger.    |
| LLM prompt rendering (RoutingAgent tie-breaker)                                                                | Audit #16 — candidate metadata flows verbatim into the prompt; injection via SRU peers closed with regex + repr-quoting + allow-listed raw keys + system-prompt directive. |

---

## 4. Enforcement table

For each (sensitive operation × auth role × surface) cell — what enforces it?

| Operation                                  | Role                | Surface          | Enforcement layer                                                                                                                                                                                                  |
| ------------------------------------------ | ------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST /requests`                           | `staff:<u>@<lib>`   | JSON API         | `_require_console_auth` → 401 on missing/wrong creds. Out-of-scope `requesting_library` → 403. pydantic `IllRequest` shape + max_length per field (audit #14). Server-side `request_id = uuid4()` (audit #20). |
| `POST /sagas/{id}/approve`                 | `staff:<u>@<lib>`   | JSON API         | `_require_console_auth` + `_assert_saga_in_scope`. Step in `_APPROVABLE_STEPS` allow-list. `actor` from principal (audit #21). Typed `StepExtras` rejects rogue keys (audit #15). Idempotency-key collision check (audit #22). |
| `POST /sagas/{id}/compensate`              | `staff:<u>@<lib>`   | JSON API         | Same as approve, plus deterministic comp idempotency key `comp-{step}-{saga_id}` (audit #5).                                                                                                                       |
| `POST /sagas/{id}/reject`                  | `staff:<u>@<lib>`   | JSON API         | Same auth + scope. Refuses on terminal saga or already-committed forward (audit #30).                                                                                                                              |
| `POST /sagas/{id}/override`                | `staff:<u>@<lib>`   | JSON API         | Same auth + scope. `target_state` in `_OVERRIDE_TARGETS` allow-list. Saga must be DISPUTED.                                                                                                                        |
| `POST /sagas/{id}/discover`                | `staff:<u>@<lib>`   | JSON API         | Same auth + scope. Refuses on terminal saga. Candidate / diagnostics list capped to 50 (audit #19). Requires `X-Agora-Admin: 1` header (CSRF guard on no-body JSON POST, review 2026-07-13; HTML forms can't set custom headers).                |
| `POST /sagas/{id}/renew`                   | `staff:<u>@<lib>`   | JSON API         | Same auth + scope. Saga must be RECEIVED. `extension_days` bounded 1..180.                                                                                                                                         |
| `GET /sagas`                               | `staff:<u>@<lib>`   | JSON API         | Same auth. Listing SQL-filters to principal library (audit #3 stopgap).                                                                                                                                            |
| `GET /sagas/{id}`                          | `staff:<u>@<lib>`   | JSON API         | Same auth + scope. PII filtering on response shape — deferred until #3 multi-principal lands (audit #26).                                                                                                          |
| HTML form actions (`/ui/sagas/.../*`)      | `staff:<u>@<lib>`   | HTML form        | Same auth dependency on every route. CSRF middleware double-submit cookie when `AGORA_CSRF_ENABLED=true` (audit #8).                                                                                               |
| Patron view (`/portal/requests/{saga_id}`) | Patron (HMAC token) | HTML             | HMAC over (saga_id, patron_id) when `AGORA_PORTAL_SIGNING_KEY` set + saga's stored `patron_id` matches query param (audit #2). 404 on every failure mode — no oracle.                                              |
| Outbox dispatch                            | `agent:outbox-worker` | Internal       | Action allow-list `_RESHARE_ACTIONS` / `_NCIP_ACTIONS` (audit #4 / #28). Lease-race guard `outbox_claim_still_ours` (audit #12). Fail-fast for known-failing actions (audit #35).                                  |
| Saga ledger append                         | All                 | Internal         | `UNIQUE(idempotency_key)` at DB layer is the load-bearing replay-safety primitive (mirrored in `idempotency_key` collision validation — audit #22). Terminal-state guard refuses ANY state-changing event regardless of kind (review 2026-07-13; sole carve-out: RESOLVE OBSERVATION DISPUTED→CANCELLED/UNFILLED for `/override`).           |
| ReShare HTTP call                          | `agent:outbox-worker` | Outbound HTTP  | `httpx.AsyncClient(verify=True)` (system CA bundle). Tenacity retry on 5xx/network only, NOT on 4xx. 4xx body redacted in ClientError (audit #7). Okapi token expires + proactively refreshes (audit #11).         |
| SRU XML parse                              | `agent:discovery`   | Outbound HTTP   | `SAFE_XML_PARSER` (audit #6 / #18) — `resolve_entities=False, no_network=True, huge_tree=False`.                                                                                                                  |
| LLM tie-breaker prompt                     | `agent:routing-llm-tiebreaker` | Internal | `HolderCandidate.symbol` regex (audit #16) + repr-quoted rendering + allow-listed raw keys + 256-char per-value cap + system-prompt attack-resistance directive.                                                |

Acceptable enforcement values for a FastAPI/SQLAlchemy stack:

- pydantic schema with strict types + `Field(constraints)` at the route boundary
- FastAPI `Depends(_require_console_auth)` + `_assert_saga_in_scope` for tenant scoping
- SQLAlchemy `UniqueConstraint` / `NOT NULL` / `ForeignKey` at table-create
- Alembic migration adding an index or constraint
- Application-layer state-machine wrapper (`Coordinator.run_forward` / `run_compensator` require a committed gate AND a legal current→next transition per `FORWARD_STEP_ALLOWED_STATES` / `COMPENSATOR_ALLOWED_STATES`, raising `IllegalTransitionError`→409; gates are single-use, consumed by any later FORWARD for the step — review 2026-07-13; direct ledger writes forbidden by convention)
- Saga step + compensator pair (every forward step has a paired compensator registered via `build_registry`)

---

## 5. CI checks

| Invariant                                          | CI check                                                                                | Status                                                                                                          |
| -------------------------------------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Strict mypy                                        | `mypy --strict` over `src/` and `tests/`                                                | clean (this sprint enforced strict typing through every audit fix)                                              |
| Linting                                            | `ruff check src tests`                                                                  | clean                                                                                                           |
| Security scan                                      | `make audit` (bandit + pip-audit + detect-secrets)                                      | clean                                                                                                           |
| Test suite (full)                                  | 665 tests + 6 postgres-only skipped                                                     | green                                                                                                           |
| Alembic forward + roundtrip + ORM-vs-DB autogenerate | `tests/test_alembic_postgres.py` against `postgres:15-alpine` in `.github/workflows/postgres-tests.yml` | green (gated on `AGORA_TEST_DB_URL` locally)                                                                    |
| Routing rules-engine floor                          | `.github/workflows/routing-eval-floor.yml` against `evals/routing/baseline-rules.json` | green (rules: top-1 0.8000 / Spearman 0.5556)                                                                   |
| `.env.example` ↔ `Settings` ↔ runbook env-var table symmetry | `tests/test_config.py` (6 cases)                                              | green; SecretStr-aware                                                                                          |
| Doc-count drift (test count, ADR count)            | `tests/test_doc_counts.py` against `scripts/sync_doc_counts.py`                         | green                                                                                                           |
| All saga forward steps have a paired compensator   | `tests/test_steps.py` registry walk                                                     | green                                                                                                           |
| All state transitions go through the coordinator   | Code-grep convention + property tests in `tests/test_property_saga.py`                  | green; no other ledger-write callers in `src/agora/`                                                            |

---

## 6. Known-gap registry

| Gap                                                                     | Severity | Issue / Note                                                                                                                                                          | Target close                                                                                       |
| ----------------------------------------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Audit #26 — PII field filtering on cross-library `GET /sagas/{id}`     | MEDIUM   | Deferred until multi-principal auth lands (depends on #3 follow-up). Today scoped principals get 403 entirely; partial-redaction model needs role definitions.        | After ADR-0018 multi-principal follow-up.                                                          |
| Audit #11/#13 — FOLIO `/authn/login-with-expiry` live verification      | LOW      | OkapiAuth code change is documented per FOLIO docs but not probed against a live FOLIO instance. Tolerant body parsing falls back to legacy reactive-only refresh.    | When a sandbox tenant is available — see `NEXT_SESSION.md`.                                        |
| Audit #24 — TLS pinning beyond system CA bundle                          | MEDIUM   | Operator-responsibility: trusted CA is added to system bundle or `SSL_CERT_FILE`. Per-cert pinning beyond that requires a code-level change (out of scope as documented). | Documented in runbook § 9.1; revisit when threat model requires.                                  |
| Audit #32 — `RESHARE_BASE_URL` is operator-controlled                    | LOW      | Treat as trusted-deploy-config. Allow-list of expected schemes/hosts at startup is a future hardening if env injection is ever less-trusted-than-deploy.              | Documented in runbook § 9.2.                                                                       |
| Audit #39 — Jinja2 templates not audited                                 | LOW      | Out of scope for the audit-remediation sprint; relies on Jinja2's auto-escape for HTML safety. Separate review pass scheduled.                                         | Separate audit pass.                                                                               |
| ADR-0017 — `renew_request` sandbox-blocked                               | n/a      | Documented in ADR. Audit #35 dead-letters on attempt 1 instead of running 17 hours of retry storm.                                                                    | When mod-rs renewal action is verified against a live two-tenant sandbox.                          |
| ADR-0016 — production recall via ISO 18626 Cancel                        | n/a      | `manualClose` prototype force-close used today; ISO 18626 Cancel via `message` performAction is the production path.                                                  | When two-tenant sandbox confirms the wire path.                                                    |

---

## 7. Last audit

- **Date:** 2026-05-09
- **Audit type:** `/security-audit` deterministic LLM pass (researcher / hacker perspective)
- **Findings link:** `docs/security-audits/2026-05-09.md`
- **Triage status:** 36 of 42 closed in 7-batch sprint (commits b15ed9..29c36fb). Remaining: #24 / #32 / #39 documented as operator responsibilities or scoped-out follow-ups; #26 deferred until multi-principal auth lands (ADR-0018 follow-up).

Re-audit cadence: after every multi-PR sprint touching auth/saga/DB schema, before any production deploy, quarterly otherwise.
