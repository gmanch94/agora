# PRD 01 — Lifecycle & State Machine

> Last reviewed against code: 2026-05-02.

## Lifecycle

Five user-facing states map onto the ISO 18626 supplier-side state machine:

```
   ┌─────────┐    ┌────────┐    ┌──────────┐    ┌────────┐    ┌──────────┐
   │Submitted│───▶│Routed  │───▶│Approved  │───▶│Shipped │───▶│Returned  │
   └─────────┘    └────────┘    └──────────┘    └────────┘    └──────────┘
        │              │             │              │              │
        ▼              ▼             ▼              ▼              ▼
   Cancelled       Submitted     Cancelled       Disputed       Disputed
   (terminal)      (re-rank)     (terminal)      (recall)       (manual)
```

`LifecycleState` enum (in `src/agora/models/lifecycle.py`):
`SUBMITTED, ROUTED, APPROVING, APPROVED, SHIPPED, RETURNED, CANCELLED,
UNFILLED, DISPUTED`.
`TERMINAL_STATES = {RETURNED, CANCELLED, UNFILLED, DISPUTED}`.

`APPROVING` is an in-flight intermediate added per ADR-0012. It marks
"intent committed, supplier not yet acknowledged" — the APPROVE
forward will enqueue a `send_request` outbox row and the worker will
project the supplier ack into a transition to `APPROVED`. The
diagram above still shows the user-facing happy path; today's code
transitions directly `ROUTED → APPROVED` until the flow rewrite
lands in the follow-up PR.

## Forward transitions

| Step | Trigger | ISO 18626 wire effect | Human gate | Agent that drafts |
|------|---------|------------------------|------------|---------------------|
| Submit | Patron form / OpenURL | none yet | Patron self-serve | n/a |
| Route | Submitted persisted | none | **Staff approve choice** | RoutingAgent |
| Approve | Routed approved by staff | `Request` sent to chosen supplier | Supplier-side staff implicit | TransactionAgent |
| Ship | Supplier marks `Loaned` | `SupplyingAgencyMessage Loaned` received | Lender confirm in their ILS | TransactionAgent |
| Return | Borrower returns item | `RequestingAgencyMessage Returned`, then supplier `LoanCompleted` | Borrower-side check-in | TransactionAgent |

## Compensators (per step)

| Forward | Compensator state_after | Real-world action (in `saga/flows.py`) |
|---------|-------------------------|----------------------------------------|
| Submit  | `Cancelled`             | Mark withdrawn before any peer contacted (ledger-only). |
| Route   | `Submitted`             | Revert routing; saga returns to Submitted for re-rank (ledger-only). |
| Approve | `Cancelled` (terminal)  | Enqueue `cancel_request` outbox intent → mod-rs cancel. |
| Ship    | `Disputed`              | Enqueue `recall_request` outbox intent. NB: `HttpReShareClient.recall_request` raises today (mod-rs has no first-class recall); surfaces as a `dead_letter` row for staff. |
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
| Loaned / Overdue / Recalled | Shipped                                                        |
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
