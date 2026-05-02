# ADR 0001 — Wrap FOLIO/ReShare for ISO 18626 + NCIP

**Status:** Accepted
**Date:** 2026-05-02

## Context

ISO 18626 is a non-trivial XML wire protocol with state machine semantics
that have evolved across editions (2014, 2017, 2021, draft revision in
progress). NCIP (Z39.83) is similarly stateful and underspecified in places.
Implementing either from scratch as part of a research prototype would burn
weeks on spec compliance with low marginal value.

FOLIO is an open-source library services platform with two relevant modules:
- **`mod-rs`** (and the **ReShare** application built on it) — production
  ISO 18626 implementation used by consortia (PALCI, EAST).
- **`mod-ncip`** — NCIP responder/initiator integrated with FOLIO inventory
  and circulation.

## Decision

Treat FOLIO/ReShare as the standards-compliance layer. Agora's agents
drive ReShare via its REST API; ReShare handles all ISO 18626 wire
formatting, validation, state machine. NCIP traffic to local ILS goes
through `mod-ncip`.

## Consequences

**Positive**
- Zero ISO 18626 XML serialization code to write or maintain.
- Inherit state-machine correctness from a production-tested codebase.
- ReShare already models consortium tenancy — matches our requirement.
- Open source; can deploy locally for development via Docker.

**Negative**
- Adds a heavyweight dependency (FOLIO requires Postgres, Kafka, Okapi).
- Coupling to ReShare API shape; if it changes, our adapter changes.
- Agora can no longer claim "pure" agent system — it's an orchestration
  + intelligence layer over an existing platform.
- Some flows we may want (e.g. predictive routing) require data that
  ReShare doesn't expose — may need direct DB reads as escape hatch.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| Implement ISO 18626 from scratch in Python | Weeks of work, low research value, easy to get state machine wrong |
| Use Ex Libris Alma resource sharing API | Proprietary; can't run locally; vendor lock |
| Use OCLC WorldShare ILL API | Proprietary; can't run locally; can't simulate consortium |
| ReShare with custom thin shim (no FOLIO deps) | mod-rs is tightly coupled to FOLIO/Okapi; extracting it is more work than running the platform |

## Boundary

Agora owns: agent orchestration, saga ledger, idempotency, staff console,
discovery (SRU/OpenURL), policy (CONTU, eligibility, budget), human
approval gates.

ReShare owns: ISO 18626 wire protocol, NCIP wire protocol, lender/borrower
state machine, peer discovery within consortium, retry of wire-level
delivery.
