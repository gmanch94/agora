# PRD 05 — Staff Console

> Last reviewed against code: 2026-05-07 (post PRs #116/#117 — RENEW saga
> step + UI form (#116); read-only patron portal under `/portal/*` (#117).
> Earlier baseline: PRs #80/#82/#84/#90/#92/#93 — UI shell, override HTMX
> form, saga browser).

The staff console is the **primary UI in the prototype**. It is the
human-in-the-loop surface for every state transition. A separate
**read-only patron portal** (PR #117) lives under `/portal/*` —
patrons cannot mutate state through it (no write endpoints; no patron
self-serve renewal).

**UI shell status:** *shipped (slices 1–3)*. HTMX + Jinja2 server-rendered
console (ADR-0015). Inbox (`GET /`), detail view (`GET /sagas/{id}/view`),
approve / reject / compensate form endpoints, and a discover-candidates
HTMX panel are live. Saga browser (filter by state/library/date) is live (`GET /browser`,
PR #93).

## Users

ILL borrowing staff and lending staff at consortium member libraries.
Single-tenant authentication (HTTP basic / dev token) acceptable for
prototype.

## Surfaces

### Inbox view
Pending agent recommendations awaiting staff approval.

For each row:
- Saga id (short)
- Patron name + library
- Item title (1 line)
- Current state
- Agent recommendation summary (1 line)
- Action buttons: **Approve** / **Reject** / **View details**

### Detail view
Single saga, full reasoning trace.

- Lifecycle timeline (rendered from `saga_event` projection)
- Current agent recommendation:
  - Step (e.g. "Route to supplier ABCDE")
  - Rationale (≤3 sentences from the agent)
  - Inputs (collapsible: full request, candidate list, policy flags)
  - Idempotency key (debug aid)
- Action buttons: Approve, Reject (with reason), Compensate, Override (DISPUTED only —
  `POST /sagas/{id}/override` JSON API + `/ui/sagas/{id}/override` HTMX form both implemented),
  Renew (RECEIVED only — `POST /sagas/{id}/renew` JSON API +
  `/ui/sagas/{id}/renew` HTMX form, PR #116; the detail view sets
  `can_renew = (state == RECEIVED)` to gate the button)

### Saga browser
Filter by state, library, date. Read-only. Useful for demo + debug.

### Patron portal (PR #117)
Read-only status surface for patrons. Patron enters their `patron_id`
on `/portal` and sees their list of requests + per-request detail
(item, current state, due date, renewal count, terminal flag, event
timeline labelled in patron-friendly language via `_patron_event_label`).
**No write endpoints** — patrons cannot submit, cancel, renew, or
override through the portal; staff still drive every state change.

**Privacy posture (post-2026-05-09 audit, audit #2).** Two modes,
toggled by `AGORA_PORTAL_SIGNING_KEY`:

- **Empty key (dev mode):** form-entry path active, the **saga UUID
  is the secret token**, and `patron_id` is a UX label echoed into
  the page rather than an access gate (a gate on patron-id while
  `/portal/requests` accepts arbitrary IDs would be false reassurance).
  Use this only for local development.
- **Key set (production mode):** every `/portal/*` endpoint requires
  a `?token=<HMAC>` query parameter. The list view signs `patron_id`;
  the detail view signs `(saga_id, patron_id)` AND verifies that
  the saga's stored `patron_id` matches the query param (so a token
  issued for one patron cannot enumerate another patron's sagas).
  Tokens are minted out-of-band — typically emailed to the patron —
  so guessing or harvesting a `patron_id` alone does not unlock
  circulation history. 404 on every failure path; no oracle for
  token validity.

Long-term federal-grade auth (SAML/Shibboleth, PIV/CAC) is still
deferred per [ADR-0007](../adr/0007-fedramp-deferred.md); the HMAC
gate is the prototype-grade interim. See `mint_portal_token` /
`verify_portal_token` in `src/agora/api/app.py`.

**Due date semantics.** `_portal_due_date` walks committed events in
`seq` order: `forward.ship.due_at` seeds the value, each
`forward.renew` pushes its `new_due_at` onto a stack, each
`compensator.renew` pops the most recent renewal. Without the pop, a
cancelled renewal would leave the portal showing the rolled-back due
date.

## Backend endpoints (FastAPI)

Implemented today (`src/agora/api/app.py`), no `/api` prefix:

```
GET    /health                         # liveness + version
POST   /requests                       # patron submit
GET    /sagas                          # list active + recent sagas
GET    /sagas/{id}                     # full event timeline (JSON)
POST   /sagas/{id}/approve             # commit gate AND run forward in one tx
POST   /sagas/{id}/reject              # mark pending gate failed
POST   /sagas/{id}/compensate          # run compensator for committed forward
POST   /sagas/{id}/discover            # run DiscoveryAgent; ROUTE-anchored OBSERVATION (#53)
POST   /sagas/{id}/override            # resolve DISPUTED saga → CANCELLED or UNFILLED
POST   /sagas/{id}/renew               # commit RENEW gate + run forward (RECEIVED → RECEIVED, PR #116)

# Staff console UI (server-rendered HTML, ADR-0015)
GET    /                               # inbox — all active sagas
GET    /browser                        # saga browser — filter by state/library/date (PR #93)
GET    /sagas/{id}/view                # detail view with event timeline
POST   /ui/sagas/{id}/approve          # form submit → approve; 303 redirect to detail
POST   /ui/sagas/{id}/reject           # form submit → reject;  303 redirect to detail
POST   /ui/sagas/{id}/compensate       # form submit → compensate; 303 redirect to detail
POST   /ui/sagas/{id}/discover         # HTMX partial → _discover_panel.html fragment
POST   /ui/sagas/{id}/override         # form submit → resolve DISPUTED; 303 redirect to detail (PR #92)
POST   /ui/sagas/{id}/renew            # form submit → renew; 303 redirect to detail (PR #116)

# Patron portal — read-only (server-rendered HTML, PR #117)
GET    /portal                         # patron-id lookup form (landing)
GET    /portal/requests                # ?patron_id=… [&token=…]  list this patron's sagas (most recent 200; token required when AGORA_PORTAL_SIGNING_KEY is set)
GET    /portal/requests/{saga_id}      # ?patron_id=… [&token=…]  saga detail; with key set, HMAC over (saga_id, patron_id) AND stored patron_id must match query param
```

**Idempotency keys are minted server-side** — every saga event
generated by the API gets a fresh ULID via `new_idempotency_key()`.
The `Idempotency-Key` request header is **not currently honoured**
(safe to retry the same request body without one because the API
is read-or-mutate based on saga state, not on caller-supplied keys).

**Override endpoint** (`POST /sagas/{id}/override`): **implemented
(narrowly scoped)**. Resolves a `DISPUTED` saga directly to
`CANCELLED` or `UNFILLED` — the two sensible staff-resolution
outcomes when a receipt dispute cannot be settled via normal
compensators. Writes an `OBSERVATION` event (`step=resolve`,
`outcome=committed`) directly to the ledger; `saga.current_state`
advances atomically. No outbox dispatch — any open ILS loans must
be settled out-of-band by staff (see `saga/flows.py` § RECEIVE
compensator for rationale). Broader override (arbitrary state
forcing, PolicyAgent hard-fail integration) remains out of scope.

**Auth (post-2026-05-09 audit, #1 / #3 / #21).** HTTP Basic gates
**every** route now (HTML console + JSON API + patron portal —
the trusted-network assumption from the previous baseline is
superseded by [ADR-0018](../adr/0018-tenant-scoping-stopgap.md)).
Set `AGORA_CONSOLE_USERNAME` / `AGORA_CONSOLE_PASSWORD` to enable;
empty password keeps auth off for local dev. With
`AGORA_CONSOLE_LIBRARY_SYMBOL` set the principal binds to a single
library and saga endpoints 403 on cross-library access (`GET /sagas`
also SQL-filters); single-tenant by construction, multi-principal
auth is the ADR-0018 follow-up. The `actor` recorded on every ledger
event is sourced from the authenticated principal, not from the
request body. `/health` is the only unauthenticated route. Full
FedRAMP-grade auth (SAML/Shibboleth, PIV/CAC) remains deferred per
[ADR-0007](../adr/0007-fedramp-deferred.md). CSRF + rate-limit +
HTTPS redirect + security-headers middleware all toggleable via
the audit-batch-5 env vars (`AGORA_CSRF_ENABLED`,
`AGORA_RATE_LIMIT_ENABLED`).

## UX principles

- **Reasoning before action.** Recommendation summary always visible
  before the approve button.
- **No silent autopilot.** Staff must type a rationale for every approve /
  reject / compensate action; rationale persists in the saga ledger.
- **Reproducibility.** Every action has an event in the ledger; UI is a
  projection, never authoritative.

## Implementation note (prototype)

API-first approach used. UI is server-rendered HTMX + Jinja2 (ADR-0015) —
no Node toolchain, no build step. Visual polish deferred; correctness +
traceability is the demo target. HTMX 2.0.4 vendored to `static/htmx.min.js`
(no CDN dependency).
