# Agora — Agentic Inter-Library Loan (ILL) System

> Research prototype. Multi-library consortium. Agents over FOLIO/ReShare.
> Saga + idempotency. Human-approval at every state transition.

> Last reviewed against code: 2026-05-09 (audit-remediation sprint —
> commits b15ed9..a6eb6fa close 36 of 42 findings from the 2026-05-09
> security audit: JSON API auth + tenant-scoping stopgap (ADR-0018);
> patron-portal HMAC; OkapiAuth proactive expiry refresh; outbox
> action allow-list + lease-race verification + deterministic
> compensator key + fail-fast renew; LLM tie-breaker prompt-injection
> guard; CSRF / rate-limit / HTTPS / security-headers middleware;
> typed StepExtras + IllRequest field-level max_length; SecretStr
> credentials; SAFE_XML_PARSER for SRU + NCIP; tracking-scanner
> race mitigation; JSONB GIN index; jitter on poll loops; Jinja
> XSS-guard CI script. Earlier baselines: PRs #100/#101/#102/#116/#117/#134
> shipped DiscoveryAgent consortium-member fallback, NCIP HTTP smoke
> test, RENEW saga step + JSON + HTMX endpoints, read-only patron
> portal, and strict-grade post-merge bug fixes; PRs #41-#93 shipped
> the HTMX/Jinja2 staff console (ADR-0015), NCIP item-barcode,
> override endpoint, and the `sync-doc-counts` pytest gate.

## What this is

Agora is a research prototype that puts a multi-agent orchestration layer
on top of standards-compliant ILL infrastructure. The lifecycle
**Submitted → Routed → Approved → Shipped → Received → Returned** is
driven by agents that produce *recommendations*; humans approve every
transition. Every state change is recorded in an event-sourced saga
ledger with paired forward + compensator operations and ULID-keyed
idempotency.

The standards plumbing (ISO 18626 wire protocol, NCIP, etc.) is
delegated to FOLIO's `mod-rs` / ReShare and `mod-ncip`. Agora does not
reimplement them.

## Status

**Working prototype.** End-to-end demo runs via `make demo`
(`agora.demos.happy_path`). **610 tests** green (+6 postgres-only in CI).
Saga + outbox + APPROVING-via-outbox (ADR-0012), multi-worker outbox
safety (`SELECT … FOR UPDATE SKIP LOCKED`), TrackingAgent three-tier
overdue scanner (overdue / recall-proposed / receipt-unconfirmed) wired
into the FastAPI lifespan, NCIP fan-out on RECEIVE / RETURN forwards,
DiscoveryAgent with CrossRef + SRU clients + consortium-member fallback
when SRU yields no holdings (`POST /sagas/{id}/discover` endpoint),
RoutingAgent LLM tie-breaker (ADR-0014, top-1 0.95 against the
20-scenario eval), RENEW saga step (PR #116, JSON + HTMX endpoints,
`renew_request` outbox intent — sandbox-blocked on `HttpReShareClient`
per ADR-0017; mock succeeds), read-only patron portal at `/portal/*`
(PR #117, status + due date + renewal count), ISO 18626 XSD validation
harness, Alembic-on-real-Postgres, and the 2026-05-09 audit-remediation
sprint hardening (JSON API Basic auth, tenant scoping stopgap per
ADR-0018, patron portal HMAC, OkapiAuth proactive expiry refresh,
outbox action allow-list + lease-race guard + fail-fast renew, LLM
prompt injection guard, CSRF + rate-limit + HTTPS + security-headers
middleware, SecretStr credentials, SAFE_XML_PARSER, tracking-scanner
race mitigation, JSONB GIN index, Jinja XSS-guard CI script) all
shipped. CI gates: bandit + pip-audit + detect-secrets, pytest + ruff +
mypy --strict, alembic+ORM parity against `postgres:15-alpine`,
routing-eval rules-floor regression check, Jinja autoescape-bypass
guard.

See `docs/prd/` for product requirements, `docs/adr/` for architecture
decisions (18 ADRs through 0018), `docs/architecture.md` for the
hand-drawn diagrams, `docs/runbook.md` for operations, `docs/solution.md`
for the solution doc, `docs/lessons.md` for accumulated gotchas, and
`prompts/build-agora.md` to bootstrap a fresh dev session.

## Quick layout

```
agora/
├── prompts/             # Project bootstrap prompt
├── docs/
│   ├── prd/             # Product requirements (00-06)
│   ├── adr/             # Architecture decisions (0001-0018)
│   ├── architecture.md  # Hand-drawn Mermaid diagrams
│   ├── runbook.md       # Operations / on-call notes
│   ├── solution.md      # Solution overview
│   ├── lessons.md       # Accumulated gotchas (append-only)
│   └── standards/       # ISO 18626 XSD validator + fixtures
├── evals/routing/       # Routing tie-breaker eval scenarios + baselines
├── alembic/versions/    # DB migrations
├── scripts/             # validate_iso18626.py + tooling
├── src/agora/
│   ├── agents/          # Discovery, Routing, Policy, Transaction,
│   │                    #   Tracking (+ OverdueScanner), Reconciliation,
│   │                    #   AdkLlmTiebreaker (ADR-0014)
│   ├── saga/            # Coordinator, ledger, flows (forward+
│   │                    #   compensator pairs), steps, idempotency,
│   │                    #   outbox, db
│   ├── clients/         # ReShare (+ OkapiAuth), NCIP, SRU, CrossRef,
│   │                    #   OpenURL
│   ├── api/             # FastAPI staff console + lifespan
│   ├── demos/           # happy_path runnable end-to-end demo
│   ├── evals/           # Routing eval harness (run via make eval-routing)
│   ├── models/          # pydantic schemas (ISO 18626 subset)
│   ├── config.py / cli.py / logging.py / py.typed
├── tests/               # 610 unit + property + e2e (+6 postgres-only)
├── .github/workflows/   # audit.yml, postgres-tests.yml, triple-gate.yml,
│                        #   routing-eval-floor.yml
├── docker-compose.yml   # Postgres-only sandbox today
├── Makefile
└── pyproject.toml
```

## Getting started

```bash
# 1. Set up env
cp .env.example .env
# (edit .env if needed; defaults work for local dev)

# 2. Install
make install

# 3. Bring up Postgres
make up

# 4. Run migrations
make migrate

# 5. Run tests
make test

# 6. Run the API
make api
# → http://localhost:8000/docs
```

## Standards & specs

| Standard | Role | Implementation strategy |
|---|---|---|
| ISO 18626:2021 | Peer-to-peer ILL messaging | Delegated to ReShare `mod-rs`; XSD validation harness in `scripts/validate_iso18626.py` for the day Agora emits XML directly |
| NCIP / Z39.83 | Library ↔ ILS circulation | `HttpNcipClient` (source-review-only, PR #98/#99); `MockNcipClient` default; live mod-ncip probe pending |
| SRU | Catalog discovery | Direct HTTP client (`agora.clients.sru`) |
| CrossRef REST | DOI → bibliographic record | Direct HTTP client (`agora.clients.crossref`) |
| OpenURL | Citation resolution | Pure-Python parser |
| Z39.50 (binary) | Legacy catalog discovery | **Not implemented** (see ADR-0006) |
| FedRAMP | US gov cloud security | **Alignment-noted only** (see ADR-0007) |

## Documentation index

- [PRD 00 — Overview](docs/prd/00-overview.md)
- [PRD 01 — Lifecycle & State Machine](docs/prd/01-lifecycle-and-states.md)
- [PRD 02 — Agents](docs/prd/02-agents.md)
- [PRD 03 — Saga & Idempotency](docs/prd/03-saga-and-idempotency.md)
- [PRD 04 — Discovery](docs/prd/04-discovery.md)
- [PRD 05 — Staff Console](docs/prd/05-staff-console.md)
- [PRD 06 — Non-Functional Requirements](docs/prd/06-non-functional.md)
- [Architecture diagrams](docs/architecture.md)
- [Runbook](docs/runbook.md)
- [Solution overview](docs/solution.md)
- [Lessons learned](docs/lessons.md)
- [ADRs](docs/adr/) — 18 records, latest are
  [ADR-0016 (compensate-ship via manualClose)](docs/adr/0016-compensate-ship-manualclose.md),
  [ADR-0017 (renew_request sandbox gap)](docs/adr/0017-renew-request-sandbox-gap.md),
  and [ADR-0018 (tenant-scoping stopgap)](docs/adr/0018-tenant-scoping-stopgap.md)
- [Bootstrap prompt](prompts/build-agora.md)

## License

Apache 2.0. See [LICENSE](LICENSE).
