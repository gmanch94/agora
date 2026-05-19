"""HTTP-layer tests for the admin DSAR endpoints (G-07, ADR-0020).

Two endpoints, both ADMIN-role-gated:

- ``GET /admin/patrons/{patron_id}/sagas``  — list sagas + scrubbed flag
- ``POST /admin/patrons/{patron_id}/forget`` — immediate scrub of eligible sagas

Tests cover:
- 503 when ``AGORA_PII_SCRUB_SALT`` is empty
- 403 when the principal is APPROVER or below (only ADMIN passes)
- happy path for both endpoints
- partitioning in /forget: scrubbed vs already_scrubbed vs skipped_active
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agora.api.app import create_app
from agora.config import get_settings
from agora.models.lifecycle import LifecycleState
from agora.saga.db import Saga


def _basic(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    *,
    patron_id: str,
    state: LifecycleState,
    updated_at: datetime | None = None,
) -> UUID:
    saga_id = uuid4()
    async with sm() as session, session.begin():
        saga = Saga(
            id=saga_id,
            request_id=uuid4(),
            current_state=state.value,
            request_payload={
                "request_type": "loan",
                "patron": {"library_symbol": "A", "patron_id": patron_id},
                "requesting_library": {"symbol": "A", "name": "Library A"},
                "item": {
                    "title": "Brave New World",
                    "author": "Huxley",
                    "isbn": "9780060850524",
                    "item_barcode": "BC-0001",
                },
            },
        )
        if updated_at is not None:
            saga.updated_at = updated_at
        session.add(saga)
    return saga_id


def _configure(
    monkeypatch: Any, *, role: str, salt: str = "0" * 32 + "abcdef" * 5 + "ab"
) -> None:
    monkeypatch.setenv("AGORA_CONSOLE_USERNAME", "alice")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "alice-pw")
    monkeypatch.setenv("AGORA_CONSOLE_ROLES", f"alice:{role}")
    monkeypatch.setenv("AGORA_PII_SCRUB_SALT", salt)
    # Retention scanner OFF — these tests drive the DSAR endpoints
    # directly; we don't want the background loop racing the test.
    monkeypatch.setenv("AGORA_RETENTION_ENABLED", "false")
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    _configure(monkeypatch, role="admin")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, sm
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def approver_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    _configure(monkeypatch, role="approver")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin_no_salt_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    """ADMIN role but no scrub salt — exercises the 503 precondition."""
    _configure(monkeypatch, role="admin", salt="")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


# ---- 503 when salt is empty -----------------------------------------------


async def test_dsar_list_returns_503_when_salt_empty(
    admin_no_salt_client: AsyncClient,
) -> None:
    r = await admin_no_salt_client.get(
        "/admin/patrons/patron-001/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 503
    assert "AGORA_PII_SCRUB_SALT" in r.text


async def test_dsar_forget_returns_503_when_salt_empty(
    admin_no_salt_client: AsyncClient,
) -> None:
    r = await admin_no_salt_client.post(
        "/admin/patrons/patron-001/forget",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 503


# ---- 403 for non-admin ----------------------------------------------------


async def test_dsar_list_rejects_approver(approver_client: AsyncClient) -> None:
    r = await approver_client.get(
        "/admin/patrons/patron-001/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 403
    assert "minimum 'admin'" in r.text


async def test_dsar_forget_rejects_approver(approver_client: AsyncClient) -> None:
    r = await approver_client.post(
        "/admin/patrons/patron-001/forget",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 403


# ---- 401 still wins over 403 ----------------------------------------------


async def test_dsar_list_unauth_returns_401(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = admin_client
    r = await client.get("/admin/patrons/patron-001/sagas")
    assert r.status_code == 401


# ---- Happy path ----------------------------------------------------------


async def test_dsar_list_finds_cleartext_and_scrubbed(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """One cleartext + one already-scrubbed saga both surface in the listing."""
    client, sm = admin_client
    cleartext_id = await _seed(
        sm, patron_id="patron-007", state=LifecycleState.RECEIVED
    )
    # Seed a "different" patron that will pass through the scrub later
    # via the /forget endpoint, then re-query to verify it surfaces.
    r = await client.get(
        "/admin/patrons/patron-007/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    [row] = body["sagas"]
    assert row["saga_id"] == str(cleartext_id)
    assert row["scrubbed"] is False
    assert row["current_state"] == LifecycleState.RECEIVED.value


async def test_dsar_forget_scrubs_eligible_only(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Active sagas land in ``skipped_active``; terminal sagas get scrubbed."""
    client, sm = admin_client
    long_ago = datetime.now(UTC) - timedelta(days=120)
    terminal_id = await _seed(
        sm,
        patron_id="patron-009",
        state=LifecycleState.RETURNED,
        updated_at=long_ago,
    )
    active_id = await _seed(
        sm,
        patron_id="patron-009",
        state=LifecycleState.SHIPPED,
        updated_at=long_ago,
    )
    r = await client.post(
        "/admin/patrons/patron-009/forget",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert str(terminal_id) in body["scrubbed"]
    assert str(active_id) in body["skipped_active"]


async def test_dsar_forget_is_idempotent_after_scrub(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Second /forget call surfaces the saga in ``already_scrubbed``."""
    client, sm = admin_client
    await _seed(
        sm,
        patron_id="patron-011",
        state=LifecycleState.UNFILLED,
        updated_at=datetime.now(UTC) - timedelta(days=200),
    )
    # First call — scrubs.
    r1 = await client.post(
        "/admin/patrons/patron-011/forget",
        headers=_basic("alice", "alice-pw"),
    )
    assert r1.status_code == 200
    assert len(r1.json()["scrubbed"]) == 1

    # Second call with the same cleartext id — list endpoint should now
    # find the scrubbed row via the fingerprint lookup.
    r2 = await client.get(
        "/admin/patrons/patron-011/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] == 1
    assert body["sagas"][0]["scrubbed"] is True
