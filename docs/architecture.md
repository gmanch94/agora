# Agora — Architecture (hand-drawn)

> Last reviewed against code: 2026-05-03 (post PRs #17/#18/#19/#24/
> #25/#28 + RECEIVED state — APPROVING-via-outbox, NCIP fan-out,
> TrackingScanner lifespan task, alembic-on-real-postgres CI,
> multi-worker outbox, borrower-receipt state).

The diagrams below use Mermaid's hand-drawn (`look: handDrawn`) theme so
they read like a whiteboard sketch. GitHub renders them inline.

## Layer cake

```mermaid
---
config:
  look: handDrawn
  theme: neutral
  flowchart:
    htmlLabels: true
---
flowchart TB
    subgraph UI["Staff console (FastAPI + future HTMX/React)"]
        UI_REQ["POST /requests"]
        UI_SAGA["GET /sagas/:id"]
        UI_APPROVE["POST /sagas/:id/approve"]
    end

    subgraph AGENTS["Advisory agents (Google ADK style)"]
        DISC["DiscoveryAgent<br/>SRU + OpenURL"]
        ROUTE["RoutingAgent<br/>weighted scorer"]
        POL["PolicyAgent<br/>CONTU / eligibility / budget"]
        TX["TransactionAgent<br/>builds ReShare intents"]
        TRK["TrackingAgent<br/>overdue / recall"]
        REC["ReconciliationAgent<br/>compensator"]
    end

    subgraph SAGA["Saga core (Postgres event-sourced)"]
        COORD["Coordinator<br/>open_gate / commit_gate / run_forward"]
        LEDGER[("saga_event<br/>append-only ledger")]
        SAGAS[("saga<br/>state projection")]
        IDEM[("inbox / outbox<br/>idempotency tables")]
    end

    subgraph WORKERS["Lifespan tasks (asyncio)"]
        OUTW["OutboxWorker<br/>claim via SKIP LOCKED<br/>→ ReShare / NCIP<br/>→ projection callback"]
        SCAN["OverdueScanner<br/>tier-1 overdue +<br/>tier-2 recall_proposed"]
    end

    subgraph RESHARE["FOLIO mod-rs (ReShare)"]
        MODRS["ISO 18626 state machine<br/>+ Kafka"]
        MODNCIP["mod-ncip"]
    end

    subgraph EXT["External"]
        PEERS(["Peer libraries<br/>ISO 18626"])
        ILS(["Local ILS<br/>NCIP"])
        CAT(["Catalogs<br/>SRU / OpenURL"])
    end

    UI_REQ --> COORD
    UI_APPROVE --> COORD
    UI_SAGA --> SAGAS

    COORD --> LEDGER
    COORD --> SAGAS
    COORD --> IDEM

    DISC -.advisory.-> COORD
    ROUTE -.advisory.-> COORD
    POL  -.advisory.-> COORD
    TX   -.intent.-> IDEM
    TRK  --> COORD
    REC  --> COORD

    OUTW --> IDEM
    OUTW --> RESHARE
    OUTW --> MODNCIP
    OUTW -.projection.-> LEDGER
    SCAN --> LEDGER

    DISC --> CAT
    RESHARE --> PEERS
    MODNCIP --> ILS
```

## Lifecycle state machine

```mermaid
---
config:
  look: handDrawn
  theme: neutral
---
stateDiagram-v2
    [*] --> Submitted: patron submits<br/>(OpenURL / form)
    Submitted --> Routed: staff approves<br/>routing rec
    Routed --> Approving: staff approves<br/>(APPROVE forward<br/>enqueues outbox)
    Approving --> Approved: outbox worker<br/>delivered + projection<br/>writes reshare_id
    Approved --> Shipped: lender confirms<br/>SupplierMarkShipped<br/>(+ NCIP check_out fan-out)
    Shipped --> Received: borrower confirms<br/>physical receipt<br/>(ItemReceived note;<br/>supplier still Loaned)
    Received --> Returned: borrower confirms<br/>RequesterMarkReturned<br/>(+ NCIP check_in fan-out)
    Returned --> [*]

    Submitted --> Cancelled: submit compensator<br/>(patron withdraw)
    Routed --> Submitted: route compensator<br/>(re-rank suppliers)
    Approved --> Cancelled: approve compensator<br/>(cancel at supplier)
    Shipped --> Disputed: ship compensator<br/>(recall enqueued — manual;<br/>NCIP NOT rolled back)
    Received --> Disputed: receive compensator<br/>(physical receipt contested —<br/>staff reconciliation)
    Returned --> Disputed: return compensator<br/>(reconciliation case)
    Cancelled --> [*]
    Unfilled --> [*]
    Disputed --> [*]
```

States and compensator targets reflect `LifecycleState` and
`saga/flows.py`. Notes:

- `APPROVING` (per ADR-0012, PR #17) is the in-flight state between
  the staff click and the supplier ack: APPROVE forward enqueues an
  outbox `send_request` intent and advances to `APPROVING`; the
  worker drains the row, calls ReShare, and the projection callback
  writes an OBSERVATION carrying `reshare_id` that advances to
  `APPROVED`. Compensate during `APPROVING` returns 400 — there is
  no `reshare_id` to cancel against.
- No `Recalled` state in the enum — the SHIP compensator transitions
  to `Disputed` and enqueues a recall outbox intent for staff
  intervention.
- `RECEIVED` is a borrower-side marker between `SHIPPED` and
  `RETURNED`: the saga records the patron's physical-receipt
  confirmation as a pure ledger write (no outbox, no peer ack — the
  supplier-side state stays `Loaned`). Compensator lands in
  `Disputed` because receipt is physically un-undoable. NCIP
  `check_out` is **not** yet re-anchored from SHIP to RECEIVE — that
  is tracked separately in CLAUDE.md known-gaps and waits on a
  follow-up PR.
- NCIP fan-out on SHIP/RETURN is fire-and-forget (CLAUDE.md
  known-gap) and the SHIP compensator does **not** issue a NCIP
  `check_in` rollback — staff investigates the stuck NCIP outbox row
  separately.

## Saga step anatomy (forward + compensator pair)

```mermaid
---
config:
  look: handDrawn
  theme: neutral
---
flowchart LR
    A([staff click<br/>approve]) --> G[/"gate event<br/>outcome=committed"/]
    G --> F["forward step<br/>e.g. submit_to_supplier"]
    F -->|success| L1[("ledger:<br/>state advances")]
    F -->|failure| C["compensator<br/>e.g. cancel_at_supplier"]
    C --> L2[("ledger:<br/>state reverts /<br/>terminal-failure")]

    classDef gate fill:#fef3c7,stroke:#92400e
    classDef fwd fill:#dbeafe,stroke:#1e40af
    classDef comp fill:#fee2e2,stroke:#991b1b
    class G gate
    class F fwd
    class C comp
```

## Idempotency model

```mermaid
---
config:
  look: handDrawn
  theme: neutral
---
flowchart TB
    IN["Inbound msg<br/>(ISO 18626 / NCIP webhook)"] --> CHK{"inbox.message_id<br/>seen?"}
    CHK -->|yes| REPLAY["return stored response"]
    CHK -->|no| PROC["process + store response<br/>+ append saga event"]
    PROC --> LEDG[("ledger append<br/>UNIQUE idempotency_key")]
    LEDG -->|duplicate key| SAVEPOINT["savepoint rolls back<br/>row only — caller tx safe"]
    LEDG -->|fresh| OK["commit"]

    OUT["Outbound delivery<br/>(to ReShare / peer / NCIP)"] --> OBOX[("outbox<br/>pending → in_flight<br/>→ delivered or → dead_letter<br/>(claimed_at lease;<br/>SKIP LOCKED on Postgres)")]
    OBOX -->|UNIQUE idempotency_key<br/>= worker-replay safe| RS["ReShare mod-rs<br/>(ignores Idempotency-Key;<br/>replay-safety lives in<br/>saga + outbox UNIQUEs)"]
```

**Replay-safety lives entirely in our two `UNIQUE` constraints**
(`saga_event.idempotency_key` and `outbox.idempotency_key`). mod-rs
predates the `Idempotency-Key` header convention and ignores it; the
`HttpReShareClient` still passes the header for handlers that do
honour it, but we do not depend on the external side for dedup.

## Where standards live

```mermaid
---
config:
  look: handDrawn
  theme: neutral
---
flowchart LR
    AGORA["Agora<br/>(this repo)"] -->|ULID idem keys<br/>+ saga state<br/>+ rationale| RS["ReShare<br/>mod-rs"]
    RS -->|ISO 18626 XML| PEERS(["Peer libraries"])
    RS -->|NCIP / Z39.83| ILS(["Local ILS"])
    AGORA -->|SRU / CQL| CAT(["Catalogs"])
    AGORA -->|OpenURL 1.0 KEV<br/>parser only| AGORA

    classDef us fill:#dbeafe,stroke:#1e40af
    classDef them fill:#f3f4f6,stroke:#4b5563
    class AGORA us
    class RS,PEERS,ILS,CAT them
```

## Notes

- Boxes in blue = Agora-owned. Boxes in grey = wrapped or external.
- Dashed arrows = advisory (recommendation only — does not commit).
- Solid arrows = state-changing call (committed via the saga ledger).
- The ledger is the source of truth; `saga.current_state` is a
  denormalised projection used by the staff console for cheap reads.
