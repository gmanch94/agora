# ADR-0019: Staff Console RBAC Roles — `viewer` / `approver` / `admin`

## Status

Accepted (2026-05-19) as a single-user stopgap. Closes the code-side
of gap **G-02** from `docs/productionization.md`. The multi-user
follow-up arrives with OIDC (G-01) and an explicit per-principal
claim model.

## Context

`docs/productionization.md` § 4.2 lists G-02 (Authorisation) as a
**P0** gap blocking the Phase-1 pilot. Today every authenticated
console user can hit every endpoint — submit requests, commit gates,
fire compensators, override DISPUTED sagas. No role separation
exists.

The audit-2026-05-09 remediation sprint closed authentication (#1,
HTTP Basic on every endpoint) and tenant scoping (#3, single-library
binding via ADR-0018). Authorisation was deferred — `_require_console_auth`
returns a principal but never checks what that principal is allowed
to do.

A consortium pilot needs at minimum:
- **Read-only viewers** — student workers, intake staff, dashboards.
- **Approvers** — full circulation staff who commit gates.
- **Admins** — supervisors with future access to roster mgmt,
  dead-letter purge, etc.

`docs/productionization.md` § 4.2 also names a `relay` service
identity for outbox handlers, but the outbox worker runs in-process
(see ADR-0011) and authenticates implicitly through the asyncio
lifespan — no HTTP surface to gate.

## Decision

**Three RBAC tiers** — `viewer < approver < admin` — wired at the
FastAPI dependency layer.

### Implementation

1. **`Role` enum** in `src/agora/api/app.py` with a `.rank`
   ordering so dependency factories can compare numerically:
   ```python
   class Role(str, Enum):
       VIEWER = "viewer"
       APPROVER = "approver"
       ADMIN = "admin"
   ```

2. **`ConsolePrincipal.role`** — new dataclass field with default
   `Role.APPROVER`. The default preserves single-principal back-compat:
   existing deployments without an `AGORA_CONSOLE_ROLES` roster keep
   the lone console user at the approver tier.

3. **`AGORA_CONSOLE_ROLES`** — new env var, comma-separated
   `username:role` pairs. Empty default. Examples:
   - `alice:admin,bob:approver,charlie:viewer` — full roster.
   - `alice:viewer` — downgrade the single console user to read-only
     (pilot rollout pattern: temporarily lock writes during incident
     review without ripping the deployment apart).

4. **`_parse_console_roles`** — boot-time parser. Fails fast on
   malformed entries, unknown role tokens, duplicate usernames.

5. **`_resolve_role(username)`** — lookup helper inside `create_app`.
   - Empty roster → `APPROVER` (back-compat default).
   - Username in roster → assigned role.
   - Username absent from a non-empty roster → `VIEWER`
     (least-privilege fallback). A typo'd username gets the
     read-only role rather than silently elevating.

6. **`_require_role(minimum: Role)`** — FastAPI dependency factory.
   Wraps `_require_console_auth` so 401 still precedes 403.
   Constructs a `_checker` that 403s when `principal.role.rank < minimum.rank`.

7. **Endpoint gating.** Every mutating endpoint switched from
   `Depends(_require_console_auth)` to
   `Depends(_require_role(Role.APPROVER))`. Read-only endpoints
   (GET `/sagas`, GET `/sagas/{id}`, HTML inbox / browser / detail
   views, `/portal/*`) keep `_require_console_auth` and accept any
   authenticated role (incl. VIEWER).

   APPROVER-gated:
   - `POST /requests`
   - `POST /sagas/{id}/approve|compensate|reject|override|discover|renew`
   - `POST /ui/sagas/{id}/approve|compensate|reject|override|discover|renew`

   No `ADMIN`-only endpoints today — reserved for future
   roster-mgmt, dead-letter purge, retention-purge override.

### Authentication seam

Single-user limit: HTTP Basic still pins to one
`AGORA_CONSOLE_USERNAME`. Today the roster simply changes that user's
privilege tier. The G-01 OIDC follow-up will let each authenticated
principal carry its own username and the roster becomes a real RBAC
matrix.

## Consequences

### Positive

- **Closes Phase-1 entry blocker.** G-02 was P0; the prototype can
  now demonstrate role separation even before OIDC lands.
- **Least-privilege fallback.** Unknown usernames in a non-empty
  roster get `VIEWER`. A typo cannot accidentally grant
  approver-level access.
- **Fail-fast boot.** Bad config (typo'd role, duplicate username,
  missing separator) raises `ValueError` at `create_app()` time, not
  on the first 403 in production.
- **Back-compat.** Empty `AGORA_CONSOLE_ROLES` (the default) yields
  exactly the pre-G-02 behaviour. Existing tests, demos, and the
  happy-path script need zero changes.

### Negative / followup work

- **Single-user limit.** The Basic-auth check still pins to one
  username; only that username's role is configurable. Real
  multi-user RBAC needs G-01.
- **No `admin`-gated endpoint yet.** The ADMIN tier exists in the
  enum but isn't wired to a privileged operation. Retention-purge
  override (G-07) and roster mgmt are the first candidates.
- **Roster lives in an env var.** Production-scale role mgmt needs a
  durable backing store — IdP claims, a `staff_role` DB table, etc.
  For one-consortium pilots the env var is operationally fine; for
  Phase-2+ deployments it becomes a maintenance burden.

### CI / docs

- `tests/test_authz.py` — 16-case RBAC matrix (parser unit tests +
  HTTP-layer 403 / 200 cells across viewer / approver / admin /
  default).
- `.env.example` — documents the env var.
- `docs/runbook.md` § 1.2 — env-var table updated.
- `docs/SECURITY_MODEL.md` § 2 / § 4 — role surface + per-endpoint
  enforcement row.
- `docs/productionization.md` § 4.2 — G-02 marked Phase-0 complete
  (single-user stopgap caveat).

## Alternatives considered

1. **Wait for OIDC.** Rejected: blocks Phase-1 entry indefinitely on
   an unrelated dep (IdP procurement).
2. **Per-endpoint role list in a YAML file.** Rejected: adds a config
   file before there's a need for one. Inline `Depends(_require_role(...))`
   on each endpoint keeps the gate visible at the route definition.
3. **Use OAuth2 scopes via FastAPI's `SecurityScopes`.** Rejected as
   premature: HTTP Basic doesn't carry scopes; introducing the
   scopes layer without a token-based auth substrate is bookkeeping
   without benefit. Revisit with G-01.

## References

- `docs/productionization.md` § 4.2 (G-02)
- ADR-0018 (tenant scoping stopgap — the related single-principal
  hardening this builds on)
- ADR-0007 (FedRAMP deferred; the no-auth posture this and ADR-0018
  jointly supersede)
- `src/agora/api/app.py` `Role` / `_parse_console_roles` /
  `_require_role`
- `tests/test_authz.py`
