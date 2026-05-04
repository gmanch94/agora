"""HTML smoke tests for the staff console UI (ADR-0015).

Covers two slices:
- Slice 1 (inbox): ``GET /`` returns 200 / text/html; empty and populated states.
- Slice 2 (detail + actions): ``GET /sagas/{id}/view`` renders the timeline and
  action forms; ``POST /ui/sagas/{id}/approve|reject|compensate`` perform the
  action and redirect (303) to the detail view.

Reuses the ``client`` fixture pattern from ``tests/test_api.py`` —
ASGITransport against a fresh ``create_app()`` per test, with the
in-memory SQLite engine fixture from ``conftest.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import create_app

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REQUEST_PAYLOAD: dict[str, Any] = {
    "request_type": "loan",
    "patron": {"library_symbol": "LIB-A", "patron_id": "p-001"},
    "requesting_library": {"symbol": "LIB-A", "name": "Library A"},
    "item": {"title": "Brave New World", "author": "Huxley"},
    "citation": {
        "raw": "Huxley, A. (1932). Brave New World.",
        "parsed_from": "freetext",
        "parsed_at": "2026-05-04T00:00:00+00:00",
    },
}


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> FastAPI:
    """Build a fresh FastAPI app per test.

    The ``engine`` fixture from ``conftest.py`` swaps in an in-memory
    SQLite database before ``create_app()`` resolves the sessionmaker.
    """
    return create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_inbox_empty_renders_html(client: AsyncClient) -> None:
    """Empty database — inbox renders the documented empty-state copy."""
    r = await client.get("/")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html"), r.headers
    body = r.text
    # Page scaffolding (base.html).
    assert "<title>Inbox" in body
    assert "Agora" in body
    # Empty-state copy (inbox.html when sagas list is empty).
    assert "No sagas yet" in body
    # Stylesheet link is present so a runtime browser pulls the theme.
    assert "/static/theme.css" in body


async def test_inbox_lists_submitted_saga(client: AsyncClient) -> None:
    """After submitting a request, the inbox lists its title."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    assert submit.status_code == 201, submit.text

    r = await client.get("/")
    assert r.status_code == 200
    assert "Brave New World" in r.text
    # Saga's current state is rendered as a pill — submitted is the
    # initial state for a fresh /requests POST.
    assert "submitted" in r.text


async def test_inbox_row_links_to_detail(client: AsyncClient) -> None:
    """Inbox table cell contains a link to the detail view."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    r = await client.get("/")
    assert r.status_code == 200
    assert f"/sagas/{saga_id}/view" in r.text


# ---------------------------------------------------------------------------
# Slice 2 — detail view
# ---------------------------------------------------------------------------


async def test_detail_view_renders_html(client: AsyncClient) -> None:
    """GET /sagas/{id}/view returns 200 text/html with timeline and breadcrumb."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    r = await client.get(f"/sagas/{saga_id}/view")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html"), r.headers
    body = r.text
    # Breadcrumb back to inbox.
    assert "← Inbox" in body or "&larr; Inbox" in body or "&#8592; Inbox" in body
    # Saga title in page heading.
    assert "Brave New World" in body
    # Event timeline table is rendered.
    assert "Event timeline" in body
    # At least the SUBMIT forward event shows up.
    assert "submit" in body


async def test_detail_view_shows_approve_form_for_submitted_saga(
    client: AsyncClient,
) -> None:
    """SUBMITTED saga — detail view shows an Approve route form."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    r = await client.get(f"/sagas/{saga_id}/view")
    body = r.text
    # Approve form targeting the route step.
    assert "/ui/sagas/" in body
    assert "approve" in body
    assert "route" in body
    # Supplier input required for ROUTE step.
    assert "chosen_supplier" in body


async def test_detail_view_404_on_missing_saga(client: AsyncClient) -> None:
    """Unknown saga UUID returns 404, not 500."""
    r = await client.get("/sagas/00000000-0000-0000-0000-000000000000/view")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Slice 2 — form action endpoints
# ---------------------------------------------------------------------------


async def test_ui_approve_routes_saga(client: AsyncClient) -> None:
    """POST /ui/sagas/{id}/approve with route step advances state to ROUTED."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    r = await client.post(
        f"/ui/sagas/{saga_id}/approve",
        data={"step": "route", "chosen_supplier": "LIB-B", "rationale": "Test approve."},
    )
    # Form endpoints redirect to detail view on success.
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/sagas/{saga_id}/view"

    # Verify the saga state advanced via the JSON API.
    detail = await client.get(f"/sagas/{saga_id}")
    assert detail.json()["saga"]["current_state"] == "routed"


async def test_ui_reject_appends_failed_gate(client: AsyncClient) -> None:
    """POST /ui/sagas/{id}/reject appends a FAILED gate and redirects."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    r = await client.post(
        f"/ui/sagas/{saga_id}/reject",
        data={"step": "route", "rationale": "Not in scope."},
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/sagas/{saga_id}/view"

    # State is unchanged — reject records a FAILED gate but doesn't advance.
    detail = await client.get(f"/sagas/{saga_id}")
    assert detail.json()["saga"]["current_state"] == "submitted"
    events = detail.json()["events"]
    failed = [e for e in events if e["outcome"] == "failed"]
    assert failed, "Expected at least one FAILED gate event after reject"


async def test_ui_compensate_cancels_routed_saga(client: AsyncClient) -> None:
    """Compensate route step on a ROUTED saga reverts to SUBMITTED."""
    submit = await client.post("/requests", json=_REQUEST_PAYLOAD)
    saga_id = submit.json()["saga_id"]

    # First route the saga via the UI approve endpoint.
    await client.post(
        f"/ui/sagas/{saga_id}/approve",
        data={"step": "route", "chosen_supplier": "LIB-B"},
    )

    r = await client.post(
        f"/ui/sagas/{saga_id}/compensate",
        data={"step": "route", "rationale": "Wrong supplier."},
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/sagas/{saga_id}/view"

    detail = await client.get(f"/sagas/{saga_id}")
    assert detail.json()["saga"]["current_state"] == "submitted"
