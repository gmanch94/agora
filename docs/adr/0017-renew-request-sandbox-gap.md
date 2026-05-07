# ADR-0017: Renew Request Implementation Gap (Sandbox-Blocked)

## Status

Accepted (2026-05-07). Revisit before production: the wire-level
mod-rs action for borrower-initiated renewal has not been confirmed
and must be verified against a live two-tenant sandbox before
`HttpReShareClient.renew_request` can be unblocked.

## Context

The Agora RENEW saga step (`StepName.RENEW`, `src/agora/saga/flows.py`)
allows a patron to extend an active loan. The forward step enqueues a
`renew_request` outbox intent directed at `HttpReShareClient`. On the
mock client the intent succeeds; on the HTTP client it raises
`ClientError` immediately, leaving the saga at RECEIVED with a
dead-letter outbox row for staff review.

**Why it raises:** No borrower-initiated renewal action has been
confirmed in mod-rs `Actions.groovy`. Unlike the well-documented
state-machine actions (`requesterReceived`, `requesterReturnShipped`,
etc.), renewal is not enumerated as a `performAction` target in the
source reviewed during the 2026-05-06 sandbox probe (mod-rs
2.19.0-rc17). The probe was focused on the recall/compensate-SHIP
path (ADR-0016); renewal was not exercised.

**ISO 18626 context.** ISO 18626 defines a `Renew` message type sent
from the requesting agency to the supplying agency. The supplier
responds with `RenewedItemReceived` (granted) or `UnableToRenew`
(denied). If mod-rs supports the borrower's side of this exchange, it
would likely be exposed as either:

- A `performAction` on the `REQ_RECEIVED` state (e.g. `requesterRenew`
  or similar), causing mod-rs to emit a `RequestingAgencyMessage` Renew
  to the supplier, or
- A `message` performAction with an ISO 18626 Renew payload in
  `actionParams` — the same mechanism considered for recall in ADR-0016.

**Two options for the production path:**

**Option A — `performAction` with a mod-rs renewal action**

If mod-rs exposes a named action (e.g. `requesterRenewed`) on
`REQ_RECEIVED`, `HttpReShareClient.renew_request` would call
`POST /rs/patronrequests/{reshare_id}/performAction` with
`{"action": "<action>", "actionParams": {"extension_days": N}}`.

The supplying institution's mod-rs would receive the ISO 18626 Renew
message and respond; the requester's mod-rs would surface the grant or
denial in the record's state or events, from which Agora could
project a new due date.

Requires: browsing `Actions.groovy` for `REQ_RECEIVED` validActions
in a current mod-rs build, then wire-testing against a two-tenant
sandbox.

**Option B — `message` performAction with ISO 18626 Renew payload**

If no named renewal action exists, the same `message` route explored
for ADR-0016 recall could carry a Renew payload. The `actionParams`
shape is undocumented; needs empirical discovery.

Trade-off: the `message` action at `REQ_RECEIVED` was not probed in
the 2026-05-06 session, so this path is equally unverified.

## Decision

**Preserve `ClientError` in `HttpReShareClient.renew_request` until a
two-tenant sandbox probe confirms the wire-level action.**

The mock client succeeds, so tests and demos work. The outbox dead-
letter row makes the failure visible to staff without corrupting saga
state — the saga stays at RECEIVED with the forward event committed,
the portal shows the renewal as in-progress, and staff can manually
contact the supplier to extend the loan while Agora is unblocked.

When the sandbox probe is carried out:

1. Confirm which `performAction` string (if any) mod-rs accepts on a
   `REQ_RECEIVED` record for borrower-initiated renewal.
2. Determine whether the supplier's mod-rs transitions state and
   whether an outbound ISO 18626 message is generated.
3. Update `HttpReShareClient.renew_request` to send the confirmed
   action, replacing the `ClientError`.
4. Add `extension_days` to `actionParams` (or the equivalent field
   mod-rs reads) and map the response to `ReShareSendResult`.
5. Pair the implementation with an integration test against the live
   sandbox (same pattern as the 2026-05-06 `make reshare-probe`
   session documented in `docs/lessons.md`).

## Consequences

- **Prototype:** Renewal via the staff console records a RENEW forward
  event and advances the saga to RECEIVED (same state — renewal is
  idempotent with respect to state). The outbox dead-letters the
  HTTP call; staff sees a stuck outbox row and must extend the loan
  out-of-band.
- **Production blocker:** `HttpReShareClient.renew_request` must be
  unblocked before Agora can serve as a live borrowing system for
  institutions that need patron-driven renewals.
- **Precedent:** This gap mirrors ADR-0016 (recall). The same two-tenant
  sandbox setup required for ADR-0016 Option A resolution will also
  unblock this ADR.
