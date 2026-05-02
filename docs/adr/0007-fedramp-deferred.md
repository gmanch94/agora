# ADR 0007 — FedRAMP authorization deferred; alignment-noted only

**Status:** Accepted
**Date:** 2026-05-02

## Context

User listed FedRAMP among applicable standards. FedRAMP is a US federal
program providing a standardized approach to security assessment for
cloud services serving the federal government. Authorization tiers:
Low, **Moderate**, High. Moderate ≈ 325 NIST 800-53 controls with a
12–18 month authorization process (3PAO assessment, agency sponsor,
SSP/SAR/POA&M, continuous monitoring).

User chose **research prototype** scope during planning.

## Decision

Do not implement FedRAMP controls in the prototype. Document
**alignment notes** in `docs/prd/06-non-functional.md` describing
which control families would apply and how the prototype design
already aligns or would need to change.

Do **not** claim FedRAMP-aligned, FedRAMP-ready, or FedRAMP-authorized
status anywhere in code, docs, or marketing.

## Consequences

**Positive**
- Avoid weeks of compliance scaffolding for a research artifact.
- Keep the build local and fast.
- Document the gap honestly.

**Negative**
- Prototype cannot be deployed as-is to a federal environment.
- Some architectural choices (in-process secrets, no FIPS modules)
  would need revision for production.

## Alignment notes

Captured in `docs/prd/06-non-functional.md` § Security and § FedRAMP
alignment. Key observations:

- Saga ledger already serves as an audit log substrate (AU-2, AU-3).
- Idempotency + replay-safe design supports SI-10 (input validation)
  and CP-10 (recovery).
- Stack runs on Postgres + GCP-friendly Python — straightforward
  migration path to GCP Assured Workloads (FedRAMP High region).
- Gaps: identity (no SAML/PIV/CAC), encryption (no FIPS 140-3), audit
  immutability (need cloud audit log integration), continuous monitoring.

## Out of scope

- SSP / SAR / POA&M authoring
- Boundary diagram for SP 800-37 RMF process
- Vulnerability scanning automation
- Penetration testing
