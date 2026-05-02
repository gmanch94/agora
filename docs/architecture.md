# Agora — Architecture (hand-drawn)

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
        TX["TransactionAgent<br/>drives ReShare"]
        TRK["TrackingAgent<br/>overdue / recall"]
        REC["ReconciliationAgent<br/>compensator"]
    end

    subgraph SAGA["Saga core (Postgres event-sourced)"]
        COORD["Coordinator<br/>open_gate / commit_gate / run_forward"]
        LEDGER[("saga_event<br/>append-only ledger")]
        SAGAS[("saga<br/>state projection")]
        IDEM[("inbox / outbox<br/>idempotency tables")]
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
    TX   --> RESHARE
    TRK  --> COORD
    REC  --> COORD

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
    Routed --> Approved: staff approves<br/>+ supplier accepts
    Approved --> Shipped: lender confirms<br/>SupplierMarkShipped
    Shipped --> Returned: borrower confirms<br/>RequesterMarkReturned
    Returned --> [*]

    Submitted --> Cancelled: patron / staff cancel
    Routed --> Cancelled: re-route exhausted
    Approved --> Unfilled: supplier RetryPossible
    Shipped --> Recalled: lender recall
    Cancelled --> [*]
    Unfilled --> [*]
    Recalled --> Returned: physical return
```

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

    OUT["Outbound delivery<br/>(to ReShare / peer)"] --> OBOX[("outbox<br/>pending → delivered<br/>or → dead-letter")]
    OBOX --> RS["ReShare<br/>dedups on<br/>Idempotency-Key header"]
```

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
