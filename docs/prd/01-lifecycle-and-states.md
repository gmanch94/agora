# PRD 01 — Lifecycle & State Machine

> Last reviewed against code: 2026-05-04 (post PRs #89/#90 — NCIP item-barcode + override endpoint).

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

## Compensators (per step)

| Forward | Compensator state_after | Real-world action (in `saga/flows.py`) |
|---------|-------------------------|----------------------------------------|
| Submit  | `Cancelled`             | Mark withdrawn before any peer contacted (ledger-only). |
| Route   | `Submitted`             | Revert routing; saga returns to Submitted for re-rank (ledger-only). |
| Approve | `Cancelled` (terminal)  | Enqueue `cancel_request` outbox intent → mod-rs cancel. |
| Ship    | `Disputed`              | Enqueue a single `recall_request` outbox intent. Both branches (saga at `SHIPPED` or post-`RECEIVED`) emit only the recall — the NCIP `check_out` re-anchor moved ILS-loan opening to RECEIVE forward, so at `SHIPPED` no loan exists to clear and at `RECEIVED` the patron physically holds the book (loan correctly reflects custody; the eventual return flow owns `check_in`). The `current_state` branch survives only as state-aware rationale text. NB: `HttpReShareClient.recall_request` raises today (mod-rs has no first-class recall); surfaces as a `dead_letter` row for staff. |
| Receive | `Disputed`              | Receipt is physical — un-undoable. Records the contradiction and routes to staff reconciliation (ledger-only). |
| Return  | `Disputed`              | Open manual reconciliation case (ledger-only). |

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

## Terminal states

`Returned` (success), `Cancelled` (pre-approval **or** post-approval
revoke — see compensator table above), `Unfilled` (no supplier
fulfilled), `Disputed` (manual escalation). The ledger refuses any
further state-changing event once a saga reaches a terminal state
(`SagaLedger.append` raises `TerminalStateError`); benign
OBSERVATION events are still allowed.
