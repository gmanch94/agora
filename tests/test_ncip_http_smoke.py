"""Real mod-ncip smoke test — skips unless AGORA_TEST_NCIP_URL is set.

Exercises the live ``HttpNcipClient`` against a real FOLIO tenant with
mod-ncip deployed and configured.  Two tiers:

**Tier 1 — health probe (always runs when URL is set)**
  ``GET /admin/health`` — verifies TCP reachability, URL/path handling,
  and the FastAPI factory wiring.  Read-only; safe against any tenant.

**Tier 2 — checkout/checkin round-trip (opt-in)**
  ``check_out`` then ``check_in`` using a test item barcode and patron.
  Gated on ``AGORA_TEST_NCIP_ITEM_ID`` and ``AGORA_TEST_NCIP_PATRON_ID``.
  **Mutates ILS state** — point only at a dedicated test tenant or a
  known-safe item barcode that the tenant recirculates freely.

Local invocation::

    # Health probe only
    AGORA_TEST_NCIP_URL=http://mod-ncip.local:9090 \\
    RESHARE_TENANT=diku \\
    NCIP_AGENCY_ID=MY-LIB \\
        pytest tests/test_ncip_http_smoke.py -v

    # Full round-trip
    AGORA_TEST_NCIP_URL=http://mod-ncip.local:9090 \\
    RESHARE_TENANT=diku \\
    NCIP_AGENCY_ID=MY-LIB \\
    AGORA_TEST_NCIP_ITEM_ID=barcode-0042 \\
    AGORA_TEST_NCIP_PATRON_ID=patron-0001 \\
        pytest tests/test_ncip_http_smoke.py -v

For Okapi-fronted setups also set ``OKAPI_URL``,
``RESHARE_USER``, and ``RESHARE_PASSWORD`` — ``HttpNcipClient`` picks
up the Okapi token flow the same way ``HttpReShareClient`` does
(ADR-0013).

The test is intentionally excluded from CI (requires live mod-ncip;
unverified against real tenant — see CLAUDE.md known-gaps).
"""

from __future__ import annotations

import os

import pytest

from agora.clients.ncip import HttpNcipClient, NcipError
from agora.config import Settings

pytestmark = pytest.mark.integration

_TEST_NCIP_URL = os.environ.get("AGORA_TEST_NCIP_URL")
_TEST_ITEM_ID = os.environ.get("AGORA_TEST_NCIP_ITEM_ID")
_TEST_PATRON_ID = os.environ.get("AGORA_TEST_NCIP_PATRON_ID")

requires_ncip = pytest.mark.skipif(
    _TEST_NCIP_URL is None,
    reason="AGORA_TEST_NCIP_URL not set; point at a real mod-ncip and re-run",
)

requires_ncip_roundtrip = pytest.mark.skipif(
    _TEST_NCIP_URL is None
    or _TEST_ITEM_ID is None
    or _TEST_PATRON_ID is None,
    reason=(
        "AGORA_TEST_NCIP_URL + AGORA_TEST_NCIP_ITEM_ID + "
        "AGORA_TEST_NCIP_PATRON_ID must all be set for round-trip"
    ),
)


def _settings() -> Settings:
    assert _TEST_NCIP_URL is not None  # gated by requires_ncip
    return Settings(
        NCIP_BASE_URL=_TEST_NCIP_URL,
        NCIP_AGENCY_ID=os.environ.get("NCIP_AGENCY_ID", "AGORA-DEV"),
        RESHARE_BASE_URL="",
        RESHARE_TENANT=os.environ.get("RESHARE_TENANT", "diku"),
        RESHARE_USER=os.environ.get("RESHARE_USER", ""),
        RESHARE_PASSWORD=os.environ.get("RESHARE_PASSWORD", ""),
        OKAPI_URL=os.environ.get("OKAPI_URL", ""),
    )


# ---------------------------------------------------------------------------
# Tier 1: health probe
# ---------------------------------------------------------------------------


@requires_ncip
async def test_http_ncip_health_probes_live_endpoint() -> None:
    """``HttpNcipClient.health()`` returns True against live mod-ncip.

    ``GET /admin/health`` is the standard FOLIO health endpoint (no auth
    required per mod-ncip source review). A 200 confirms: TCP reachable,
    URL/path handling correct, and mod-ncip process is up.
    """
    client = HttpNcipClient(_settings())
    try:
        ok = await client.health()
    finally:
        await client.aclose()

    assert ok, (
        f"health() returned False against {_TEST_NCIP_URL} — "
        "check that mod-ncip is running and /admin/health returns 200"
    )


# ---------------------------------------------------------------------------
# Tier 2: checkout / checkin round-trip (mutates ILS state — opt-in only)
# ---------------------------------------------------------------------------


@requires_ncip_roundtrip
async def test_http_ncip_checkout_checkin_roundtrip() -> None:
    """Send CheckOutItem then CheckInItem to live mod-ncip.

    Verifies the full wire path: XML construction, auth headers, HTTP
    transport, and response parsing.  Uses distinct idempotency keys so
    the pair can be replayed safely if the test is interrupted.

    **Side-effect**: creates then clears an ILS loan on the test item.
    Point at a dedicated test tenant or a known-safe item barcode.
    """
    assert _TEST_ITEM_ID is not None   # gated by requires_ncip_roundtrip
    assert _TEST_PATRON_ID is not None

    idem_out = f"smoke-checkout-{_TEST_ITEM_ID}"
    idem_in = f"smoke-checkin-{_TEST_ITEM_ID}"

    client = HttpNcipClient(_settings())
    try:
        # check_out — expect NcipResult(state='checked_out')
        try:
            out_result = await client.check_out(
                idempotency_key=idem_out,
                item_id=_TEST_ITEM_ID,
                patron_id=_TEST_PATRON_ID,
            )
        except NcipError as exc:
            pytest.fail(
                f"check_out raised NcipError: {exc}\n"
                "Check item barcode, patron barcode, agency ID, and tenant config."
            )

        assert out_result.state == "checked_out"
        assert out_result.item_id == _TEST_ITEM_ID
        assert out_result.patron_id == _TEST_PATRON_ID

        # check_in — expect NcipResult(state='checked_in', patron_id='')
        try:
            in_result = await client.check_in(
                idempotency_key=idem_in,
                item_id=_TEST_ITEM_ID,
            )
        except NcipError as exc:
            pytest.fail(
                f"check_out succeeded but check_in raised NcipError: {exc}\n"
                "Item may still be checked out — clear manually if needed."
            )

        assert in_result.state == "checked_in"
        assert in_result.item_id == _TEST_ITEM_ID
        assert in_result.patron_id == ""  # mod-ncip does not echo patron on check_in
    finally:
        await client.aclose()
