# ADR 0008 — ULID idempotency keys, threaded through the call graph

**Status:** Accepted
**Date:** 2026-05-02

## Context

User explicitly required idempotency everywhere. We need a key scheme
that:
- Uniquely identifies an intended operation
- Is sortable for debugging and ordered processing
- Can be generated client-side without coordination
- Is stable across retries

## Decision

Use **ULIDs** (Universally Unique Lexicographically Sortable Identifiers)
as idempotency keys throughout Agora. Threaded explicitly:

1. The saga coordinator generates an idempotency key when scheduling a
   forward step.
2. The key is passed as a parameter to the agent / client function.
3. Clients embed the key in the outbound request (HTTP `Idempotency-Key`
   header for REST; `messageId` for ISO 18626 messages).
4. The key is recorded in `saga_event.idempotency_key` (UNIQUE).
5. Inbound messages use the originator's `messageId` as their inbox
   dedup key.

Library: `python-ulid`.

## Consequences

**Positive**
- 26-char string, sortable by creation time — sorts events naturally.
- Uniqueness without coordination.
- Replay-safety: DB UNIQUE constraint catches duplicates.
- Debug-friendly: a single key follows an operation through ledger,
  outbox, ReShare logs.

**Negative**
- Threading the key through every function signature is verbose.
  Mitigation: encapsulate the key in a `SagaContext` object passed
  to step functions.
- Some external systems may not accept arbitrary idempotency keys.
  Mitigation: hash to fit when needed; record both forms in ledger.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| UUIDv4 | Not sortable; harder to debug ordered flows |
| UUIDv7 | Sortable but newer; ULID has wider library support |
| Auto-increment IDs | Require coordination; can't generate client-side |
| Hash of payload | Two semantically distinct retries with same payload would collide |

## Inbox/outbox interaction

- **Outbox row** holds the ULID; worker delivers to ReShare with
  `Idempotency-Key` header; ReShare returns same response on retry.
- **Inbox row** dedups by message originator's id (not our ULID); we
  store both.
