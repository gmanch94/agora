# PRD 06 — Non-Functional Requirements

> Last reviewed against code: 2026-05-07 (post PRs #100/#101/#116/#117 —
> no NFR-level shifts; refresh confirms targets/budgets still apply).

## Performance (prototype targets)

- API p50 < 200ms, p99 < 1s on local dev hardware (excluding LLM calls).
- Saga ledger writes within a single transaction.
- Discovery SRU calls timeout at 5s per target; degrade to "unknown"
  status not failure.
- Agents that call LLMs may take seconds; UI renders pending state.

## Observability

- **Structured JSON logs** — implemented via `structlog`
  (`src/agora/logging.py`). Saga ID, step, actor, idempotency key
  bound on every saga log line via `structlog.contextvars`. Key
  event names: `saga.forward.start` / `.committed` / `.failed`,
  `saga.compensator.start` / `.committed`, `outbox.delivered` /
  `.retry_scheduled` / `.dead_letter`, `saga.overdue_scan.complete`.
- **Saga ledger doubles as audit log.** Every state change recorded.
- **OpenTelemetry traces** — *planned, not yet implemented*. Span
  per saga step + child spans per external call is the design target.
- **Metrics** — *planned, not yet implemented*. Prometheus-style
  counters for saga state durations, agent recommendation latencies,
  outbox dead-letter rate.

## Reliability

- Saga ledger is the source of truth. Loss of ephemeral process state
  is recoverable by replaying events.
- **Outbox worker retries with exponential backoff** —
  `base_backoff_secs=60`, schedule = `now + base * 2**(attempts-1)`.
  After `OUTBOX_RETRY_MAX_ATTEMPTS` failures (default 10) the row
  is flipped to `dead_letter` for staff triage. **No explicit cap**
  on the per-attempt backoff today (cumulative window with defaults
  is ~17 hours). Implemented in `outbox_mark_failed`
  (`src/agora/saga/idempotency.py`).
- **Crash mid-step.** Forward steps that did not commit have no
  effect (the savepoint in `SagaLedger.append` rolls back); replay
  with the same idempotency key picks up where we left off if the
  caller persists the key, or generates a fresh attempt if not.
- **Stall detection** — `SAGA_STALL_TIMEOUT_SECS=600` is reserved
  for a future scanner that flags long-pending GATE rows; *not yet
  implemented*.

## Security (alignment notes — not implemented in prototype)

The prototype runs locally; production-grade controls deferred.
Documented for future migration:

- **Auth/authorization**: SAML/Shibboleth for staff (consortium SSO),
  OIDC for service accounts. PIV/CAC required for FedRAMP-authorized
  build.
- **Encryption**: TLS everywhere; FIPS 140-3 modules for FedRAMP path.
  Postgres at-rest encryption via cloud-managed keys.
- **Secrets**: dev uses `.env`; prod will use Secret Manager (GCP) or
  equivalent.
- **Audit**: saga ledger plus immutable cloud audit log (Cloud Audit
  Logs / equivalent) for FedRAMP path.
- **Data residency**: prototype is local. FedRAMP path requires
  Assured Workloads / GovCloud region.
- **Patron PII**: minimize at every layer; never log patron names in
  cleartext logs (use saga id refs).

## FedRAMP alignment (deferred)

Prototype documents which NIST 800-53 control families *would* apply,
without implementing:

- AC (access control), AU (audit), IA (identification & authentication),
  SC (system & communication protection), SI (system & information
  integrity), CM (configuration management), IR (incident response).

See `docs/adr/0007-fedramp-deferred.md` for the explicit decision
and boundary diagram placeholder.

## Out of scope

- Multi-region failover
- High availability
- DDoS protection
- WAF / IDS
- Penetration testing
- Real money / payment processing
