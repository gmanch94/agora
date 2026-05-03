"""Real-ReShare smoke test (backlog #9 / PR-B of 4).

Exercises the live ``HttpReShareClient`` ``health()`` probe against
a real mod-rs (or Okapi-fronted ReShare) endpoint so we can catch
regressions in: factory wiring, auth flow (Basic or Okapi token
per ADR-0013), URL/path handling, and httpx connection lifecycle.

**Skipped unless ``AGORA_TEST_RESHARE_URL`` is set.** Mirrors the
``AGORA_TEST_DB_URL`` pattern from ``test_alembic_postgres.py`` so
that nobody's local ``.env`` (which sets ``RESHARE_BASE_URL`` for
dev work) accidentally fires real HTTP from ``pytest``.

Scope is read-only by design — only ``health()`` is exercised. The
test must not touch ``send_request``, ``cancel_request``, or any
state-mutating action; pointing it at a shared sandbox should never
risk corrupting tenant data.

Local invocation::

    AGORA_TEST_RESHARE_URL=http://mod-rs.local:8081 \\
    RESHARE_TENANT=consortium-a \\
    RESHARE_USER=admin RESHARE_PASSWORD=admin \\
        pytest tests/test_reshare_http_smoke.py -v

For Okapi-fronted setups, additionally set ``OKAPI_URL`` to the
gateway and the same user/password are reused as Okapi creds (per
ADR-0013).
"""

from __future__ import annotations

import os

import pytest

from agora.clients.reshare import HttpReShareClient
from agora.config import Settings

pytestmark = pytest.mark.integration

_TEST_RESHARE_URL = os.environ.get("AGORA_TEST_RESHARE_URL")

requires_reshare = pytest.mark.skipif(
    _TEST_RESHARE_URL is None,
    reason="AGORA_TEST_RESHARE_URL not set; point at a real mod-rs and re-run",
)


@requires_reshare
async def test_http_reshare_health_probes_live_endpoint() -> None:
    """``HttpReShareClient.health()`` returns True against a live mod-rs.

    The probe is ``GET /rs/patronrequests?perPage=0`` (mod-rs has no
    ``/admin/health``, see module docstring). A 200/204 means: TCP
    reachable, TLS valid (if https), tenant header accepted, auth
    accepted, and the patronrequests collection endpoint exists.
    """
    assert _TEST_RESHARE_URL is not None  # for mypy; gated by requires_reshare
    settings = Settings(
        RESHARE_BASE_URL=_TEST_RESHARE_URL,
        RESHARE_TENANT=os.environ.get("RESHARE_TENANT", "consortium-a"),
        RESHARE_USER=os.environ.get("RESHARE_USER", ""),
        RESHARE_PASSWORD=os.environ.get("RESHARE_PASSWORD", ""),
        OKAPI_URL=os.environ.get("OKAPI_URL", ""),
    )
    client = HttpReShareClient(settings)
    try:
        ok = await client.health()
    finally:
        await client.aclose()

    assert ok, (
        f"health() returned False against {_TEST_RESHARE_URL} — "
        "check tenant header, auth creds, and that mod-rs is up"
    )
