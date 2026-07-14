# PRD 01 — Lifecycle & State Machine

> Last reviewed against code: 2026-05-07 (post PR #116 — RENEW saga step added; saga stays at RECEIVED).

## Lifecycle

Six user-facing states map onto the ISO 18626 supplier-side state machine:

```
   ┌─────────┐    ┌────────┐    ┌──────────┐    ┌──────────┐    ┌────────┐    ┌────────┐    ┌──────────┐
   │Submitted│───▶│Routed  │───▶│Approving │───▶│Approved  │───▶│Shipped │───▶│Received│───▶│Returned  │
   └─────────┘    └────────┘    └──────────┘    └──────────┘    └────────┘    └────────┘    └──────────┘
        │              │                               │              │              │              │
        ▼              ▼                               ▼              ▼              ▼              ▼
   Cancelled       Submitted                       Cancelled       Disputed       Disputed       Disputed
   (terminal)      (re-rank)                       (terminal)      (recall)       (manual)       (manual)
```

`LifecycleState` enum (in `src/agora/models/lifecycle.py`):
`SUBMITTED, ROUTED, APPROVING, APPROVED, SHIPPED, RECEIVED, RETURNED,
CANCELLED, UNFILLED, DISPUTED`.
`TERMINAL_STATES = {RETURNED, CANCELLED, UNFILLED, DISPUTED}` —
`RECEIVED` is **not** terminal (it's an active borrower-custody state
that flows on to `RETURNED`).

`APPROVING` is an in-flight intermediate added per ADR-0012 (fully
wired since PR #59). The APPROVE forward enqueues a `send_request`
outbox row and advances the saga to `APPROVING`; the outbox worker
calls the supplier and the projection callback advances the saga to
`APPROVED`. Staff may not compensate during `APPROVING` — the
`reshare_id` is not yet available (endpoint rejects with 400).

## Forward transitions

| Step | Trigger | ISO 18626 wire effect | Human gate | Agent that drafts |
|------|---------|------------------------|------------|---------------------|
| Submit | Patron form / OpenURL | none yet | Patron self-serve | n/a |
| Route | Submitted persisted | none | **Staff approve choice** | RoutingAgent |
| Approve | Routed approved by staff | `Request` sent to chosen supplier | Supplier-side staff implicit | TransactionAgent |
| Ship | Supplier marks `Loaned` | `SupplyingAgencyMessage Loaned` received | Lender confirm in their ILS | TransactionAgent |
| Receive | Borrower confirms physical receipt | `RequestingAgencyMessage` "ItemReceived" note (supplier stays `Loaned`) | **Borrower confirm** | n/a (advisory; staff-driven) |
| Return | Borrower returns item | `RequestingAgencyMessage Returned`, then supplier `LoanCompleted` | Borrower-side check-in | TransactionAgent |
| Renew  | Patron requests loan extension while saga is at `RECEIVED` (PR #116) | `renew_request` outbox intent → mod-rs renewal action; **sandbox-blocked** on `HttpReShareClient` (no verified mod-rs renewal action; raises `ClientError`, surfaces as `dead_letter` row for staff). `MockReShareClient` succeeds so demo + tests stay green. Saga stays at `RECEIVED`; the new due date lands in the RENEW forward event payload. | **Staff approve renewal** (no patron self-serve) | n/a (advisory; staff-driven) |

## Compensators (per step)

| Forward | Compensator state_after | Real-world action (in `saga/flows.py`) |
|---------|-------------------------|----------------------------------------|
| Submit  | `Cancelled`             | Mark withdrawn before any peer contacted (ledger-only). |
| Route   | `Submitted`             | Revert routing; saga returns to Submitted for re-rank (ledger-only). |
| Approve | `Cancelled` (terminal)  | Enqueue `cancel_request` outbox intent → mod-rs cancel. |
| Ship    | `Disputed`              | Enqueue a single `recall_request` outbox intent. Both branches (saga at `SHIPPED` or post-`RECEIVED`) emit only the recall — the NCIP `check_out` re-anchor moved ILS-loan opening to RECEIVE forward, so at `SHIPPED` no loan exists to clear and at `RECEIVED` the patron physically holds the book (loan correctly reflects custody; the eventual return flow owns `check_in`). The `current_state` branch survives only as state-aware rationale text. NB: `HttpReShareClient.recall_request` raises today (mod-rs has no first-class recall); surfaces as a `dead_letter` row for staff. |
| Receive | `Disputed`              | Receipt is physical — un-undoable. Records the contradiction and routes to staff reconciliation (ledger-only). |
| Return  | `Disputed`              | Open manual reconciliation case (ledger-only). |
| Renew   | `Received`              | Ledger-only revert: writes a COMPENSATOR event with `renewal_cancelled=True` and the original `new_due_at` echoed back as `reverted_new_due_at`. **No outbox intent** — no confirmed mod-rs un-renew action exists. Staff must notify the patron of the reverted due date. Saga stays at `RECEIVED`. |

**Compensators are not symmetric inverses.** They model real-world
recovery, not DB rollback. The saga ledger tracks both forward outcome
and compensator outcome; both are auditable.

## Branching states (ISO 18626)

The supplier-side state machine has more states than the user-facing
lifecycle. Map these to user lifecycle as follows:

| ISO 18626 state             | User-visible status                                            |
|-----------------------------|----------------------------------------------------------------|
| Requested                   | Approving (intent sent, supplier ack pending — ADR-0012)       |
| ExpectToSupply / WillSupply | Approved                                                       |
| Loaned / Overdue / Recalled | Shipped (pre-borrower-receipt) **or** Received (post-receipt). Supplier-side stays `Loaned` either way; the user-visible split is driven by the borrower's `ItemReceived` confirmation. |
| LoanCompleted               | Returned                                                       |
| Unfilled                    | Unfilled (terminal)                                            |
| Cancelled                   | Cancelled (terminal — APPROVE compensator end state)           |
| RetryPossible               | Routed → loop back to Routing with annotation (planned)        |
| CopyCompleted               | Returned (for copy requests, no physical return)               |

## State invariants

- Saga ledger stores user lifecycle state. ISO 18626 state is stored
  separately as `iso18626_state` for supplier-side correctness.
- Transitions are append-only; never mutate prior rows.
- Every transition row carries `idempotency_key`, `actor` (agent id or
  staff user), `reason` (free text), `iso18626_message_id` (if any).
- A request can be in only one user-lifecycle state at a time, but may
  have multiple in-flight ISO 18626 messages (e.g. ship + recall race).

## Transition legality & single-use gates (review 2026-07-13)

The lifecycle above is enforced at the data layer, not just drawn:

- **Legal-transition tables.** `FORWARD_STEP_ALLOWED_STATES` and
  `COMPENSATOR_ALLOWED_STATES` (`models/lifecycle.py`) encode the
  allowed *current* state for each step. `Coordinator.run_forward` /
  `run_compensator` check the persisted `current_state` before running
  and raise `IllegalTransitionError` (the API maps it to **409**) when
  the step is illegal from that state. Steps absent from a table fail
  closed. This blocks step-skipping (e.g. `receive` at `Approved`) and
  compensator jumps (e.g. `compensate step=submit` at `Shipped`).
- **Gates are single-use.** A committed gate is consumed by any later
  FORWARD event for its step; re-running a step requires a fresh
  approval. This blocks a double-clicked approve from dispatching two
  supplier requests.

## Terminal states

`Returned` (success), `Cancelled` (pre-approval **or** post-approval
revoke — see compensator table above), `Unfilled` (no supplier
fulfilled), `Disputed` (manual escalation). Once a saga reaches a
terminal state, `SagaLedger.append` refuses **any** state-changing
event regardless of kind (raises `TerminalStateError`) — including a
state-changing OBSERVATION (review 2026-07-13). The sole carve-out is
the `RESOLVE` OBSERVATION that moves `Disputed → Cancelled/Unfilled`
for the `/override` endpoint. Non-state-changing OBSERVATION events
(`state_before == state_after`) are still allowed.
