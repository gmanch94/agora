# PRD 00 — Agora Overview

> Last reviewed against code: 2026-05-05 (post PRs #87–#93 — NCIP item-barcode + override endpoint + override HTMX form + saga browser).

## Problem

Inter-Library Loan (ILL) workflows in academic and research libraries
are manual-step heavy. NCIP adoption alone reduces borrow-side staff
steps ~50% and lend-side ~42% (NISO benchmarks), but humans still
manually do discovery, supplier ranking, copyright clearance,
status chasing, recall coordination, and reconciliation.

ILL involves long-running, multi-party transactions across heterogeneous
library systems. Failures are common (item not available, supplier
declines, lost in transit). Real money and legal compliance (CONTU,
copyright) raise the bar for correctness.

## Hypothesis

A multi-agent orchestrator over standards-compliant ILL infrastructure
(FOLIO/ReShare) can:

- Compress the human-touch surface further by automating discovery,
  routing, policy checks, and tracking.
- Improve correctness via explicit saga + compensator semantics.
- Maintain legal/policy safety by keeping humans in the loop on every
  state transition (advisory agents, human commits).

This prototype tests that hypothesis without taking on production /
compliance burden.

## Users

| Persona | Role | Primary needs |
|---|---|---|
| ILL Borrowing Staff | Approves outbound requests at consortium | Quickly review agent recommendations, click approve/reject, see reasoning |
| ILL Lending Staff | Fulfills inbound from peers | See queued requests, confirm shipment, manage returns |
| Patron | Library user wanting an item | Submit request via OpenURL or manual form (out of scope for prototype UI) |
| Consortium Admin | Sets routing policy across member libraries | Configure SLA tiers, copyright thresholds |

## Goals

1. Demonstrate end-to-end lifecycle (Submit → Return) running through
   real ReShare sandbox with two simulated tenants.
2. Show saga compensation actually rolls back correctly under chaos.
3. Show idempotency: replay any message N times, observable effect once.
4. Show agent reasoning traces drive faster human approvals.

## Non-goals

- Production deployment
- FedRAMP authorization
- Real money / billing
- Patron-facing UI
- Multi-region / HA topology

## Success criteria (prototype demo)

- `make demo` runs the scripted happy path against `MockReShareClient`
  (in-memory SQLite + outbox drain), shows every state transition in
  the ledger, and ends with `Returned`. **Implemented**
  (`src/agora/demos/happy_path.py`).
- `pytest` passes with property-based saga + idempotency tests
  (`tests/test_property_saga.py`, Hypothesis). **Implemented** —
  361 tests green at time of review (+6 postgres-only).
- Architecture & decisions documented under `docs/` — PRDs, ADRs
  (16), runbook, and SDD. **Implemented**.
- ~~Chaos test (`make chaos`)~~: **dropped** — never wired and the
  property tests in `tests/test_property_saga.py` cover compensator
  symmetry under arbitrary forward sequences, which subsumes the
  random-injection use case. The Makefile target was removed in the
  same PR that retired this row.
