---
name: saga-trace
description: Pretty-print the event timeline for an Agora saga given a saga_id or a JSON dump of saga_event rows. Use when debugging a stuck or unexpectedly-terminal saga, when verifying a compensator actually ran, or when a user asks "what happened with saga X". Reads from the live DB if `DATABASE_URL` is set; otherwise expects a JSON file path.
---

# saga-trace

Render an Agora saga's `saga_event` rows as a readable timeline. The
ledger is the source of truth — this skill turns it into something a
human can scan in five seconds.

## When to invoke

- User asks "why did saga `<uuid>` end in state X" / "trace saga X"
- Debugging a failed forward step or compensator
- Verifying a gate was actually committed before a forward ran
- After a chaos test, checking the ledger landed in a consistent
  terminal state

## What to do

### Step 1 — locate the events

Try in this order:

1. **Live DB** — if `DATABASE_URL` (or `DB_URL`) env var is set, query
   directly. Use `.venv/Scripts/python.exe` and the project's session
   factory:

   ```python
   import asyncio, json
   from uuid import UUID
   from agora.saga.db import get_sessionmaker
   from agora.saga.ledger import SagaLedger

   async def dump(saga_id: str) -> None:
       sm = get_sessionmaker()
       async with sm() as s:
           ledger = SagaLedger(s)
           saga = await ledger.get_saga(UUID(saga_id))
           events = await ledger.events_for(UUID(saga_id))
           print(json.dumps({
               "saga_id": str(saga.id),
               "current_state": saga.current_state,
               "events": [e.model_dump(mode="json") for e in events],
           }, indent=2, default=str))
   asyncio.run(dump("<saga_id>"))
   ```

2. **JSON file** — user pastes/points to a file containing the events
   array (e.g. captured from `GET /sagas/{id}` or the demo output).

3. **Demo output** — if user mentions the happy-path demo, run
   `python -m agora.demos.happy_path` and parse its stdout.

### Step 2 — render the timeline

Print one line per event, columns aligned:

```
SAGA <id> -- final state: <state>
  seq=NN  <kind:12>  <step:10>  <before:>10> -> <after:<10>  outcome=<outcome:10>  actor=<actor>
                                                                                   rationale=<one-line>
```

Where:
- `seq` — event sequence (zero-padded 2 digits)
- `kind` — `forward` / `compensator` / `gate` / `observation`
- `step` — step name (`submit`, `route`, `approve`, `ship`,
  `return_item`, ...)
- `before` / `after` — lifecycle state names from the row
- `outcome` — `pending` / `committed` / `failed`
- `actor` — who triggered it (`patron:<id>`, `staff:<id>`,
  `agent:<name>`, `system`)
- `rationale` — only show if non-empty; indent under the row

### Step 3 — flag anomalies

After printing, scan for and call out:

- **Gate-without-forward**: a `gate outcome=committed` for step X with
  no following `forward` for the same step.
- **Forward-without-gate**: a `forward outcome=committed` for step X
  with no preceding `gate outcome=committed`. THIS IS A BUG — the
  invariant says forwards require committed gates.
- **Compensator without committed forward**: a `compensator` event
  for a step that has no `forward outcome=committed` ancestor.
- **Non-contiguous seq**: gaps in the seq column.
- **Duplicate idempotency_key**: two rows with the same key
  (shouldn't be possible — UNIQUE constraint — but catch it).
- **Terminal state with pending events**: saga is in `returned` /
  `cancelled` / etc. but has `outcome=pending` events.

For each anomaly, print a `WARN:` line with the seq number(s) and
short explanation.

### Step 4 — summary footer

```
Summary:
  total events: N
  forward steps committed: N
  gates committed: N
  compensators run: N
  terminal: yes/no (state=<state>)
  anomalies: <count> (see WARN above)
```

## Input shapes accepted

- `saga_id` as a UUID string (queries live DB)
- Path to a JSON file with `{events: [...], current_state: "..."}`
- Path to a JSON file with just `[...]` (an events array; current
  state inferred from the last event's `state_after`)
- Raw JSON pasted into the prompt (parse it directly)

## Don'ts

- Don't write or modify any saga rows.
- Don't query Postgres without confirming the URL is a dev/test DB if
  the user hasn't been explicit.
- Don't dump huge `payload` blobs inline — show the keys, and only
  expand a payload on user request.
