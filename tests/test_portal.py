"""Patron portal HTML smoke tests.

Covers:
- GET /portal              landing page renders
- GET /portal/requests     empty and populated list; filters by patron_id
- GET /portal/requests/{id} detail view; patron_id guard (404 on mismatch)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import create_app

_PAYLOAD_A: dict[str, Any] = {
    "request_type": "loan",
    "patron": {"library_symbol": "LIB-A", "patron_id": "portal-p1"},
    "requesting_library": {"symbol": "LIB-A", "name": "Library A"},
    "item": {"title": "Dune", "author": "Herbert", "isbn": "9780441013593"},
    "citation": {
        "raw": "Herbert, F. (1965). Dune.",
        "parsed_from": "freetext",
        "parsed_at": datetime.now(UTC).isoformat(),
    },
}

_PAYLOAD_B: dict[str, Any] = {
    "request_type": "loan",
    "patron": {"library_symbol": "LIB-A", "patron_id": "portal-p2"},
    "requesting_library": {"symbol": "LIB-A", "name": "Library A"},
    "item": {"title": "Foundation", "author": "Asimov", "isbn": "9780553293357"},
    "citation": {
        "raw": "Asimov, I. (1951). Foundation.",
        "parsed_from": "freetext",
        "parsed_at": datetime.now(UTC).isoformat(),
    },
}


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> FastAPI:
    return create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------ /portal


async def test_portal_home_renders(client: AsyncClient) -> None:
    r = await client.get("/portal")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Patron portal" in r.text
    assert "patron_id" in r.text  # form field name


async def test_portal_home_has_lookup_form(client: AsyncClient) -> None:
    r = await client.get("/portal")
    assert "/portal/requests" in r.text
    assert 'method="get"' in r.text.lower() or "method=get" in r.text.lower()


# ------------------------------------------------------------------ /portal/requests


async def test_portal_requests_empty_patron(client: AsyncClient) -> None:
    r = await client.get("/portal/requests?patron_id=nobody")
    assert r.status_code == 200
    assert "No requests found" in r.text
    assert "nobody" in r.text


async def test_portal_requests_missing_patron_id_returns_422(client: AsyncClient) -> None:
    r = await client.get("/portal/requests")
    assert r.status_code == 422


async def test_portal_requests_shows_own_sagas(client: AsyncClient) -> None:
    await client.post("/requests", json=_PAYLOAD_A)
    await client.post("/requests", json=_PAYLOAD_A)

    r = await client.get("/portal/requests?patron_id=portal-p1")
    assert r.status_code == 200
    assert "Dune" in r.text
    assert r.text.count("Dune") >= 2  # two requests


async def test_portal_requests_filters_by_patron(client: AsyncClient) -> None:
    await client.post("/requests", json=_PAYLOAD_A)
    await client.post("/requests", json=_PAYLOAD_B)

    r = await client.get("/portal/requests?patron_id=portal-p1")
    assert r.status_code == 200
    assert "Dune" in r.text
    assert "Foundation" not in r.text


async def test_portal_requests_shows_state(client: AsyncClient) -> None:
    await client.post("/requests", json=_PAYLOAD_A)
    r = await client.get("/portal/requests?patron_id=portal-p1")
    assert "submitted" in r.text


# ------------------------------------------------------------------ /portal/requests/{id}


async def test_portal_detail_renders(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=portal-p1")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Dune" in r.text
    assert "Herbert" in r.text
    assert "submitted" in r.text


async def test_portal_detail_shows_event_history(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=portal-p1")
    assert r.status_code == 200
    assert "Request submitted" in r.text


async def test_portal_detail_wrong_patron_returns_404(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=wrong-patron")
    assert r.status_code == 404


async def test_portal_detail_missing_patron_id_returns_422(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}")
    assert r.status_code == 422


async def test_portal_detail_unknown_saga_returns_404(client: AsyncClient) -> None:
    from uuid import uuid4

    r = await client.get(f"/portal/requests/{uuid4()}?patron_id=portal-p1")
    assert r.status_code == 404


async def test_portal_detail_shows_isbn(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=portal-p1")
    assert "9780441013593" in r.text


async def test_portal_detail_no_due_date_before_ship(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_PAYLOAD_A)).json()["saga_id"]
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=portal-p1")
    assert r.status_code == 200
    assert "Due date" not in r.text  # only rendered when due_date is non-empty
