# PRD 01 — Lifecycle & State Machine

## Lifecycle

Five user-facing states map onto the ISO 18626 supplier-side state machine:

```
        ┌──────────────────────────────────────────────────┐
        │                                                  ▼
   ┌─────────┐    ┌────────┐    ┌──────────┐    ┌────────┐    ┌──────────┐
   │Submitted│───▶│Routed  │───▶│Approved  │───▶│Shipped │───▶│Returned  │
   └─────────┘    └────────┘    └──────────┘    └────────┘    └──────────┘
        │              │             │              │              │
        ▼              ▼             ▼              ▼              ▼
     Cancel         Reroute       Decline         Recall         Disputed
                                                                    │
                                                                    ▼
                                                              Reconciled
```

## Forward transitions

| Step | Trigger | ISO 18626 wire effect | Human gate | Agent that drafts |
|------|---------|------------------------|------------|---------------------|
| Submit | Patron form / OpenURL | none yet | Patron self-serve | n/a |
| Route | Submitted persisted | none | **Staff approve choice** | RoutingAgent |
| Approve | Routed approved by staff | `Request` sent to chosen supplier | Supplier-side staff implicit | TransactionAgent |
| Ship | Supplier marks `Loaned` | `SupplyingAgencyMessage Loaned` received | Lender confirm in their ILS | TransactionAgent |
| Return | Borrower returns item | `RequestingAgencyMessage Returned`, then supplier `LoanCompleted` | Borrower-side check-in | TransactionAgent |

## Compensators (per step)

| Forward | Compensator | Real-world action |
|---------|-------------|---------------------|
| Submit | CancelRequest | Mark withdrawn before any peer contacted |
| Route | Reroute / Withdraw | Pick next-ranked supplier or terminate |
| Approve | Revoke | Send `RequestingAgencyMessage Cancel` to supplier |
| Ship | Recall | Send recall request; if item already in transit, intercept |
| Return | Re-loan / Dispute | Re-ship to next requester or open dispute saga |

**Compensators are not symmetric inverses.** They model real-world
recovery, not DB rollback. The saga ledger tracks both forward outcome
and compensator outcome; both are auditable.

## Branching states (ISO 18626)

The supplier-side state machine has more states than the user-facing
lifecycle. Map these to user lifecycle as follows:

| ISO 18626 state | User-visible status |
|------------------|---------------------|
| Requested | Submitted (peer contacted) |
| ExpectToSupply / WillSupply | Approved |
| Loaned / Overdue / Recalled | Shipped |
| LoanCompleted | Returned |
| Unfilled / Cancelled | Routed → triggers Reroute saga |
| RetryPossible | Routed → loop back to Routing with annotation |
| CopyCompleted | Returned (for copy requests, no physical return) |

## State invariants

- Saga ledger stores user lifecycle state. ISO 18626 state is stored
  separately as `iso18626_state` for supplier-side correctness.
- Transitions are append-only; never mutate prior rows.
- Every transition row carries `idempotency_key`, `actor` (agent id or
  staff user), `reason` (free text), `iso18626_message_id` (if any).
- A request can be in only one user-lifecycle state at a time, but may
  have multiple in-flight ISO 18626 messages (e.g. ship + recall race).

## Terminal states

`Returned` (success), `Cancelled` (pre-approval), `Unfilled` (no supplier
fulfilled), `Disputed` (manual escalation). Saga marks these and stops
scheduling forward steps; reconciliation can still run cleanup.
