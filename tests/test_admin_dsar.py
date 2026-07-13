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


# CSRF guard header required on state-mutating /admin/* routes — HTML
# forms can't set custom headers, so a forged form POST riding cached
# Basic-auth credentials can't reach the irreversible scrub.
_ADMIN_HEADER = {"X-Agora-Admin": "1"}


def _admin_headers(username: str = "alice", password: str = "alice-pw") -> dict[str, str]:
    return {**_basic(username, password), **_ADMIN_HEADER}


async def _seed(
    sm: async_sessionmaker[AsyncSession],
    *,
    patron_id: str,
    state: LifecycleState,
    updated_at: datetime | None = None,
    library_symbol: str = "A",
) -> UUID:
    saga_id = uuid4()
    async with sm() as session, session.begin():
        saga = Saga(
            id=saga_id,
            request_id=uuid4(),
            current_state=state.value,
            request_payload={
                "request_type": "loan",
                "patron": {"library_symbol": library_symbol, "patron_id": patron_id},
                "requesting_library": {
                    "symbol": library_symbol,
                    "name": f"Library {library_symbol}",
                },
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
    monkeypatch: Any,
    *,
    role: str,
    salt: str = "0" * 32 + "abcdef" * 5 + "ab",
    library_symbol: str | None = None,
) -> None:
    monkeypatch.setenv("AGORA_CONSOLE_USERNAME", "alice")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "alice-pw")
    monkeypatch.setenv("AGORA_CONSOLE_ROLES", f"alice:{role}")
    monkeypatch.setenv("AGORA_PII_SCRUB_SALT", salt)
    if library_symbol is not None:
        monkeypatch.setenv("AGORA_CONSOLE_LIBRARY_SYMBOL", library_symbol)
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
async def scoped_admin_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """ADMIN principal tenant-scoped to library 'A'."""
    _configure(monkeypatch, role="admin", library_symbol="A")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        sm = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, sm
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
        headers=_admin_headers(),
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
    # Header present so the 403 is the ROLE rejection, not the CSRF guard.
    r = await approver_client.post(
        "/admin/patrons/patron-001/forget",
        headers=_admin_headers(),
    )
    assert r.status_code == 403
    assert "minimum 'admin'" in r.text


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
        headers=_admin_headers(),
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
        headers=_admin_headers(),
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


# ---- CSRF header guard on the mutating admin route ------------------------


async def test_dsar_forget_without_admin_header_is_rejected(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """No ``X-Agora-Admin: 1`` header → 403; nothing is scrubbed.

    A cross-site HTML form POST can carry the browser's cached
    Basic-auth credentials but cannot set custom headers — the guard
    closes the CSRF path to the irreversible scrub.
    """
    client, sm = admin_client
    saga_id = await _seed(
        sm,
        patron_id="patron-020",
        state=LifecycleState.RETURNED,
        updated_at=datetime.now(UTC) - timedelta(days=200),
    )
    r = await client.post(
        "/admin/patrons/patron-020/forget",
        headers=_basic("alice", "alice-pw"),  # authed, but no header
    )
    assert r.status_code == 403
    assert "X-Agora-Admin" in r.text

    # Nothing was scrubbed.
    async with sm() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert saga.request_payload["patron"]["patron_id"] == "patron-020"


async def test_dsar_forget_with_admin_header_works(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, sm = admin_client
    saga_id = await _seed(
        sm,
        patron_id="patron-021",
        state=LifecycleState.RETURNED,
        updated_at=datetime.now(UTC) - timedelta(days=200),
    )
    r = await client.post(
        "/admin/patrons/patron-021/forget",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert str(saga_id) in r.json()["scrubbed"]


async def test_dsar_list_does_not_require_admin_header(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """The read-only list route is not state-mutating — no header needed."""
    client, _ = admin_client
    r = await client.get(
        "/admin/patrons/patron-022/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 200


# ---- Tenant scoping (reviewer HIGH — cross-library DSAR IDOR) --------------


async def test_dsar_list_scoped_admin_sees_only_own_library(
    scoped_admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Library-A-scoped admin must not enumerate library B's patrons."""
    client, sm = scoped_admin_client
    a_id = await _seed(
        sm, patron_id="patron-030", state=LifecycleState.RETURNED,
        library_symbol="A",
    )
    await _seed(
        sm, patron_id="patron-030", state=LifecycleState.RETURNED,
        library_symbol="B",
    )
    r = await client.get(
        "/admin/patrons/patron-030/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["sagas"][0]["saga_id"] == str(a_id)


async def test_dsar_list_scoped_admin_cross_library_returns_empty(
    scoped_admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, sm = scoped_admin_client
    await _seed(
        sm, patron_id="patron-031", state=LifecycleState.RETURNED,
        library_symbol="B",
    )
    r = await client.get(
        "/admin/patrons/patron-031/sagas",
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


async def test_dsar_forget_scoped_admin_cannot_scrub_other_library(
    scoped_admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Cross-library forget scrubs zero rows; B's payload stays intact."""
    client, sm = scoped_admin_client
    long_ago = datetime.now(UTC) - timedelta(days=200)
    a_id = await _seed(
        sm, patron_id="patron-032", state=LifecycleState.RETURNED,
        updated_at=long_ago, library_symbol="A",
    )
    b_id = await _seed(
        sm, patron_id="patron-032", state=LifecycleState.RETURNED,
        updated_at=long_ago, library_symbol="B",
    )
    r = await client.post(
        "/admin/patrons/patron-032/forget",
        headers=_admin_headers(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scrubbed"] == [str(a_id)]
    assert str(b_id) not in body["scrubbed"]

    async with sm() as session:
        b_saga = await session.get(Saga, b_id)
        assert b_saga is not None
        assert b_saga.request_payload["patron"]["patron_id"] == "patron-032"


# ---- patron_id path-param validation ---------------------------------------


async def test_dsar_list_rejects_malformed_patron_id(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """Path param carries the portal shape bound — control chars → 422."""
    client, _ = admin_client
    r = await client.get(
        "/admin/patrons/bad%20id%21/sagas",  # "bad id!" — space + bang
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 422


async def test_dsar_forget_rejects_overlong_patron_id(
    admin_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = admin_client
    r = await client.post(
        f"/admin/patrons/{'x' * 65}/forget",
        headers=_admin_headers(),
    )
    assert r.status_code == 422
