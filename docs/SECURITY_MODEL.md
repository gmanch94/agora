# SECURITY_MODEL.md

The threat model and per-invariant enforcement layer for Agora. Auto-loaded by Claude Code per `~/.claude/rules/security.md`. Update on every change to: auth provider, DB schema, API surface, file storage, role definitions.

**STATUS: scaffold — fill in before next material change to auth/API/DB.**

---

## 1. Auto-generated endpoint surfaces

Endpoints exposed by the stack that we did NOT explicitly write:

| Surface                        | Reachable with                              | Notes                                                                                              |
| ------------------------------ | ------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| FastAPI auto-docs (`/docs`, `/redoc`, `/openapi.json`) | Public unless gated      | FastAPI exposes these by default — fine for dev, surfaces full route schema in prod if not disabled. |
| (Add others — alembic CLI, asyncpg admin URLs, structlog destinations, etc.) | _fill_      | _fill_                                                                                             |

**Implication for FastAPI:** there's no auto-generated REST surface like PostgREST/Hasura. Attack surface = routes you write + DB credentials. The "single-path-of-control assumption" failure mode (per global security rules) applies less here, but the "app-layer-only validation" mode still applies — pydantic at the route boundary is necessary, but DB-level CHECK constraints / NOT NULL / FK should mirror critical invariants.

---

## 2. Auth roles / principals

(Fill in based on `agora.api.auth` and consortium model.)

| Role | Source | Bypass | Reachable from |
| ---- | ------ | ------ | -------------- |
| anonymous | _fill_ | n/a | _fill_ |
| library staff | _fill_ | n/a | _fill_ |
| consortium admin | _fill_ | n/a | _fill_ |
| service / system | _fill_ | n/a | _fill_ |

Document: human-approval default-deny semantics — every state transition needs a human approver. Enumerate the approver-role mapping per state in the saga.

---

## 3. Sensitive operations

ILL-domain candidates (fill in / extend):

- [ ] State transitions (Submitted → Routed → Approved → Shipped → Received → Returned + compensators) — who can advance each?
- [ ] Patron PII (borrower identity, contact info)
- [ ] Library credentials (FOLIO/ReShare API tokens)
- [ ] Audit log / saga history — append-only?
- [ ] OpenURL / SRU query strings — log retention + PII risk
- [ ] LLM prompt content — does it ever include patron PII?

---

## 4. Enforcement table

For each (sensitive operation × auth role × surface) cell — what enforces it? Empty cells = KNOWN GAPS.

| Operation | Auth role | Surface | Enforcement layer | Status |
| --------- | --------- | ------- | ----------------- | ------ |
|           |           |         |                   |        |

Acceptable enforcement values for a FastAPI/SQLAlchemy stack:

- pydantic schema with strict types + `Field(constraints)` at the route boundary
- FastAPI `Depends(get_current_user)` + role check (`require_role("admin")`)
- SQLAlchemy `CheckConstraint` / `NOT NULL` / `ForeignKey` at table-create
- Alembic migration adding a CHECK constraint or trigger
- Application-layer state-machine wrapper (state-transition function that all routes go through; direct DB writes forbidden by convention)
- Saga step + compensator pair (every forward step has a documented backward compensator that fires on failure)

---

## 5. CI checks

| Invariant | CI check | Status |
| --------- | -------- | ------ |
| Strict mypy | `mypy --strict` | aspirational per CLAUDE.md — not gating |
| Linting | `ruff check` | ✅ |
| Security scan | `make audit` (bandit + pip-audit + detect-secrets) | ✅ |
| Test suite | 503 unit tests + 6 postgres-only | ✅ |
| All state transitions go through the wrapper function | _fill — grep gate?_ | ❌ backlog |
| All saga forward steps have a paired compensator | _fill — registry-based check?_ | ❌ backlog |

---

## 6. Known-gap registry

| Gap | Severity | Issue / Note | Target close |
| --- | -------- | ------------ | ------------ |
| (Fill from §4 empty cells + §5 backlog rows) |  |  |  |

---

## 7. Last audit

- **Date:** _never_
- **Audit type:** _run `/security-audit` to get a baseline_
- **Findings link:**
- **Triage status:**

Re-audit cadence: after every multi-PR sprint touching auth/saga/DB schema, before any production deploy, quarterly otherwise.
