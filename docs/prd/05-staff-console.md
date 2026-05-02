# PRD 05 — Staff Console

The staff console is the **only UI in the prototype**. It is the
human-in-the-loop surface for every state transition.

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
- Action buttons: Approve, Reject (with reason), Override (with reason)

### Saga browser
Filter by state, library, date. Read-only. Useful for demo + debug.

## Backend endpoints (FastAPI)

```
GET    /api/sagas                       # list, filter
GET    /api/sagas/{id}                  # detail with events
POST   /api/sagas/{id}/approve          # commit pending forward step
POST   /api/sagas/{id}/reject           # mark step rejected, no forward
POST   /api/sagas/{id}/override         # override hard-fail policy flag
POST   /api/sagas/{id}/compensate       # manually trigger compensator
POST   /api/requests                    # patron submit (dev/test only)
GET    /api/health
```

All write endpoints require an idempotency key in `Idempotency-Key`
header; safe to retry.

## UX principles

- **Reasoning before action.** Recommendation summary always visible
  before the approve button.
- **No silent autopilot.** Even with override, staff must type a reason;
  reason persists in ledger.
- **Reproducibility.** Every action has an event in the ledger; UI is a
  projection, never authoritative.

## Implementation note (prototype)

Build the API first. UI can be the simplest possible — server-side
rendered HTMX pages or a tiny React shell. Don't waste time on
visual polish; correctness + traceability is the demo target.
