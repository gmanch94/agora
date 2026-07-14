---
name: outbox-handler-add
description: Scaffold a new outbox target handler (NCIP, webhook, peer relay, etc.) following the commit-then-enqueue pattern from ADR-0011. Use when adding a new external system that saga steps need to dispatch to asynchronously, when the user asks to "wire up an outbox handler for X", or when extending the OutboxWorker registry. Walks the developer through the handler signature, idempotency-key contract, lifespan registration, and the test pattern.
---

# outbox-handler-add

The outbox table buffers outbound dispatches so a saga step can
commit its ledger event atomically with the enqueue and let a
separate worker handle delivery (ADR-0011: commit-then-enqueue).
Today there is one handler — `make_reshare_handler` for ReShare —
and the system is designed for more (NCIP next, then ILS-specific
relays, then peer webhooks). This skill is the lockstep checklist
for adding one without breaking the replay-safety invariant.

## When to invoke

- User says "add an outbox handler for X", "wire up the NCIP outbox",
  "scaffold a webhook target", "next outbox integration"
- A new saga step needs to dispatch to an external system that the
  ReShare client doesn't cover
- The `target` column in `outbox` needs a new value other than
  `"reshare"`

## Why a handler — and not an inline call

Forward steps that touch external systems should return one or more
`OutboxIntent` rows on their `StepResult`. The coordinator writes
the `saga_event` row **and** the outbox rows in the same DB
transaction. The worker drains the rows asynchronously. **Do not**
add a new `await client.do_thing()` inline in `saga/flows.py` — that
breaks the atomicity guarantee that makes the pipeline crash-safe.

The one exception is APPROVE forward (see CLAUDE.md known-gaps):
the saga ledger needs the returned `reshare_id` stamped onto its
forward-event payload, so APPROVE still calls
`TransactionAgent.submit_to_supplier` inline. Migrating that to
full outbox needs an `APPROVING` intermediate state — future ADR.
**Do not** mimic APPROVE for a new target unless that ADR lands.

## Handler contract

A handler is one coroutine:

```python
Handler = Callable[[dict[str, Any], str], Awaitable[None]]
```

- **Args**: `(payload, idempotency_key)`. The key is sourced from
  the **outbox row**, not the payload — that is what makes
  dispatch replay-safe even if the worker crashes after the remote
  call but before `mark_delivered` commits.
- **Success**: return `None`. Worker writes `delivered_at` in the
  same `mark_delivered` step.
- **Failure**: raise. The worker increments `attempts`, records
  `last_error = str(exc)`, schedules the next retry at
  `now + base_backoff_secs * 2**attempts`, and after
  `max_attempts` flips status to `dead_letter`.
- **Idempotency**: must be safe to invoke twice with the same
  `idempotency_key`. We assume the *external* system either
  honours the key or its API is naturally idempotent on the
  payload (PUT-style); if it isn't, the handler must do
  client-side dedup (e.g. look up before sending).

## Files to touch

For target name `<X>` (e.g. `ncip`, `peer_webhook`):

1. **`src/agora/clients/<X>.py`** — async client wrapping the
   external system. Signature for each action mirrors
   `ReShareClient`: `async def do_thing(*, idempotency_key: str,
   ...) -> SomeResponse`.
2. **`src/agora/saga/outbox.py`** — add a builder
   `make_<X>_handler(client) -> Handler` next to
   `make_reshare_handler`. Dispatches on
   `payload["action"]`. Mirror the validation: assert
   `action: str`, `args: dict`, then `getattr(client, action)`.
3. **`src/agora/api/app.py`** (lifespan) — register the handler in
   the `OutboxWorker` map:
   ```python
   handlers = {
       "reshare": make_reshare_handler(reshare),
       "<X>": make_<X>_handler(<X>_client),
   }
   worker = OutboxWorker(get_sessionmaker(), handlers, ...)
   ```
   Construct the client in lifespan startup so it shares the
   process-wide httpx pool.
4. **`src/agora/saga/flows.py`** — for each step that should now
   dispatch via this target, change its forward function to return
   an `OutboxIntent(target="<X>", ...)` rather than calling the
   client inline. Compensators follow the same pattern.
5. **`src/agora/config.py`** — add a `<X>_*` `Field` group if the
   client needs base URL / credentials / timeouts. Mirror
   `reshare_*` shape (`Field(..., alias="AGORA_<X>_BASE_URL")`).
6. **`tests/test_outbox.py`** — add a test that builds a fake
   `<X>Client`, hands it to `make_<X>_handler`, enqueues a row
   with `target="<X>"`, and asserts the worker calls the right
   action with the row's idempotency key.
7. **`docs/adr/`** — if this target introduces a new external
   contract or a different idempotency story (e.g. NCIP's
   `RequestId` semantics), drop an ADR via the `adr-new` skill.
8. **`docs/prd/03-saga-and-idempotency.md`** — extend the outbox
   `target` enum list (`'reshare'`, `'ncip'`, …).
9. **`CLAUDE.md`** — update known-gaps if the new client is
   mock-only or has known unverified surface.

## Skeleton: handler builder

```python
def make_<X>_handler(client: <X>Client) -> Handler:
    """Build a Handler that dispatches ``payload['action']`` on ``client``.

    Expected payload shape::

        {"action": "<verb>",
         "args": { ...method kwargs (excluding idempotency_key)... }}
    """

    async def handler(payload: dict[str, Any], idempotency_key: str) -> None:
        action = payload.get("action")
        args = payload.get("args", {})
        if not isinstance(action, str):
            raise ValueError(f"<X> outbox payload missing 'action': {payload!r}")
        if not isinstance(args, dict):
            raise ValueError(f"<X> outbox payload 'args' must be dict: {payload!r}")

        method = getattr(client, action, None)
        if method is None or not callable(method):
            raise ValueError(f"<X> client has no action {action!r}")

        await method(idempotency_key=idempotency_key, **args)

    return handler
```

## Skeleton: flow OutboxIntent

In `saga/flows.py`, a forward step that previously called
`client.do_thing(...)` inline becomes:

```python
async def my_forward(ctx: StepContext) -> StepResult:
    intent = OutboxIntent(
        target="<X>",
        idempotency_key=ctx.idempotency_key,  # or new_idempotency_key("<step>")
        payload={"action": "do_thing", "args": {"saga_id": str(ctx.saga_id), ...}},
    )
    return StepResult(
        state_after=LifecycleState.<NEW_STATE>,
        outbox=[intent],
        rationale="Enqueued <X> dispatch.",
    )
```

The coordinator writes the `saga_event` and the `OutboxIntent` row
in the same transaction. The worker dispatches asynchronously.

## Skeleton: test

```python
@pytest.mark.asyncio
async def test_<X>_handler_dispatches_action(sessionmaker):
    fake = FakeXClient()  # records (action, idempotency_key, kwargs)
    handler = make_<X>_handler(fake)

    await handler(
        {"action": "do_thing", "args": {"resource_id": "abc"}},
        idempotency_key="dothing_01HX...",
    )

    assert fake.calls == [
        ("do_thing", "dothing_01HX...", {"resource_id": "abc"}),
    ]
```

For end-to-end, follow the `test_outbox_worker_*` patterns: build
an `OutboxWorker` with a `{"<X>": handler}` map, enqueue a row via
`outbox_enqueue`, call `worker.drain_until_empty`, and assert
`stats.delivered == 1`.

## Single-drainer caveat

`OutboxWorker` assumes one drainer per DB. Running two against the
same table double-delivers because `outbox_pending` has no row-level
lock. The fix is `SELECT … FOR UPDATE SKIP LOCKED` (Postgres-only) —
out of scope for the prototype, flagged in CLAUDE.md known-gaps.
Don't add a second handler instance until that ADR lands.

## Out of scope

- Implementing the external client itself (use the relevant
  domain skill — e.g. NCIP semantics, peer-relay specifics)
- Wire-level XSD/Schema validation (delegate to the target's
  own library or, for ReShare, to mod-rs)
- Multi-worker safety (separate ADR)

## Pair tools

- `adr-new` — when this target introduces a new external contract
  worth locking in
- `docs-stale-check` — run after touching `flows.py` + `app.py` +
  `outbox.py` + `config.py` to surface PRD/ADR/runbook drift
