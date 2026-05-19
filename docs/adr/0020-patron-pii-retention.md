# ADR-0020: Patron PII Retention — Scrub After Terminal Window

## Status

Accepted (2026-05-19). Closes the code side of gap **G-07** from
`docs/productionization.md`. Operator-side decisions (per-jurisdiction
retention window selection, off-system audit log of scrub events,
ADMIN-only key rotation runbook) land alongside.

## Context

`docs/productionization.md` § 4.3 lists G-07 (Patron PII retention) as
a **P0** Phase-1 entry blocker. Today the saga ledger retains:

- `patron_id` (in `saga.request_payload['patron']`)
- `patron_type`
- `item_barcode` (in `saga.request_payload['item']`)
- `patron_email` (when portal magic-links land in a future PR)

These are borrower-identifying records. ALA model policy
([recommendation](https://www.ala.org/advocacy/intfreedom/librarybill/interpretations/privacy))
calls for destruction once the transaction completes and any
disputes are resolved. Many state library-record statutes are
stronger: California Govt Code 6267 protects circulation records;
Illinois 75 ILCS 70 prohibits disclosure; equivalents exist in most
US jurisdictions and analogues in UK / EU / CA. Live deployments
need a defensible retention story.

The saga ledger is **append-only** (`saga_event` UNIQUE constraints,
no DELETE path in app code). "Destruction" in this context means
in-place anonymisation of the PII fields while preserving the saga
lifecycle and event timeline — the data needed to audit the system
or revisit a routing decision stays; the data that identifies the
patron does not.

## Decision

**Time-based in-place scrub** of borrower-identifying fields driven
by a background scanner, plus admin-only DSAR endpoints for query +
immediate-forget.

### Retention policy

- **Window:** 90 days post-terminal-state (configurable via
  `AGORA_RETENTION_DAYS`).
- **Eligible states:** `RETURNED`, `CANCELLED`, `UNFILLED`. **DISPUTED
  is excluded** — a saga in DISPUTED has an open issue the staff has
  not resolved; scrubbing while live destroys the evidence needed to
  resolve it. Staff must `POST /sagas/{id}/override` the DISPUTED
  saga to CANCELLED / UNFILLED first; only then does it enter the
  retention window.
- **Eligibility clock:** `saga.updated_at` (last transition into the
  terminal state).

### Anonymisation contract

Each scrubbed saga has borrower fields replaced across **three
storage surfaces**:

1. `saga.request_payload` — top-level submission record.
2. `saga_event.payload` — every FORWARD / COMPENSATOR / OBSERVATION
   event for the saga (RECEIVE / RETURN forwards write
   `patron_id` into NCIP-intent payloads; OBSERVATION events from
   the tracking agent may also carry borrower data).
3. `outbox.payload` — queued-but-not-yet-delivered NCIP / ReShare
   intents for the saga.

Per surface:
- key `patron_id` whose value matches the cleartext id is replaced
  with `"scrubbed:<HMAC-SHA256(patron_id, salt)>"`.
- key `item_barcode` (any non-null value) is nulled.
- key `patron_email` (any non-null value) is nulled.

Deep walk via `_deep_scrub_json`; mutation is forced to flush via
`sqlalchemy.orm.attributes.flag_modified` because plain JSON columns
don't auto-track nested mutations.

Saga lifecycle, event timeline (kinds / steps / actors / outcomes),
supplier identity, and routing decisions are preserved — the data
needed to audit the saga or revisit a routing decision stays; the
data that identifies the patron does not. The `scrubbed:` prefix lets the staff
console and DSAR endpoints detect scrubbed status without consulting
the event timeline.

The HMAC fingerprint is **deterministic** — same `patron_id + salt`
always yields the same fingerprint. This lets a DSAR query against
cleartext find the patron's already-scrubbed rows. The salt
(`AGORA_PII_SCRUB_SALT`) prevents offline rainbow-table attacks on
the small (per-library) patron-id universe.

### Components

1. **`PatronScrubber`** in `src/agora/agents/retention.py` — applies
   the scrub to a single saga in-place + writes a
   `patron_scrubbed-{saga_id}` OBSERVATION event for audit-trail.
   Idempotent: replay hits the UNIQUE constraint on
   `saga_event.idempotency_key` and the `scrubbed:` prefix on the
   payload makes the second pass a no-op.

2. **`RetentionScanner`** — periodic background sweep, spawned from
   the FastAPI lifespan. Pattern mirrors `OverdueScanner`:
   `async with sessionmaker() as session`, query terminal sagas past
   the window, apply scrubber, commit transactionally. Bounded to
   500 sagas per tick to keep transactions short. Multi-scanner safe
   by construction (UNIQUE collision on the idempotency key).

3. **`fingerprint_patron(patron_id, salt) -> str`** — public helper
   used by the DSAR endpoints to compute the scrubbed-form sentinel
   for lookup.

4. **`RetentionConfigError`** — raised when scrubbing is invoked
   with an empty / too-short salt (`MIN_SCRUB_SALT_LEN = 32`).
   Production deployments MUST rotate a real 32-byte secret; the
   empty-or-weak-salt path fails closed at three layers:
   - `_fingerprint` raises on every scrub attempt.
   - `_require_scrub_salt` returns 503 on the DSAR endpoints.
   - `create_app` lifespan refuses to boot with
     `AGORA_RETENTION_ENABLED=true` + weak salt; a one-shot
     `RuntimeError` surfaces the misconfiguration loudly rather
     than letting the background scanner log-and-retry silently.

### DSAR endpoints (ADMIN-role-gated)

Both gated on `Depends(_require_role(Role.ADMIN))` — first
production user of the ADMIN tier reserved in ADR-0019.

- **`GET /admin/patrons/{patron_id}/sagas`** — lists every saga
  matching the patron (cleartext OR fingerprint), returns
  `{saga_id, current_state, scrubbed, updated_at}` per row.
- **`POST /admin/patrons/{patron_id}/forget`** — immediate scrub of
  matching sagas in eligible states, regardless of retention window.
  Returns partitioned arrays: `scrubbed`, `already_scrubbed`,
  `skipped_active` so the admin sees what couldn't be scrubbed
  (in-flight / DISPUTED) without re-querying.

Both endpoints surface `RetentionConfigError` as a `503 Service
Unavailable` via the `_require_scrub_salt` dependency. Operators
should never see a 5xx in this path under normal config — a 503 is
a configuration alarm.

### Configuration

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `AGORA_RETENTION_ENABLED` | `false` | Toggle the background scanner. Default off in dev. |
| `AGORA_RETENTION_DAYS` | `90` | Days post-terminal until scrub eligibility. |
| `AGORA_RETENTION_SCAN_INTERVAL_SECS` | `3600` | Sleep between sweeps. |
| `AGORA_PII_SCRUB_SALT` | `""` | HMAC salt. Empty = fail-closed. |

## Consequences

### Positive

- **Closes P0 Phase-1 entry blocker.** G-07 was the third of the
  three explicit pre-Phase-1 gates (per § 3, alongside G-01 OIDC
  and G-03 ReShare two-tenant).
- **Append-only ledger preserved.** No DELETE path added. Scrub is
  an in-place update + audit observation.
- **Idempotent + replay-safe.** Two scanners can race; the second
  loses on the UNIQUE constraint.
- **DSAR-queryable after scrub.** Deterministic fingerprint plus
  the cleartext-or-scrubbed JSON-path filter on the DSAR list lets
  staff resolve patron complaints months after the data has been
  anonymised.
- **Fail-closed on misconfiguration.** Empty salt → scrubber
  refuses → DSAR endpoints return 503 → operator sees the alarm.
- **First exercise of the ADMIN role tier.** Validates the seam
  reserved in ADR-0019.

### Negative / followup work

- **DISPUTED sagas accumulate PII.** Until staff resolves them via
  `/override`, DISPUTED sagas never enter the retention window. An
  ageing dashboard for DISPUTED would surface stale ones — out of
  scope for this PR.
- **Off-system audit log not yet wired.** The PATRON_SCRUBBED
  observation lands on the saga ledger (same DB). Compliance audits
  ultimately want an immutable off-system log; that's part of the
  G-08 audit-log-sink follow-up.
- **No DSAR delete-everything-now.** Forget scrubs PII but
  preserves the saga shape. A true "right to deletion" request that
  deletes the saga rows is policy-decision territory and library-
  record retention statutes generally REQUIRE preserving the
  circulation record sans PII. The current shape matches the law's
  intent.
- **No migration of historical sagas in the deployment cutover.**
  When `AGORA_RETENTION_ENABLED=true` flips, the scanner backfills
  the eligible historical sagas on the next tick. For a busy
  deployment with millions of terminal sagas, the 500-per-tick
  bound means hours of warm-up. The fix (parallel batch backfill) is
  trivial; out of scope today.

### CI / docs

- `tests/test_retention.py` — 15 cases (fingerprint, scrubber,
  scanner, idempotency, eligibility filter).
- `tests/test_admin_dsar.py` — 8 cases (RBAC gating, 503 on empty
  salt, list + forget happy path, partitioning, idempotency).
- `docs/SECURITY_MODEL.md` § 3 / § 4 — added retention surface +
  per-endpoint enforcement row.
- `docs/productionization.md` § 4.3 — G-07 marked Phase-0 closed.
- `.env.example`, `docs/runbook.md` § 1.2 — 4 new env vars.

## Alternatives considered

1. **Hard DELETE of patron-data columns.** Rejected: the
   library-record statutes generally require preserving the
   circulation transaction record sans PII (the system MUST be able
   to attest that loan X happened between borrower-fingerprint Y and
   supplier Z), not delete the row entirely.
2. **Cryptographic key destruction.** Encrypt patron data at write,
   destroy the per-saga key at scrub time. Cleaner cryptographically
   but ties Agora to an out-of-band key store (KMS / Vault) and
   forecloses cross-saga DSAR queries. Revisit when G-08 (audit log
   sink) lands a KMS dependency anyway.
3. **No-op until G-08.** Rejected: G-07 and G-08 are independent.
   Shipping scrub without an off-system audit log is strictly better
   than shipping neither — the saga ledger already records the
   scrub event, and a later G-08 PR forwards it off-system.
4. **DELETE OBSERVATION events instead of in-place mutate.** Append a
   redaction event; readers reconstruct the anonymised view. Cleaner
   audit story but every reader (staff console, portal, agents, ML
   eval) has to honour the redaction. In-place mutation makes the
   anonymisation visible at the storage layer — defensive in depth.

## References

- `docs/productionization.md` § 4.3 (G-07)
- ALA Library Privacy Guidelines
- California Govt Code 6267; Illinois 75 ILCS 70
- ADR-0005 (append-only saga ledger)
- ADR-0019 (RBAC roles; ADMIN tier this exercises)
- `src/agora/agents/retention.py`
- `tests/test_retention.py`, `tests/test_admin_dsar.py`
