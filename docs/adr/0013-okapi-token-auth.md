# ADR 0013 — Okapi token auth for `HttpReShareClient`

**Status:** Proposed
**Date:** 2026-05-03

## Context

`HttpReShareClient` (`src/agora/clients/reshare.py`) wires HTTP Basic
auth against the mod-rs module's direct port. Per the module
docstring (verified 2026-05-02 against mod-rs master) Basic only
works "when permissions are disabled" — i.e. when the request hits
the module port directly without going through the FOLIO Okapi
gateway. Production FOLIO deploys put Okapi in front of every
module: requests carry an `X-Okapi-Token` and module-direct ports
are firewalled off.

Today the client is unreachable from a real consortium tenant
because:

1. The base URL would be the Okapi gateway, not the module.
2. Okapi rejects requests without a valid `X-Okapi-Token`.
3. We have no token-acquisition flow and no token cache.

Backlog item #1 ("real ReShare wire") is the gating piece for items
#4 (SHIP-compensator NCIP rollback), #5 (`item_id = reshare_id`
approximation), and #10 (ISO 18626 XSD validation on outbound wire).
Each of those needs real-tenant payloads to lock down. None of them
can land until the auth story works.

This ADR is **PR-A** in the four-PR slicing for backlog #1:

- **PR-A (this ADR):** Okapi token auth + settings + tests. Mock
  client still wins in `app.py:260`.
- PR-B: Wire `get_client()` factory in `app.py`, smoke-test gate.
- PR-C: Live-tenant payload probe; tighten `_parse` + `request_payload`
  contract.
- PR-D: `recall_request` mod-rs action mapping.

## Decision

Add a new `OkapiAuth(httpx.Auth)` class that owns the token
lifecycle, and wire it into `HttpReShareClient.__init__` as the auth
strategy when an Okapi URL is configured. Existing Basic-auth path
remains the default for dev (mod-rs module-direct, permissions
disabled).

### Login endpoint pick: `/authn/login` (legacy)

FOLIO ships two login endpoints:

| Endpoint | Token delivery | Refresh model |
|---|---|---|
| `POST /authn/login` (legacy) | `x-okapi-token` response header | No expiry on the access token in classic Okapi; token until logout |
| `POST /authn/login-with-expiry` (newer) | Access token via `folioAccessToken` cookie + ~10min expiry; refresh token via `folioRefreshToken` cookie | Refresh by POST to `/authn/refresh` with refresh-cookie |

We pick `/authn/login` for PR-A because:

1. It's in the long-tail of FOLIO releases (R-series and later all
   ship it; many consortium deploys still use it as primary).
2. The token-in-response-header pattern is trivially testable with
   `httpx.MockTransport` — no cookie-jar + refresh-token state to
   mock.
3. We have no live tenant to test against. Picking the simpler flow
   first lets us land + verify against a fake Okapi, then upgrade if
   the target tenant only ships the expiry variant.

The auth class is structured so swapping to `/authn/login-with-expiry`
is a one-method change (`_login_request` + token-extraction) plus
adding refresh logic. We do not try to support both speculatively.

**Open assumption to verify against a live tenant:** the target
consortium's Okapi accepts `/authn/login` and returns
`x-okapi-token`. If not, file a follow-up ADR (or amend this one)
for the expiry variant.

### Settings shape

One new setting:

```python
okapi_url: str = Field(default="", alias="OKAPI_URL")
```

Semantics: when `OKAPI_URL` is set, login POSTs go to
`{OKAPI_URL}/authn/login` and the auth class is wired in. When unset,
the existing Basic-auth fallback applies. We **reuse**
`RESHARE_USER` and `RESHARE_PASSWORD` as the Okapi credentials
because in FOLIO the same identity logs into Okapi and accesses
mod-rs through it; introducing parallel `OKAPI_USER` / `OKAPI_PASSWORD`
would invite drift between two settings that always carry the same
value in practice.

`RESHARE_TENANT` continues to supply `X-Okapi-Tenant` for both the
login request and downstream module calls.

`RESHARE_BASE_URL` continues to be the base for `/rs/...` paths. In
production this typically points at the Okapi gateway (same host as
`OKAPI_URL`); operators set both to the same value. We do **not**
auto-derive one from the other in this ADR — that's a follow-up
once we have a live tenant and know whether the same host is always
correct.

### `OkapiAuth` design

`httpx.Auth` exposes three hooks: `auth_flow`, `sync_auth_flow`,
`async_auth_flow`. The async client always calls `async_auth_flow`,
and the docstring is explicit that I/O-doing or lock-holding auth
schemes MUST override the async variant rather than rely on the
default-generator fallback.

We override `async_auth_flow` only. Calling the auth class from a
sync client is unsupported (we have none); the base
`sync_auth_flow` defers to `auth_flow` which yields the request
unchanged — silent auth bypass. The auth class is documented
"async-only"; if a sync need arises later, override
`sync_auth_flow` to raise `RuntimeError`.

Flow:

1. **First request, no token cached.** Acquire `asyncio.Lock`,
   double-check the cache (another task may have raced ahead),
   yield a login request through the same client, extract the
   token from `x-okapi-token`, store it, release the lock, attach
   the token to the original request, yield it.
2. **Subsequent request, token cached.** Skip the lock, attach
   token, yield original request.
3. **401 on the original request.** The cached token may have
   expired. Capture the stale value, acquire the lock, only
   re-login if the token hasn't already been refreshed by another
   task (`if self._token == stale_token`), then retry the original
   request once. A second 401 returns to the caller — we don't
   loop.

The `asyncio.Lock` matters because the auth flow is invoked once
per outbound request. Without it, N concurrent in-flight requests
all observing a missing/expired token would issue N parallel logins.
Holding the lock across the `yield self._login_request()` is
correct: the yield suspends the async generator until the response
arrives, the lock stays held, no other task can race the same
login.

### What does NOT change in this PR

Per the PR-A scope:

- `app.py:260` continues to hard-code `MockReShareClient()`. No
  factory flip, no behaviour change for any running code.
- `recall_request` continues to raise `ClientError`.
- `_parse` and the `request_payload` contract are untouched.
- `runbook.md` env-var table is not updated (next docs-stale-check
  pass picks it up).

The new auth code is dormant until PR-B flips the factory.

## Consequences

**Positive**

- Real-tenant integration becomes a one-config-flip: set
  `OKAPI_URL`, `RESHARE_USER`, `RESHARE_PASSWORD`, point
  `RESHARE_BASE_URL` at the Okapi gateway, and `HttpReShareClient`
  authenticates correctly.
- The token cache + lock mean the client doesn't hammer
  `/authn/login` once per outbound request. One login per process
  + per token-expiry event.
- 401-refresh-and-retry is automatic. Outbox worker code stays
  oblivious to token lifecycle.
- The auth class is testable in isolation against
  `httpx.MockTransport` with no real Okapi.

**Negative**

- One more class with concurrency primitives in it. The lock is the
  small-but-real complexity surface; misuse (e.g. holding it across
  unrelated I/O) would serialize all requests.
- The login request goes through the same `httpx.AsyncClient` as
  the data requests — meaning it inherits the same `timeout=10.0`.
  A slow Okapi at startup blocks the first outbound saga step.
  Acceptable for now (matches every other ReShare call); revisit if
  it bites in PR-C.
- We carry the `okapi_url` setting in `config.py` even though
  nothing reads it until a real client is constructed. The
  validation surface grows by one field.
- Tests inline fake credentials (`"u" / "p"`); detect-secrets
  baseline regen needed pre-push.

**Neutral / deferred**

- `/authn/login-with-expiry` support — defer to live-tenant
  feedback.
- Token persistence across worker restarts — out of scope; tokens
  are cheap to re-acquire.
- Per-tenant auth (multi-consortium) — out of scope; we have one
  `RESHARE_TENANT` today.

## Implementation steps (this PR)

1. Add `OKAPI_URL` field to `Settings` in `config.py`.
2. Create `src/agora/clients/okapi_auth.py` with `OkapiAuth(httpx.Auth)`.
3. In `HttpReShareClient.__init__`, pick `OkapiAuth` when
   `settings.okapi_url` is truthy, else fall back to existing
   `BasicAuth` path.
4. Add `tests/test_okapi_auth.py` covering: happy path, 401-refresh,
   concurrent-requests-share-login, missing-creds rejection.
5. Regenerate `.secrets.baseline` (test fixtures inline `"u"`/`"p"`).
6. Verify triple gate (`pytest -q` + `ruff` + `mypy --strict`) and
   `make audit` clean before push.

## Alternatives reconsidered

| Alternative | Reason rejected |
|---|---|
| Reuse `RESHARE_BASE_URL` + add `OKAPI_AUTH_ENABLED` flag | Overloads `reshare_base_url`; one flag + one URL beats one flag + one URL with implicit dual-meaning |
| Add `OKAPI_USER` / `OKAPI_PASSWORD` separate from `RESHARE_USER` / `RESHARE_PASSWORD` | Two parallel settings always carrying the same value invites drift; FOLIO uses one identity end-to-end |
| Login per request (no cache) | Would hammer `/authn/login` for every saga step; defeats the point |
| Cache token in DB so workers share | Premature; one cache per process is fine until we run worker fleets in different containers, which we don't today |
| Implement both `/authn/login` and `/authn/login-with-expiry` upfront | Speculative without a live tenant; doubles the surface and the test matrix |
| Skip auth entirely, document as TODO until PR-B | Leaves the gap exposed at the moment we'd most want it filled |

## Open questions

- Does the target consortium's Okapi accept `/authn/login`? Confirm
  in PR-C against a live tenant; if not, swap to the expiry variant
  and amend this ADR.
- Should `OKAPI_URL` default to `RESHARE_BASE_URL` when unset and
  `RESHARE_TENANT` is non-default? Considered "auto-derive"
  ergonomics; deferred until we know whether real deploys ever put
  Okapi on a different host than mod-rs (we suspect they don't, but
  guessing now would risk a wrong default).
- Do we need to surface token-acquisition failures distinctly from
  request failures? Today both raise `ClientError`. A future
  observability pass might want a separate `AuthError` subtype.
