"""RBAC matrix tests (G-02 from docs/productionization.md).

Exercises ``AGORA_CONSOLE_ROLES`` + ``_require_role`` + the role-gated
endpoints. The HTTP-layer fixtures mirror ``test_api_auth.py`` so the
two test files compose: ``test_api_auth.py`` covers "is the principal
authenticated?", ``test_authz.py`` covers "given an authenticated
principal, can it perform this operation?".

Single-user note: the underlying Basic-auth check still pins to a single
``AGORA_CONSOLE_USERNAME``. Multi-user RBAC lands with OIDC (G-01).
Today the roster lets ops downgrade or upgrade the lone console user
from the default APPROVER — already covers the most common production
need (read-only viewer accounts during a pilot).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import (
    ConsolePrincipal,
    Role,
    _parse_console_roles,
    create_app,
)
from agora.config import get_settings

# ---------------------------------------------------------------------
# Role enum + parser unit tests
# ---------------------------------------------------------------------


def test_role_rank_ordering() -> None:
    """VIEWER < APPROVER < ADMIN — required for _require_role comparisons."""
    assert Role.VIEWER.rank < Role.APPROVER.rank < Role.ADMIN.rank


def test_principal_default_role_is_approver() -> None:
    """Dataclass default preserves pre-G-02 single-principal behaviour.

    Existing code that constructs a ``ConsolePrincipal`` without a
    ``role=`` kwarg (tests, fixtures, dev shortcuts) keeps the
    approver-capable identity it always had.
    """
    p = ConsolePrincipal(username="alice", library_symbol=None)
    assert p.role is Role.APPROVER


def test_parse_console_roles_empty() -> None:
    assert _parse_console_roles("") == {}
    assert _parse_console_roles("   ") == {}


def test_parse_console_roles_happy_path() -> None:
    out = _parse_console_roles("alice:admin, bob:approver,charlie:viewer")
    assert out == {
        "alice": Role.ADMIN,
        "bob": Role.APPROVER,
        "charlie": Role.VIEWER,
    }


def test_parse_console_roles_tolerates_trailing_comma_and_whitespace() -> None:
    out = _parse_console_roles(" alice:admin , , ")
    assert out == {"alice": Role.ADMIN}


def test_parse_console_roles_rejects_missing_separator() -> None:
    with pytest.raises(ValueError, match="missing ':' separator"):
        _parse_console_roles("alice-admin")


def test_parse_console_roles_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        _parse_console_roles("alice:superuser")


def test_parse_console_roles_rejects_empty_username() -> None:
    with pytest.raises(ValueError, match="empty username"):
        _parse_console_roles(":admin")


def test_parse_console_roles_rejects_duplicate_username() -> None:
    with pytest.raises(ValueError, match="duplicates username"):
        _parse_console_roles("alice:admin,alice:viewer")


# ---------------------------------------------------------------------
# HTTP-layer RBAC matrix
# ---------------------------------------------------------------------


def _basic(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


def _request_payload(library_symbol: str = "A", patron_id: str = "p1") -> dict[str, Any]:
    return {
        "request_type": "loan",
        "patron": {"library_symbol": library_symbol, "patron_id": patron_id},
        "requesting_library": {"symbol": library_symbol, "name": f"Library {library_symbol}"},
        "item": {
            "title": "Brave New World",
            "author": "Huxley",
            "isbn": "9780060850524",
        },
        "citation": {
            "raw": "ctx_ver=Z39.88-2004",
            "parsed_from": "openurl",
            "parsed_at": datetime.now(UTC).isoformat(),
        },
    }


def _configure(monkeypatch: Any, *, roles: str) -> None:
    monkeypatch.setenv("AGORA_CONSOLE_USERNAME", "alice")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "alice-pw")
    monkeypatch.setenv("AGORA_CONSOLE_LIBRARY_SYMBOL", "A")
    monkeypatch.setenv("AGORA_CONSOLE_ROLES", roles)
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def viewer_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    """Alice configured as VIEWER — read-only."""
    _configure(monkeypatch, roles="alice:viewer")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def approver_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    """Alice configured as APPROVER — can commit gates."""
    _configure(monkeypatch, roles="alice:approver")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def admin_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    """Alice configured as ADMIN — strictly stronger than approver."""
    _configure(monkeypatch, roles="alice:admin")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def default_role_client(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[AsyncClient]:
    """Empty roster — falls back to APPROVER default (back-compat path)."""
    _configure(monkeypatch, roles="")
    try:
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        get_settings.cache_clear()


# ---- VIEWER cannot mutate -------------------------------------------------


async def test_viewer_cannot_submit_request(viewer_client: AsyncClient) -> None:
    r = await viewer_client.post(
        "/requests", json=_request_payload(), headers=_basic("alice", "alice-pw")
    )
    assert r.status_code == 403
    assert "minimum 'approver'" in r.text


async def test_viewer_cannot_call_approve(viewer_client: AsyncClient) -> None:
    """Even without a real saga, role gate fires before the saga lookup.

    The 403 must come from the dependency layer — proves the role
    check is enforced at the FastAPI gateway, not buried in the handler.
    """
    r = await viewer_client.post(
        "/sagas/00000000-0000-4000-8000-00000000abcd/approve",
        json={"step": "route", "actor": "alice", "rationale": "x"},
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 403


async def test_viewer_can_list_sagas(viewer_client: AsyncClient) -> None:
    """Read-only endpoints stay reachable for VIEWER."""
    r = await viewer_client.get("/sagas", headers=_basic("alice", "alice-pw"))
    assert r.status_code == 200


# ---- APPROVER can mutate (happy path) -------------------------------------


async def test_approver_can_submit_request(approver_client: AsyncClient) -> None:
    r = await approver_client.post(
        "/requests", json=_request_payload(), headers=_basic("alice", "alice-pw")
    )
    assert r.status_code == 201


# ---- ADMIN passes the APPROVER gate (rank ordering enforced) --------------


async def test_admin_can_submit_request(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/requests", json=_request_payload(), headers=_basic("alice", "alice-pw")
    )
    assert r.status_code == 201


# ---- Default (empty roster) preserves legacy behaviour --------------------


async def test_default_role_can_submit_request(
    default_role_client: AsyncClient,
) -> None:
    """Empty AGORA_CONSOLE_ROLES → APPROVER → existing deployments unchanged."""
    r = await default_role_client.post(
        "/requests",
        json=_request_payload(),
        headers=_basic("alice", "alice-pw"),
    )
    assert r.status_code == 201


# ---- 401 still wins over 403 ----------------------------------------------


async def test_unauth_returns_401_not_403_even_when_viewer_role_set(
    viewer_client: AsyncClient,
) -> None:
    """Missing creds → 401 (auth layer fires first). 403 only for bad role."""
    r = await viewer_client.post("/requests", json=_request_payload())
    assert r.status_code == 401


# ---- Reviewer-flagged hardening ------------------------------------------


def test_parse_console_roles_lowercases_username() -> None:
    """Reviewer LOW: casing mismatch shouldn't silently downgrade to viewer.

    A roster of ``Alice:admin`` must match a Basic-auth login of ``alice``.
    """
    out = _parse_console_roles("Alice:admin,BOB:approver")
    assert out == {"alice": Role.ADMIN, "bob": Role.APPROVER}


def test_create_app_raises_on_malformed_roster(monkeypatch: Any) -> None:
    """Reviewer LOW (covered): bad roster fails fast at boot, not on first 403.

    Asserting on the boot-time exception protects against future
    refactors that move the parser call out of ``create_app()``.
    """
    monkeypatch.setenv("AGORA_CONSOLE_USERNAME", "alice")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "alice-pw")
    monkeypatch.setenv("AGORA_CONSOLE_ROLES", "alice:superuser")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="unknown role"):
            create_app()
    finally:
        get_settings.cache_clear()
