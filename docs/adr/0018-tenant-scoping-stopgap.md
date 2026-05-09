# ADR-0018: Tenant Scoping — Single-Principal Stopgap

## Status

Accepted (2026-05-09) as a stopgap. Revisit when the prototype takes
on a second consortium tenant or when the auth model is upgraded
beyond HTTP Basic.

## Context

The 2026-05-09 security audit (`docs/security-audits/2026-05-09.md`,
finding #3) flagged that the API resolves every saga by `saga_id`
alone, with no enforcement that the calling principal has any
relationship to the saga's `requesting_library`. In a consortium
deployment with multiple library tenants, a staff member at library A
who learns library B's saga UUID can mutate it, dump its patron PII,
or force-resolve it into a terminal state.

The audit's recommended primary fix — multi-tenant auth with a
per-principal `library_symbol` claim — requires an authentication
model that doesn't currently exist. HTTP Basic is single-principal:
one console password, one identity. JWT / OAuth / mTLS would be the
right substrate, but introducing any of them is a project of its own.

The audit also recommended a stopgap: bind the single Basic-auth
principal to a configured library symbol and refuse cross-library
saga access at every endpoint. That bounds the blast radius in the
common deployment shape (one library, one console) without paying
the multi-tenant-auth tax up front.

## Decision

**Single-principal tenant scoping**, controlled by a new config field
`AGORA_CONSOLE_LIBRARY_SYMBOL`.

- When the env var is **empty** (default), no scoping is applied —
  authenticated callers can touch any saga. Preserves existing dev /
  test behaviour where the prototype is exercised against an
  unsegmented dataset.
- When the env var is **set** (production deployment), the
  Basic-auth principal binds to that library symbol. Every saga
  endpoint runs `_assert_saga_in_scope(saga, principal)` which raises
  `403 Forbidden` if the saga's
  `request_payload['requesting_library']['symbol']` does not match
  the principal's `library_symbol`. `GET /sagas` SQL-filters
  identically so the listing also excludes other libraries' rows.
  `POST /requests` rejects submissions whose `requesting_library`
  doesn't match the principal scope.

The `actor` recorded on every ledger event is sourced from the
principal (`f"staff:{username}@{library_symbol}"`), not from the
request body — closes the audit-trail forgery in finding #21.

## Consequences

### Positive

- **Closes the cross-library IDOR** (audit #3) for the common
  deployment shape: one console, one library, one set of credentials.
  The vast majority of small-consortium pilot deployments fit this
  shape.
- **Audit-log honesty**: `actor` is the authenticated principal, not
  whatever the request body claimed (audit #21).
- **Defense in depth**: even if a future code path forgets to call
  `_assert_saga_in_scope`, the `GET /sagas` SQL filter still hides
  cross-library rows, narrowing the discovery surface.
- **Backwards-compatible default**: empty env var = no scoping,
  existing tests + dev sessions don't need modification.

### Negative

- **Single-tenant by construction**. A deployment that wants to host
  two libraries needs two separate console passwords / processes — or
  the proper multi-tenant auth follow-up. We accept this for the
  prototype phase.
- **The principal's library symbol is a deploy-time constant**.
  Operators can't issue per-staff tokens that scope to different
  libraries. Real role / library mapping requires the JWT (or
  equivalent) follow-up.
- **`GET /sagas` filtering uses JSONB-path access** — works on both
  Postgres and SQLite, but adds a bit of query cost. Acceptable for
  the prototype's data sizes; an explicit `requesting_library_symbol`
  column with an index is the natural follow-up if scan cost shows
  up in profiling.

### Out of scope (follow-up work)

1. **Multi-principal auth**. The right shape is a JWT (or OIDC token)
   with a `library_symbol` claim, validated per-request. The
   `ConsolePrincipal` class introduced for this stopgap is the seam:
   the dependency function changes, the rest of the API stays.
2. **PII field filtering** (audit #26). After the multi-principal
   model lands, `SagaDetail` should redact `patron_id` and similar
   fields when the caller's library doesn't own the saga (or when
   the caller is not staff at that library). Deferred until role
   model is real.
3. **Audit-log retention / read-only enforcement**. Currently the
   ledger is append-only by code convention but nothing prevents an
   operator with DB access from modifying past events. A future
   compliance pass should add a Postgres trigger or a separate
   read-only audit-log database.

## Implementation pointer

- `src/agora/api/app.py` — `ConsolePrincipal`, `_require_console_auth`,
  `_assert_saga_in_scope`.
- `src/agora/config.py` — `console_library_symbol` field.
- `tests/test_api_auth.py` (new) — coverage for the scoped /
  unscoped paths.
