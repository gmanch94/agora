---
name: reshare-probe
description: Probe a running FOLIO/ReShare (mod-rs) instance to verify the actual endpoint paths, request/response shapes, and idempotency-header handling — then diff against `HttpReShareClient` in `src/agora/clients/reshare.py`. Use before flipping `reshare_enabled=true` against a real instance, or when an integration error suggests our endpoint guesses are wrong.
---

# reshare-probe

`HttpReShareClient` was written from public mod-rs documentation, not
from a running instance. The HTTP shape, idempotency-key header
handling, and error mapping are correct; the exact paths and payload
keys may not be. This skill closes that gap.

## When to invoke

- Before flipping `RESHARE_ENABLED=true` for the first time against a
  given ReShare deployment
- Integration test fails with 404 / 400 from ReShare
- New version of mod-rs is deployed and we want to spot drift
- User asks "do our reshare endpoints actually work" / "verify reshare
  client"

## Required information

- **Base URL** — `RESHARE_BASE_URL` (e.g. `http://localhost:8080`)
- **Tenant** — `RESHARE_TENANT` (Okapi tenant header)
- **Auth** — `RESHARE_USER` / `RESHARE_PASSWORD` if required
- **Whether the instance has test data** — needed for cancel / ship /
  return probes (otherwise probe send_request only)

If any of these are missing, ASK the user before probing. Don't
probe production without explicit confirmation.

## What to do

### Step 1 — health check

```python
import asyncio, httpx
async def probe():
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{base}/admin/health",
                        headers={"X-Okapi-Tenant": tenant})
        print("health:", r.status_code, r.text[:200])
asyncio.run(probe())
```

If health fails: STOP. Tell user the instance isn't reachable.

### Step 2 — probe each method

For each `HttpReShareClient` method, build a minimal valid payload
and send it with a unique `Idempotency-Key`. Capture:

- HTTP status
- Response body (first 500 chars + structure summary)
- Whether `Idempotency-Key` header was respected on a replay (send the
  same key twice, expect identical body or `409`)

Methods to probe (in order — the later ones depend on a created
request):

1. `send_request` → `POST /rs/patronrequests`
2. `cancel_request` → `POST /rs/patronrequests/{id}/performAction`
   with `{"action": "RequesterCancel"}` (only if instance has a
   non-terminal request to cancel; otherwise skip)
3. `confirm_shipment` → action `SupplierMarkShipped`
4. `confirm_return` → action `RequesterMarkReturned`
5. `recall_request` → action `SupplierRecall`

### Step 3 — diff

For each method, compare:

| Field | Our guess (in code) | Actual response |
|---|---|---|
| Path | `/rs/patronrequests` | (probed) |
| Body keys | `{supplier, request}` | (probed) |
| Response id key | `data.id` or `data.hrid` | (probed) |
| ISO message id key | `isoMessageId` / `messageId` | (probed) |
| Supplier symbol key | `supplyingAgencyId` | (probed) |
| State key | `state` | (probed) |

Where they disagree, propose a code patch to `clients/reshare.py`.

### Step 4 — write the report

Save to `docs/integration/reshare-probe-<date>.md`:

- Instance URL + tenant + version (from `/admin/health` or
  `/_/proxy/modules`)
- Per-method: path, sample request, sample response, status, idem
  behaviour
- List of mismatches vs our code with suggested patches
- Whether `Idempotency-Key` was honoured (critical — we rely on it)

## Don'ts

- **Never probe a production ReShare without explicit user
  confirmation** that the side effects (creating a patron request,
  cancelling, etc.) are acceptable.
- Don't leave probe artifacts in the database. Use `cancel_request`
  on anything you `send_request`'d unless the user says otherwise.
- Don't update `HttpReShareClient` silently — surface the diff and
  ask before patching. The current code has a comment flagging
  endpoints as unverified; that comment must be updated when
  endpoints are confirmed.
- Don't skip the idempotency replay test — that's the most important
  finding. If ReShare doesn't honour `Idempotency-Key`, our outbox
  pattern needs different dedup.
