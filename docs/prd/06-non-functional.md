# PRD 06 — Non-Functional Requirements

## Performance (prototype targets)

- API p50 < 200ms, p99 < 1s on local dev hardware (excluding LLM calls).
- Saga ledger writes within a single transaction.
- Discovery SRU calls timeout at 5s per target; degrade to "unknown"
  status not failure.
- Agents that call LLMs may take seconds; UI renders pending state.

## Observability

- **Structured JSON logs** — stdlib `logging` with JSON formatter, fields
  `saga_id`, `step`, `agent`, `idempotency_key`, `actor`.
- **OpenTelemetry traces** — span per saga step, child spans per
  external call. Console exporter for prototype (no Cloud Trace).
- **Saga ledger doubles as audit log.** Every state change recorded.
- **Metrics** — Prometheus-style counters (later): saga state durations,
  agent recommendation latencies, ReShare error rate.

## Reliability

- Saga ledger is the source of truth. Loss of ephemeral process state
  is recoverable by replaying events.
- Outbox worker retries with exponential backoff (1s, 2s, 4s, ..., capped
  60s). After N=10 failures, dead-letter to a separate table for staff
  intervention.
- Crash mid-step: on restart, scan ledger for `outcome='pending'` rows
  older than threshold; mark as failed if no completion event arrives.

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

See `docs/adr/0008-fedramp-deferred.md` for the explicit decision and
boundary diagram placeholder.

## Out of scope

- Multi-region failover
- High availability
- DDoS protection
- WAF / IDS
- Penetration testing
- Real money / payment processing
