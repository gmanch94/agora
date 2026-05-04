"""HTML smoke tests for the staff console first slice (ADR-0015).

Pins the wiring: ``GET /`` returns 200 with ``content-type: text/html``,
the empty-inbox state renders the documented copy, and a populated
inbox lists the seeded saga's title.

Reuses the ``client`` fixture pattern from ``tests/test_api.py`` —
ASGITransport against a fresh ``create_app()`` per test, with the
in-memory SQLite engine fixture from ``conftest.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import create_app


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
    submit = await client.post(
        "/requests",
        json={
            "request_type": "loan",
            "patron": {"library_symbol": "LIB-A", "patron_id": "p-001"},
            "requesting_library": {"symbol": "LIB-A", "name": "Library A"},
            "item": {"title": "Brave New World", "author": "Huxley"},
            "citation": {
                "raw": "Huxley, A. (1932). Brave New World.",
                "parsed_from": "freetext",
                "parsed_at": "2026-05-04T00:00:00+00:00",
            },
        },
    )
    assert submit.status_code == 201, submit.text

    r = await client.get("/")
    assert r.status_code == 200
    assert "Brave New World" in r.text
    # Saga's current state is rendered as a pill — submitted is the
    # initial state for a fresh /requests POST.
    assert "submitted" in r.text
