# ADR-0016: Compensate-SHIP Path — `manualClose` as Force-Close (Prototype)

## Status

Accepted (2026-05-06). Revisit before production: Option A (ISO 18626
Cancel) is the protocol-correct long-term path.

## Context

The SHIP compensator in `saga/flows.py` enqueues a ReShare
`recall_request` outbox intent whenever a saga must unwind a SHIP
step (e.g. the borrowing library cancels after the item is already
in transit). The compensator is correct from the saga's perspective;
the question is what `HttpReShareClient.recall_request` should
actually send to mod-rs.

**Probe findings (2026-05-06, mod-rs 2.19.0-rc17):**
`Actions.groovy` defines no `recall`, `requesterRecall`, or
`borrowerRecall` action. `REQ_RECALLED` is a *destination* state
that the *supplier* drives via an inbound ISO 18626 message — it is
not reachable by the requester via `performAction`. From `REQ_SHIPPED`
the only manual performAction available to the requester is
`requesterReceived`. Seven candidate action strings were tried; all
returned HTTP 400 (action not registered for that state/role).

See `src/agora/clients/reshare.py` module docstring, note 7 for
the full probe evidence.

**Two options were considered:**

**Option A — ISO 18626 Cancel via `message` performAction**

Send a `RequestingAgencyMessage` Cancel by posting `performAction`
with `action="message"` and a cancel reason code. This is the
protocol-correct approach: mod-rs would forward the cancel to the
supplying institution via ISO 18626, and the supplier's mod-rs would
transition accordingly.

Trade-off: `message` at `REQ_SHIPPED` is unverified — the probe only
observed it in the Responder (`RES_IDLE`) validActions. The exact
`actionParams` shape for a cancel reason code is undocumented in
source and needs wire-level testing against a two-tenant sandbox.
Not viable for the current prototype without additional investigation.

**Option B — `manualClose` as force-close**

`manualClose` is available at all mod-rs states (confirmed in
`AvailableActionData.groovy`). It closes the local mod-rs record
immediately with no ISO 18626 message sent to the supplier.

Trade-off: the supplier is **not notified**. This is semantically
incorrect for a production consortium where the supplying library
must know to retrieve the item. For the prototype it unblocks the
compensate-SHIP flow: the saga transitions to DISPUTED, the outbox
row is delivered, and staff can follow up manually.

## Decision

**Ship Option B (`manualClose`) for the prototype.**

- `HttpReShareClient.recall_request` calls `performAction` with
  `action="manualClose"` and the reason string in `actionParams`.
- The Protocol method name stays `recall_request` — it reflects
  the saga's *intent* (recall/cancel), not the wire mechanism. The
  docstring explicitly states that the current implementation
  force-closes the local record without supplier notification.
- A new class constant `_ACTION_MANUAL_CLOSE = "manualClose"` is
  added to `HttpReShareClient` alongside the other action constants.
- `MockReShareClient.recall_request` is unchanged — it already
  succeeds (simulates a state transition), keeping tests and the
  demo green.

## Consequences

**Positive**

- Unblocks the SHIP compensator. The outbox row is delivered instead
  of dead-lettering; the saga reaches DISPUTED as designed.
- Simple: one `_perform_action` call, one new constant, one test.
- Consistent with the "fail loudly in dev, silent dead-letter in
  prod" intent replaced by "succeed on the wire, note the gap in
  code."

**Negative / risks**

- **No supplier notification.** In a real consortium the supplying
  library will not know to abort the loan. Staff must follow up
  manually after a DISPUTED saga appears in the console. This is
  acceptable for prototype/demo traffic, not for production.
- **`manualClose` from REQ_SHIPPED is verified from source review
  only**, not live Requester-side testing (the probe only hit the
  Responder side). If mod-rs rejects `manualClose` at `REQ_SHIPPED`,
  the outbox row will dead-letter — same observable behaviour as the
  previous `ClientError`. The saga will still reach DISPUTED.
- **Method name mismatch.** `recall_request` calls `manualClose`.
  This is a documented lie acceptable for a prototype. A future
  rename to `force_close_request` (or proper Option A wiring) should
  be done before production along with updating the outbox payload's
  `"action"` string and all callers.

## Follow-up

- Before production: write ADR superseding this one, implement
  Option A (ISO 18626 Cancel via `message`), wire-test against a
  two-tenant sandbox.
- If `manualClose` is rejected at `REQ_SHIPPED` in live testing,
  fall back to the `ClientError` guard temporarily and investigate
  the correct action with `docker exec agora-mod-rs` against a
  Requester-side request.
