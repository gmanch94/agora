# ADR 0005 — Human approval required at every state transition

**Status:** Accepted
**Date:** 2026-05-02

## Context

ILL involves real money (lending fees, shipping, copyright royalties),
real legal exposure (CONTU rule of 5, copyright fair use), and real
patron relationships. An autonomous agent that mis-routes a request,
auto-approves a CONTU-violating copy, or fails to recall an item can
create disputes the consortium has to manually unwind.

The user explicitly chose **"human-approve every state"** during planning.

## Decision

Every forward state transition in the saga lifecycle requires an explicit
human approval recorded in the saga ledger before the transaction
agent fires the corresponding ISO 18626 / NCIP / external action.

- Agents produce **recommendations** with rationale.
- The staff console surfaces pending recommendations.
- A staff member clicks **Approve** (or **Reject**, with reason).
- Approval writes a `kind=gate` event to the ledger.
- Only then does the saga coordinator schedule the forward step.

The PolicyAgent retains the ability to **hard-block** a step (e.g.,
copyright violation). Staff can override only with an explicit reason
that persists in the ledger.

## Consequences

**Positive**
- Legally defensible audit trail.
- Forces agent rationales to be human-readable and concise (≤3
  sentences) — improves observability of what the agent "thought".
- Bug containment: if an agent misbehaves, staff catch it before
  external messages send.

**Negative**
- Slower than full autonomy; not the right end state for production at
  scale.
- Staff workload doesn't drop as much as it could.
- Risk: staff fall into "rubber stamp" mode and approve without reading.
  Mitigation: rationale display is the most prominent UI element;
  approve button below.

## Future evolution

Production deployment may move some routine state transitions to
auto-approval based on a confidence threshold + risk class. For now,
default-deny autonomy.

## Out of scope

- Risk classification of transitions
- Confidence threshold tuning
- Auto-approval governance / kill switches
