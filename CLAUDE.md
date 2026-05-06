# CLAUDE.md — Agora ILL Project Context

Agentic Inter-Library Loan (ILL) prototype wrapping FOLIO/ReShare for
the standards layer (ISO 18626, NCIP, SRU, OpenURL). Multi-library
**consortium** model. **Human approval at every state transition**
(default-deny autonomy). Research prototype — FedRAMP alignment-noted
only, not authorized.

Lifecycle: **Submitted → Routed → Approved → Shipped → Received →
Returned** with saga compensators paired to every forward step.

## Quick start

```bash
# Setup (one-time)
.venv/Scripts/python.exe -m pip install -e ".[dev]"

# Verify
.venv/Scripts/python.exe -m pytest tests/ -q              # 280 tests (+6 postgres-only)
.venv/Scripts/python.exe -m ruff check src tests          # lint
.venv/Scripts/python.exe -m mypy --strict                 # types
make audit                                                # bandit + pip-audit + detect-secrets
.venv/Scripts/python.exe -m agora.demos.happy_path        # end-to-end demo

# Serve API
.venv/Scripts/python.exe -m uvicorn agora.api.app:app --reload
```

## Stack

- Python 3.11+ (built/tested on 3.14.3)
- FastAPI + pydantic v2 + pydantic-settings
- SQLAlchemy 2.x async + asyncpg (Postgres) / aiosqlite (tests)
- Alembic migrations
- pytest + pytest-asyncio (`asyncio_mode = "auto"`) + Hypothesis
- ruff (lint+format), mypy (aspirational, not gating)
- Google ADK optional via `[adk]` extra
- structlog + tenacity + httpx + lxml + python-ulid

## Repo layout

```
agora/
├── prompts/build-agora.md       # Bootstrap prompt for fresh sessions
├── docs/
│   ├── prd/  (7 docs)           # Product requirements
│   ├── adr/  (16 docs)          # Architecture decisions
│   └── architecture.md          # Hand-drawn Mermaid diagram
├── alembic/versions/            # DB migrations
├── src/agora/
│   ├── agents/                  # 6 advisory agents (discovery, routing,
│   │                            #   policy, transaction, tracking,
│   │                            #   reconciliation)
│   ├── api/app.py               # FastAPI staff console
│   ├── clients/                 # ReShare / NCIP / SRU / OpenURL
│   ├── demos/happy_path.py      # End-to-end runnable demo
│   ├── models/                  # pydantic domain models
│   ├── saga/                    # ledger, coordinator, idempotency,
│   │                            #   flows (forward+compensator pairs)
│   ├── cli.py / config.py / logging.py
├── tests/                       # 280 tests (unit + property + e2e)
├── docker-compose.yml           # Postgres-only sandbox
├── Makefile / pyproject.toml
```

## Architecture invariants

**Saga ledger is source of truth.** `saga.current_state` is a
denormalised projection — never trust it over the event stream.

**Append always uses a savepoint** (`begin_nested()` in
`saga/ledger.py`). Duplicate idempotency-key insert MUST NOT roll back
the caller's outer transaction — only the savepoint.

**Forward step requires a committed gate event.** `Coordinator.run_forward`
raises `GateRequiredError` if no committed gate exists for the step.

**Idempotency keys are ULIDs** with semantic prefix
(`route_01HXY...`). UNIQUE constraint on `saga_event.idempotency_key`
makes replay safe — duplicate insert returns the prior row, not an
error.

**Agents are advisory.** They produce recommendations + rationale.
Staff click in the console commits the gate. Agent never auto-commits.

**Compensators run only against committed forward steps.** Look up via
`SagaLedger.find_committed_forward(step)` before issuing a compensator.

**ReShare is wrapped, not reimplemented.** ISO 18626 wire-level
correctness lives in mod-rs; we drive it via REST. Method names map to
ISO 18626 message types — see table in `clients/reshare.py`.

## Conventions

- All datetime is timezone-aware UTC (`datetime.now(UTC)`).
- Database UUID columns use `_PortableUUID` TypeDecorator (Postgres
  native UUID / SQLite CHAR(36)).
- BIGINT autoincrement PKs use `_bigint_pk()` helper (BigInteger on
  Postgres, Integer on SQLite — required for SQLite rowid).
- Idempotency keys created via `new_idempotency_key(prefix=...)`.
- New saga steps: define forward + compensator in `saga/flows.py` and
  register in `build_registry()`. Add to `StepName` enum in
  `models/lifecycle.py`.
- New ADRs: copy a recent template in `docs/adr/`, increment number,
  write Status / Context / Decision / Consequences sections.

## Known gaps (do not silently fix — flag to user)

- `HttpReShareClient` paths and action vocabulary verified against
  mod-rs master (UrlMappings.groovy, PatronRequestController.groovy,
  Actions.groovy, ModuleDescriptor-template.json — see module
  docstring). The FastAPI app wires `get_client()` from
  `agora.clients.reshare`, which returns `HttpReShareClient` when
  `RESHARE_BASE_URL` is set and `MockReShareClient` otherwise. Auth
  is HTTP Basic by default; setting `OKAPI_URL` switches to the
  FOLIO Okapi token flow via `OkapiAuth` (ADR-0013, PR #34).
  Sandbox probe (2026-05-06, `make reshare-probe` against mod-rs
  2.19.0-rc17) resolved most unknowns: (a) body shape confirmed —
  camelCase fields `title`, `author`, `isbn`,
  `requestingInstitutionSymbol`, `supplyingInstitutionSymbol`,
  `patronIdentifier`, `patronType`, `pickupLocation`, `neededBy`
  all accepted; (b) response: `id` is UUID reshare_id, `hrid`
  may be null, `state` is a dict with `.code`, `isoMessageId` and
  `supplyingAgencyId` absent from basic responses; (c) recall —
  confirmed no requester-initiated recall action exists in
  `Actions.groovy`; `recall_request` keeps its `ClientError` until
  an ADR resolves the compensate-SHIP path (ISO 18626 Cancel via
  `message` or `manualClose` force-close). One remaining gap:
  probe created a Responder-side record (`RES_IDLE`) because
  `supplyingInstitutionSymbol` was set; Requester-side creation
  flow (REQ_* states) is still unconfirmed against a real
  borrower-tenant. mod-rs does not honour `Idempotency-Key` —
  replay-safety lives in the saga ledger's UNIQUE constraint,
  not the wire.
- RoutingAgent uses a deterministic rules-only weighted-sum baseline
  + a pluggable `LlmTiebreaker` seam (PR-2a) + a real
  `AdkLlmTiebreaker` adapter (PR-2b) wired via the
  `agora.agents.factories.get_llm_tiebreaker()` factory. Default
  factory output is `None` (rules-only path) — the agent is
  byte-identical to PR-1 unless `AGORA_ROUTING_LLM_ENABLED=1` AND
  the caller passes `llm_tiebreaker=get_llm_tiebreaker()`. Adapter
  uses ADK `LlmAgent` + `InMemoryRunner` (ADR-0003), Gemini Flash by
  default (`AGORA_ROUTING_LLM_MODEL`), `temperature=0` pinned,
  structured output via `output_schema=TiebreakDecisionSchema` so
  JSON parsing is constrained-decoded. Per-call timeout via
  `asyncio.wait_for(timeout=AGORA_ROUTING_LLM_TIMEOUT_SECS)` — a
  stuck LLM raises, seam catches, rules-fallback diagnostic in the
  rationale. Lazy `google.adk` import inside `__init__` so a base
  install (no `[adk]` extra) doesn't crash on
  `import agora.agents.routing`. Prompt template separated into
  `src/agora/agents/routing_tiebreak_prompt.py` so prompt-wording
  iteration is a prompt-only diff.
  CI gate: `.github/workflows/routing-eval-floor.yml` runs the
  harness in `--rules-only --check-floor` mode (no GCP secrets in
  CI) — catches rules-engine regressions vs the committed
  `evals/routing/baseline-rules.json` (rules floor: top-1 0.8000 /
  Spearman 0.5556). PR-review catches LLM-quality regressions by
  reading the `baseline.json` diff. Current LLM-augmented baseline
  (post-#7c prompt + ε tuning, `gemini-2.5-flash`): top-1 **0.9500**
  (19/20), mean Spearman **0.8889**. Only `routing-015` misses —
  documented out-of-scope in ADR-0014 (rules top-2 gap 0.46, LLM
  never fires below ε). Four scenarios (`routing-013..016`) are
  deliberate rules-baseline misses encoding metadata-only signals
  (SLA tier, reciprocity, format affinity, on-time reliability).
  Three (013, 014, 016) have rules score gap 0.0 — true ties — and
  are in scope for the tie-breaker; LLM picks all three correctly
  post-#7c. The fourth (015) has gap 0.46 and stays **out of scope
  for the tie-breaker mechanism**. ε tightened from 0.05 → 0.03 in
  #7c so `routing-009` (gap 0.0467) skips the LLM — rules already
  get it right and PR-2b's first-cut LLM had picked the worse
  candidate at ε=0.05. Eval harness via `make eval-routing` (rules)
  or `python -m agora.evals.routing --llm` (LLM-augmented; needs
  ADC bound + `gcloud auth application-default set-quota-project
  <project>` + `aiplatform.googleapis.com` enabled + **Vertex AI
  Studio click-through enablement** on the project + the correct
  API model id — the Studio display label is NOT the API id; e.g.
  Studio shows "gemini-3.1-flash-lite-preview" but the public API
  takes `gemini-2.5-flash`. Rerun with **all** of
  `GOOGLE_GENAI_USE_VERTEXAI=true` +
  `GOOGLE_CLOUD_PROJECT=<project-id>` +
  `GOOGLE_CLOUD_LOCATION=us-central1` +
  `AGORA_ROUTING_LLM_ENABLED=1` +
  `AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash` +
  `AGORA_ROUTING_LLM_TIMEOUT_SECS=30`. Without
  `GOOGLE_GENAI_USE_VERTEXAI=true` the SDK silently falls back to
  public-Gemini API-key auth and 401s every call (the seam catches
  it and runs rules-only — output looks "successful" but with the
  rules baseline numbers). Config defaults are now `gemini-2.5-flash`
  and `AGORA_ROUTING_LLM_TIMEOUT_SECS=30.0` (bumped from 5s which
  was too tight for Gemini 2.5 cold-start).
  Failure paths in the seam (LLM raises / abstains / returns unknown
  symbol / times out) ALWAYS fall back to the rules pick + diagnostic;
  the agent never re-raises out to its caller (advisory-only invariant
  per ADR-0005).
- DiscoveryAgent has CrossRef + SRU client factories
  (`agora.clients.crossref.get_crossref_client`,
  `agora.clients.sru.get_sru_client`) gated on
  `AGORA_CROSSREF_ENABLED` / `AGORA_SRU_ENABLED` (default `false` →
  mock). Explicit boolean toggles rather than ReShare's URL-presence
  check because both clients ship with non-empty production URL
  defaults (`api.crossref.org`, `lx2.loc.gov`) — presence-checking
  would force http and break offline dev. The agent + both clients
  are constructed at `create_app()` top and stashed on
  `app.state.discovery`; the lifespan calls `aclose()` on both http
  pools at shutdown alongside ReShare. `SruClient` and
  `CrossrefClient` Protocols both declare `aclose() -> None` so
  mocks no-op cleanly under typing. **Endpoint wired (8c):**
  `POST /sagas/{id}/discover` runs DiscoveryAgent against the saga's
  stored request and writes a single OBSERVATION event anchored on
  `StepName.ROUTE` (the step the candidates feed) with kind
  `"discovery"` in the payload. Saga state is unchanged — staff
  still commits a ROUTE gate via `/approve` to lock in the supplier.
  Idempotency keys are fresh ULIDs (`discovery-{ULID}`) so each
  invocation produces a distinct event — re-runs are intentional
  (citation edits, SRU index refresh). Returns 404 on unknown saga,
  409 on terminal state. `consortium_members` is sourced from
  `AGORA_CONSORTIUM_MEMBERS` (comma-separated agency symbols) via
  `Settings.consortium_members` (#56) — empty default preserves the
  pre-PR behaviour where no candidate was flagged in-consortium.
  Tokenization strips whitespace, de-dupes, and tolerates trailing
  commas. The env-var is the prototype's stand-in until a real
  consortium-roster source-of-truth lands; structured shape (per-tenant
  rosters, time-bounded membership) is a future migration. WorldCat
  sandbox still unimplemented (paid API, future).
- NCIP fan-out is wired on RECEIVE and RETURN forwards
  (fire-and-forget, borrower-side ILS): `receive_forward` emits a
  `target="ncip"` `check_out` intent (re-anchored from SHIP — see
  below), `return_forward` emits a `check_in` intent paired with its
  reshare `confirm_return`. NCIP outcomes do **not** gate saga state
  — failure surfaces as a stuck outbox row for staff review, the
  saga continues. The NCIP row uses idempotency-key suffix `:ncip`
  on `ctx.idempotency_key` because `outbox.idempotency_key` is
  UNIQUE across all targets (see `saga/db.py`); RETURN's two intents
  share a base key with the reshare row taking the bare key and the
  NCIP row taking the suffix, RECEIVE's single NCIP intent uses the
  same suffix for convention. `item_id` resolution: prefer
  `ctx.request.item.item_barcode` (optional field on `ItemMetadata`,
  submitted at request creation) and fall back to `reshare_id` when
  absent — see `saga/flows.py` RECEIVE comment block.
  - **NCIP `check_out` is anchored on RECEIVE forward** (re-anchored
    from SHIP). Anchoring at borrower-receipt rather than
    supplier-shipment is the correct circulation-timing model: the
    patron's ILS record reflects the loan from the moment they
    physically take custody, not from supplier shipment. Trade-off:
    a saga whose patron never confirms receipt will never have a
    `check_out` dispatched; the TrackingAgent tier-3 watch
    (`receipt-unconfirmed-{saga_id}`, see below) surfaces this to
    staff. `due_at` still anchors to `shipped_at` (loan-period clock
    is a supplier-side commitment that starts at shipment, and an
    unconfirmed-receipt saga still needs an overdue threshold).
  - **SHIP compensator emits a single ReShare `recall_request`** in
    either branch (saga at SHIPPED or post-RECEIVE). The
    `current_state` check survives only as state-aware rationale
    text; functionally both branches enqueue the same recall. At
    SHIPPED no ILS loan was ever opened (RECEIVE forward never
    ran), at RECEIVED the patron physically holds the book so the
    loan correctly reflects current custody and the eventual return
    flow owns `check_in`. The earlier state-aware NCIP rollback
    (PR #37, idempotency-key suffix `:ncip-rollback`) compensated
    for an upstream design tension that the re-anchor removed —
    see `docs/lessons.md` § Saga / ledger.
  - **RECEIVE compensator stays ledger-only DISPUTED.** The forward
    now opens an ILS loan via `check_out`, but the compensator
    deliberately does *not* emit a paired `check_in` — the saga
    can't tell whether a receipt dispute is about non-receipt (loan
    should clear) or condition (loan should stay). Routes to
    DISPUTED for staff resolution; `POST /sagas/{id}/override`
    resolves DISPUTED → CANCELLED or UNFILLED (implemented). A
    state-aware compensator with ILS check_in logic is still future.
  - The NCIP HTTP/SOAP client itself remains a mock —
    `MockNcipClient` for prototype/tests; real `mod-ncip`
    integration is still future work.
- TrackingAgent: `OverdueScanner.run_forever` runs as a background
  task spawned from the FastAPI lifespan (asyncio task name
  `agora.tracking.scanner`; module is `agora.agents.tracking`),
  polling at `AGORA_TRACKING_SCAN_INTERVAL_SECS` (default 300s). Each
  pass scans `current_state == SHIPPED` sagas and writes deterministic
  OBSERVATION events — UNIQUE constraint absorbs replay. Three-tier
  emission, all advisory only (no outbox, no state change, no
  auto-compensator dispatch — ADR-0005):
    * **Tier-1** `overdue-{saga_id}` fires on the first scan past
      `due_at` (loan-clock time).
    * **Tier-2** `recall-proposed-{saga_id}` fires on the first scan
      where `days_overdue >= AGORA_TRACKING_RECALL_AFTER_DAYS`
      (default 14) and carries `suggested_action: "compensate_ship"`
      plus the `reshare_id` for the staff console to render as a CTA
      pointing at `POST /sagas/{id}/compensate`.
    * **Tier-3** `receipt-unconfirmed-{saga_id}` fires on the first
      scan where `now - shipped_at >= AGORA_TRACKING_UNCONFIRMED_RECEIPT_AFTER_DAYS`
      (default 7) and the saga is still at `SHIPPED` — i.e. the
      patron never confirmed RECEIVE. Tier-3 keys off transit time
      rather than loan-clock time and fires *independently* of
      tier-1/2 (a saga can be flagged "patron forgot to confirm"
      while `due_at` is still in the future). Carries no
      `suggested_action` field — staff console surfaces the advisory
      as a "chase patron" hint without an in-saga CTA. Closes the
      known-gap that PR #38 documented when re-anchoring NCIP
      `check_out` from SHIP to RECEIVE.

  Recorded `days_overdue` and `days_since_shipped` are point-in-time
  snapshots — UI computes "currently N days" from the base timestamp
  + render clock. Auto-recall and a dedicated RECALLING lifecycle
  state are explicit non-goals; staff still clicks. Disable via
  `AGORA_TRACKING_SCANNER_ENABLED=0`. Multi-scanner safe by
  construction: the three deterministic keys per saga collide on
  `UNIQUE(saga_event.idempotency_key)` and the second invocation gets
  the existing row back from `SagaLedger.append`. No outbox writes,
  no state changes — concurrent scans are wasteful (duplicate work)
  but not incorrect. No row-locking added.
- Outbox is wired into flows for every ReShare-touching step
  including APPROVE forward (ADR-0011 + ADR-0012). APPROVE forward
  is now pure: it returns an `OutboxIntent` for `send_request` and
  advances the saga to `LifecycleState.APPROVING`; the outbox
  worker drains the row, calls the supplier, and the projection
  callback (`make_reshare_on_success`) writes an OBSERVATION
  carrying `reshare_id` that advances the saga to `APPROVED`.
  Downstream SHIP/RETURN consume `reshare_id` via `_derive_extras`,
  which now reads APPROVE OBSERVATION events as well as FORWARD
  events. The compensator-during-APPROVING window (staff hits
  `/compensate` while the supplier ack is still pending) is
  rejected with a 400 — there is no `reshare_id` to cancel against.
  The projection runs **inside the same session** as
  `outbox_mark_delivered`, so the OBSERVATION write and the
  delivered flag commit atomically; a failed projection re-queues
  the row for retry without leaving the saga half-advanced. The
  API process spawns `OutboxWorker.run_forever` as an
  `asyncio.Task` from the FastAPI lifespan (`create_app`) via
  `_build_outbox_worker`, polling at
  `AGORA_OUTBOX_POLL_INTERVAL_SECS` (default 1.0s) and cancelled on
  shutdown. Disable with `AGORA_OUTBOX_WORKER_ENABLED=0`.
  Multi-worker safe on Postgres via `outbox_claim` (backlog #5):
  `SELECT ... FOR UPDATE SKIP LOCKED` acquires disjoint row sets,
  flips claimed rows to `status='in_flight'` with `claimed_at=now()`,
  and commits — concurrent workers can't double-deliver. Orphan
  recovery sweeps `in_flight` rows whose `claimed_at` is older than
  `claim_lease_secs` (default 600s) back to `pending` so a crashed
  worker doesn't strand rows. SQLite serializes writers naturally so
  the same code path works in tests; the `with_for_update` hint is
  only emitted on Postgres. Verified by
  `tests/test_outbox_concurrent_postgres.py`.
- `POST /sagas/{id}/approve` and `POST /sagas/{id}/compensate` are
  wired end-to-end (commit gate + run forward / run compensator in
  one transaction). Step inputs (`chosen_supplier`, `reshare_id`) are
  derived from prior committed forwards; the request body's `extras`
  field overrides where derivation is impossible (e.g. first ROUTE).
- Alembic migration is now exercised against a real `postgres:15-alpine`
  service container in `.github/workflows/postgres-tests.yml`. Three
  tests in `tests/test_alembic_postgres.py` cover (a) `upgrade head`
  succeeds, (b) `upgrade head -> downgrade base -> upgrade head`
  round-trips cleanly, (c) ORM metadata in `saga/db.py` matches the
  live migrated schema via `alembic.autogenerate.compare_metadata`
  with a thin filter that drops cosmetic noise (`modify_default`
  text-vs-FunctionElement, `modify_type` when types stringify the
  same). Tests skip locally unless `AGORA_TEST_DB_URL` is set; CI
  always runs them. SQLite tests still use `Base.metadata.create_all()`
  for boot speed.
- mypy `--strict` runs clean against `src/` AND `tests/` (configured
  in `pyproject.toml` with `files = ["src", "tests"]`). Package ships
  a `py.typed` marker so downstream consumers pick up the inline
  types.
- `pyproject.toml` declares `requires-python = ">=3.11"` but built on
  3.14.3.

## Test commands

```bash
.venv/Scripts/python.exe -m pytest tests/ -q                          # all tests
.venv/Scripts/python.exe -m pytest tests/test_coordinator.py -q       # one file
.venv/Scripts/python.exe -m pytest -k "contu" -q                      # by keyword
.venv/Scripts/python.exe -m pytest --hypothesis-show-statistics       # property tests
```

## Behavioural rules for Claude in this repo

- **Never auto-commit forward saga steps without a committed gate.**
  This breaks the human-in-loop invariant.
- **Never amend git commits unless the user explicitly asks.** Create
  a new commit instead.
- **Never skip hooks** (`--no-verify`, `--no-gpg-sign`) without
  explicit permission. (GPG signing is currently disabled —
  `commit.gpgsign=false` as of 2026-05-03 — so commits go through
  without pinentry prompts. If the user re-enables it, pinentry
  hangs become possible again.)
- **When adding DB columns or tables**, write a new Alembic revision
  in `alembic/versions/` AND update the ORM in `saga/db.py`. Do not
  rely on `create_all()` for production — only tests.
- **When extending the lifecycle**, update: `models/lifecycle.py`
  (`LifecycleState` + `StepName`), `saga/flows.py` (forward +
  compensator), `tests/test_coordinator.py` (happy path + gate-required),
  and the ISO 18626 mapping table in PRD `01-lifecycle-and-states.md`.
- **When adding a new `Settings` field**, three artifacts must agree:
  the field declaration in `src/agora/config.py`, a row in
  `.env.example`, and a row in the runbook env-var table at
  `docs/runbook.md` § 1.2. `tests/test_config.py` enforces the
  symmetry — six pytest cases cover keys (forward + reverse) and
  default values for both artifacts (PRs #59, #60, #65, #66). CI fails
  with the exact missing keys / mismatched defaults so the fix is
  mechanical.
- **Preserve advisory-agent contract**: agents return a recommendation
  object with `rationale` field. They never write to the saga ledger
  directly.
- **Standards conformance is non-negotiable on the wire**: any code
  that produces ISO 18626 XML must validate against the published XSD
  before we go live with real peers. Today we delegate this to ReShare
  — keep it that way unless an ADR says otherwise. The harness for
  ad-hoc + future-CI validation lives at
  `scripts/validate_iso18626.py` (PR #52); pair it with the
  `iso18626-validate` skill. Self-test fixtures under
  `tests/fixtures/iso18626/` exercise the validator on every PR; the
  real ISO 18626 v1.3 XSD is an opt-in cache step at
  `docs/standards/iso18626/iso18626-v1_3.xsd` (see that directory's
  README — the test path skips cleanly when absent).
- **Track lessons learned in `docs/lessons.md`.** When a PR finishes,
  ask: *did anything bite me that wasn't obvious from the spec?* If
  yes, append a dated paragraph to the relevant section, citing the
  PR/commit. Lessons are not ADRs — no decision is being made. They
  are concrete gotchas tied to a code location so the next session
  doesn't relearn them. See `docs/lessons.md` for format and existing
  entries.
