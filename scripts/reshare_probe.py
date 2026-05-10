"""ReShare local sandbox probe.

Verifies the three unresolved surfaces in HttpReShareClient against a
real mod-rs instance running at localhost:8081 (the `reshare` Docker
Compose profile).

Usage
-----
    # Bring up the sandbox first:
    make reshare-up

    # Wait for the mod-rs healthcheck to go green, then run:
    make reshare-probe
    # or directly:
    python scripts/reshare_probe.py

Environment variables (all have defaults for local sandbox):
    RESHARE_BASE_URL    default: http://localhost:8081
    RESHARE_TENANT      default: consortium-a
    RESHARE_USER        default: admin
    RESHARE_PASSWORD    default: admin

Probe sequence
--------------
1. Tenant init  - POST /_/tenant (idempotent; skipped if already done)
2. Health check   - GET  /rs/patronrequests?perPage=0
3. Create request  - POST /rs/patronrequests with a minimal body;
   print the *full* response JSON (unknown #1: create-request body
   shape, unknown #2: response field names).
4. Read request back  - GET /rs/patronrequests/{id} for complete field
   listing.
5. Recall probe  - attempt performAction with candidate action strings to
   locate the real recall action (unknown #3).
6. Summary  - print findings table.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL: str = os.getenv("RESHARE_BASE_URL", "http://localhost:8081").rstrip("/")
TENANT: str = os.getenv("RESHARE_TENANT", "diku")  # FOLIO tenant IDs: lowercase alphanum only (no hyphens)
USER: str = os.getenv("RESHARE_USER", "admin")
PASSWORD: str = os.getenv("RESHARE_PASSWORD", "admin")


def _headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    h = {
        "X-Okapi-Tenant": TENANT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def _client() -> httpx.Client:
    auth = httpx.BasicAuth(USER, PASSWORD) if USER else None
    return httpx.Client(timeout=90.0, auth=auth)  # first POST can be slow (lazy tenant schema init)


# ── Helpers ───────────────────────────────────────────────────────────────────

def pp(label: str, data: Any) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print("=" * 60)
    print(json.dumps(data, indent=2, default=str))


def banner(msg: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {msg}")
    print("-" * 60)


# ── Steps ─────────────────────────────────────────────────────────────────────

def wait_for_mod_rs(client: httpx.Client, max_wait: int = 180) -> None:
    """Poll until mod-rs is up (max_wait seconds)."""
    url = f"{BASE_URL}/rs/patronrequests?perPage=0"
    deadline = time.monotonic() + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            resp = client.get(url, headers=_headers())
            if resp.status_code in (200, 204, 422, 500):
                # 500 = TenantNotFoundException (tenant not yet initialised)  -
                # server IS up; init_tenant step will create the schema.
                print(f"  mod-rs is up (status {resp.status_code}, attempt {attempt})")
                return
            print(f"  [{attempt}] status {resp.status_code} -- waiting ...")
        except httpx.RequestError as exc:
            print(f"  [{attempt}] connection error: {exc}  - waiting ...")
        time.sleep(5)
    raise SystemExit(f"mod-rs not reachable at {BASE_URL} after {max_wait}s")


def init_tenant(client: httpx.Client) -> None:
    """POST /_/tenant to create the mod-rs schema for TENANT.

    Idempotent: 400 with 'already exists' body is treated as success.
    Some mod-rs versions return 200 if already initialised.
    """
    banner(f"Step 1  - Tenant init (POST /_/tenant, tenant={TENANT!r})")
    body = {
        "id": TENANT,
        "parameters": [
            {"key": "loadReference", "value": "true"},
            {"key": "loadSample", "value": "false"},
        ],
    }
    resp = client.post(f"{BASE_URL}/_/tenant", json=body, headers=_headers())
    print(f"  HTTP {resp.status_code}")
    if resp.status_code in (200, 201, 204):
        print("  Tenant init: OK")
    elif resp.status_code == 400 and "exist" in resp.text.lower():
        print("  Tenant already initialised  - continuing")
    elif resp.status_code == 422:
        # Some modules return 422 if schema already migrated
        print(f"  Tenant init returned 422 (may already exist): {resp.text[:200]}")
    else:
        print(f"  WARNING: unexpected response  - {resp.text[:400]}")
        print("  Continuing anyway (schema may already exist from DB init)")


def health_check(client: httpx.Client) -> None:
    banner("Step 2  - Health check (GET /rs/patronrequests?perPage=0)")
    resp = client.get(
        f"{BASE_URL}/rs/patronrequests?perPage=0", headers=_headers()
    )
    print(f"  HTTP {resp.status_code}")
    if resp.status_code in (200, 204):
        print("  Health: OK")
        data = resp.json()
        if isinstance(data, dict):
            total = data.get("totalRecords", "?")
        else:
            total = len(data) if isinstance(data, list) else "?"
        print(f"  Existing requests: {total}")
    else:
        print(f"  WARN: {resp.text[:300]}")


def create_request(client: httpx.Client) -> str:
    """POST /rs/patronrequests  - probe unknown #1 (body shape) and #2 (response fields).

    Returns the created reshare_id.
    """
    banner("Step 3  - Create request (POST /rs/patronrequests)")

    # Minimal body based on PatronRequest Grails domain conventions.
    # mod-rs uses camelCase field names matching its GORM domain class.
    # Probe deliberately includes the fields HttpReShareClient sends so
    # we can verify which ones mod-rs actually consumes vs ignores.
    body: dict[str, Any] = {
        # Core ISO 18626 Request fields
        "title": "Agora Probe  - The Art of Distributed Systems",
        "author": "Claude, Agora",
        "isbn": "978-0-000000-00-0",
        # Institution symbols (borrower / supplier)
        "requestingInstitutionSymbol": "CONSORTIUM-A",
        "supplyingInstitutionSymbol": "CONSORTIUM-B",
        # Patron details
        "patronIdentifier": "probe-patron-001",
        "patronType": "STUDENT",
        # Request metadata
        "pickupLocation": "Main Library Desk",
        "neededBy": "2026-12-31",
    }
    pp("Request body", body)

    resp = client.post(
        f"{BASE_URL}/rs/patronrequests",
        json=body,
        headers=_headers(idempotency_key="probe-create-001"),
    )
    print(f"\n  HTTP {resp.status_code}")

    if resp.status_code not in (200, 201):
        print(f"  ERROR: {resp.text[:600]}")
        raise SystemExit(
            f"POST /rs/patronrequests failed ({resp.status_code}). "
            "Is tenant init complete?  Run `make reshare-probe` again after "
            "the mod-rs container healthcheck turns green."
        )

    data = resp.json()
    pp("Create response (FULL  - unknown #2 resolved here)", data)

    reshare_id = str(data.get("id") or data.get("hrid") or "")
    hrid = str(data.get("hrid") or "")
    state = data.get("state")
    if isinstance(state, dict):
        state_str = state.get("code") or state.get("label") or str(state)
    else:
        state_str = str(state or "")

    print("\n  -- Key fields (HttpReShareClient._parse inputs) --")
    print(f"    id              : {data.get('id')!r}")
    print(f"    hrid            : {data.get('hrid')!r}")
    print(f"    state           : {state!r}  ->  flattened: {state_str!r}")
    print(f"    isoMessageId    : {data.get('isoMessageId')!r}")
    print(f"    messageId       : {data.get('messageId')!r}")
    print(f"    supplyingAgencyId: {data.get('supplyingAgencyId')!r}")

    print(f"\n  reshare_id for subsequent steps: {reshare_id!r} (hrid={hrid!r})")
    return reshare_id


def read_request(client: httpx.Client, reshare_id: str) -> None:
    """GET /rs/patronrequests/{id}  - full field listing."""
    banner(f"Step 4  - Read request back (GET /rs/patronrequests/{reshare_id})")
    resp = client.get(
        f"{BASE_URL}/rs/patronrequests/{reshare_id}", headers=_headers()
    )
    print(f"  HTTP {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        pp("GET response (all fields  - check for isoMessageId, supplyingAgencyId)", data)
        # Surface available actions if present in response
        actions = data.get("availableActions") or data.get("possibleActions")
        if actions:
            pp("Available actions from GET response (key finding for unknown #3)", actions)
        else:
            print("\n  NOTE: no availableActions/possibleActions key in GET response")
    else:
        print(f"  WARN: {resp.text[:300]}")


# Candidate action strings for recall (unknown #3).
# We try each against performAction and log what happens.
_RECALL_CANDIDATES: list[tuple[str, str]] = [
    ("requesterRecall", "ISO 18626 Recall  - most likely FOLIO action name"),
    ("borrowerRecall", "alternative borrower-side naming"),
    ("recall", "short form"),
    ("sendMessage", "message-based approach (ISO 18626 RequestingAgencyMessage)"),
    ("message",     "Okapi-native message action"),
    ("patronRecall", "patron-initiated variant"),
    ("escalate",    "long-shot"),
]


def probe_recall(client: httpx.Client, reshare_id: str) -> dict[str, Any]:
    """Try each candidate recall action and record the HTTP response.

    Returns a dict of {action_str: (status_code, response_body)}.
    A 2xx means the action is valid.  A 422 with 'invalid action' means
    it's recognised but rejected; a 400/404 means not found.
    """
    banner("Step 5  - Recall action probe (unknown #3)")
    print(
        "  Trying each candidate action against performAction endpoint.\n"
        "  2xx = valid;  422 = recognised but rejected for state;  400/404 = not found.\n"
    )

    results: dict[str, Any] = {}

    for action, description in _RECALL_CANDIDATES:
        body = {"action": action, "actionParams": {}}
        resp = client.post(
            f"{BASE_URL}/rs/patronrequests/{reshare_id}/performAction",
            json=body,
            headers=_headers(idempotency_key=f"probe-recall-{action}"),
        )
        verdict = "OK VALID" if resp.status_code < 300 else f"X  {resp.status_code}"
        print(f"  {verdict:<12}  {action:<20}  ({description})")
        body_excerpt = resp.text[:200].replace("\n", " ")
        if resp.status_code < 300:
            print(f"             response body: {body_excerpt}")
        elif "invalid" in resp.text.lower() or "unknown" in resp.text.lower():
            print(f"             server says: {body_excerpt}")
        results[action] = {"status": resp.status_code, "body": resp.text[:400]}

    return results


def print_summary(create_id: str, recall_results: dict[str, Any]) -> None:
    banner("SUMMARY  - findings for HttpReShareClient update")

    valid_recall = [a for a, r in recall_results.items() if r["status"] < 300]
    rejected_recall = [
        a
        for a, r in recall_results.items()
        if r["status"] in (422, 409)
    ]

    print("""
Unknown #1  - POST /rs/patronrequests body shape
  See 'Create response' above.  The probe body used camelCase fields:
    title, author, isbn, requestingInstitutionSymbol,
    supplyingInstitutionSymbol, patronIdentifier, patronType,
    pickupLocation, neededBy.
  Cross-reference with the full response to confirm which fields
  mod-rs stored vs ignored.

Unknown #2  - Response field names beyond id/hrid/state
  See 'Create response' and 'GET response' sections above.
  Update HttpReShareClient._parse() if isoMessageId / supplyingAgencyId
  appear under different keys in the real response.
""")

    print("Unknown #3  - recall_request action string:")
    if valid_recall:
        print(f"  FOUND: {valid_recall}")
        print(
            f"  Set HttpReShareClient._ACTION_REQUESTER_RECALL = {valid_recall[0]!r}"
            " and remove the ClientError raise."
        )
    elif rejected_recall:
        print(
            f"  State-rejected (422/409): {rejected_recall}"
        )
        print(
            "  These action strings exist but are not valid in the 'Requested' state."
        )
        print(
            "  Advance the saga state first (e.g. to SHIPPED), then retry."
        )
    else:
        print(
            "  No 2xx or 422 found  - recall may require a different API surface."
        )
        print(
            "  Check mod-rs Actions.groovy in the container:"
        )
        print(
            "    docker exec agora-mod-rs find / -name Actions.groovy 2>/dev/null"
        )

    print(f"\n  reshare_id used: {create_id!r}")
    print("\nDone.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("ReShare probe  - local mod-rs sandbox")
    print(f"  BASE_URL  : {BASE_URL}")
    print(f"  TENANT    : {TENANT}")
    print(f"  AUTH      : {'BasicAuth(' + USER + ')' if USER else 'none'}")

    with _client() as client:
        banner("Waiting for mod-rs to be ready ...")
        wait_for_mod_rs(client)

        init_tenant(client)
        health_check(client)
        reshare_id = create_request(client)
        read_request(client, reshare_id)
        recall_results = probe_recall(client, reshare_id)
        print_summary(reshare_id, recall_results)


if __name__ == "__main__":
    sys.exit(main())  # type: ignore[arg-type]
