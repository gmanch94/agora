"""HTTP-layer tests for the auth + tenant-scoping + portal-HMAC stack.

Exercises the audit-2026-05-09 batch-4 changes:

- #1 / #21: every JSON endpoint requires Basic auth when
  ``AGORA_CONSOLE_PASSWORD`` is set; the ``actor`` recorded on ledger
  events is the authenticated principal, not whatever the request
  body claimed.
- #3 stopgap: ``AGORA_CONSOLE_LIBRARY_SYMBOL`` binds the principal to
  a single library, and every saga endpoint refuses cross-library
  access (403). ``GET /sagas`` SQL-filters; ``POST /requests`` rejects
  out-of-scope ``requesting_library``.
- #2: when ``AGORA_PORTAL_SIGNING_KEY`` is set, the portal endpoints
  require an HMAC ``token`` query param. Saga detail also requires the
  saga's stored patron_id to match the query param.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import create_app, mint_portal_token
from agora.config import get_settings


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


def _basic(username: str, password: str) -> dict[str, str]:
    raw = f"{username}:{password}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


@pytest_asyncio.fixture
async def auth_app(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[FastAPI]:
    """App with Basic auth + library scoping enabled (library 'A')."""
    monkeypatch.setenv("AGORA_CONSOLE_USERNAME", "alice")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "alice-pw")
    monkeypatch.setenv("AGORA_CONSOLE_LIBRARY_SYMBOL", "A")
    get_settings.cache_clear()
    try:
        yield create_app()
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def portal_app(
    engine: AsyncEngine, monkeypatch: Any
) -> AsyncIterator[FastAPI]:
    """App with portal HMAC enabled (32-byte key)."""
    monkeypatch.setenv("AGORA_PORTAL_SIGNING_KEY", "k" * 64)
    get_settings.cache_clear()
    try:
        yield create_app()
    finally:
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def portal_client(portal_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=portal_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------
# Audit #1: JSON endpoints require Basic auth when password is set
# ---------------------------------------------------------------------


async def test_json_endpoint_unauthenticated_returns_401(
    auth_client: AsyncClient,
) -> None:
    """No Authorization header → 401 on every JSON endpoint."""
    r = await auth_client.post("/requests", json=_request_payload())
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers

    r = await auth_client.get("/sagas")
    assert r.status_code == 401


async def test_json_endpoint_wrong_password_returns_401(
    auth_client: AsyncClient,
) -> None:
    """Wrong creds → 401 (not 403) so browsers re-prompt."""
    headers = _basic("alice", "wrong-pw")
    r = await auth_client.post(
        "/requests", json=_request_payload(), headers=headers
    )
    assert r.status_code == 401


async def test_json_endpoint_correct_creds_returns_201(
    auth_client: AsyncClient,
) -> None:
    """Valid creds → request lands and saga is created."""
    headers = _basic("alice", "alice-pw")
    r = await auth_client.post(
        "/requests", json=_request_payload(), headers=headers
    )
    assert r.status_code == 201, r.text


async def test_health_does_not_require_auth(
    auth_client: AsyncClient,
) -> None:
    """``/health`` stays public so liveness probes work without creds."""
    r = await auth_client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------
# Audit #21: actor on ledger events comes from the principal
# ---------------------------------------------------------------------


async def test_actor_on_ledger_event_is_principal_not_body(
    auth_client: AsyncClient,
) -> None:
    """The ``actor`` recorded on the ledger ignores request-body claims.

    Pre-fix the caller could write ``actor=staff:victim`` and forge
    audit-trail attribution. Post-fix the principal username + library
    is the actor on every event, regardless of body.
    """
    headers = _basic("alice", "alice-pw")
    r = await auth_client.post(
        "/requests", json=_request_payload(library_symbol="A"), headers=headers
    )
    saga_id = r.json()["saga_id"]

    r = await auth_client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:victim@OTHERLIB",  # forged attempt
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text

    detail = (await auth_client.get(f"/sagas/{saga_id}", headers=headers)).json()
    # Find the ROUTE forward — its actor must reflect the auth principal,
    # not the spoofed body actor.
    route_events = [
        e for e in detail["events"] if e["step"] == "route" and e["kind"] == "forward"
    ]
    assert len(route_events) == 1
    assert route_events[0]["actor"] == "staff:alice@A"
    # Forged victim never lands.
    assert "victim" not in route_events[0]["actor"]


# ---------------------------------------------------------------------
# Audit #3 stopgap: tenant scoping
# ---------------------------------------------------------------------


async def test_post_requests_refuses_out_of_scope_library(
    auth_client: AsyncClient,
) -> None:
    """A scoped principal cannot submit requests for a different library."""
    headers = _basic("alice", "alice-pw")
    r = await auth_client.post(
        "/requests",
        json=_request_payload(library_symbol="B"),  # not principal's lib
        headers=headers,
    )
    assert r.status_code == 403
    assert "principal scope" in r.json()["detail"]


async def test_get_saga_refuses_cross_library_access(
    auth_client: AsyncClient, auth_app: FastAPI
) -> None:
    """GET /sagas/{id} on another library's saga returns 403, not 200.

    Seed the DB directly with a library-B saga (bypassing the API which
    would refuse it) so we have a foreign saga to probe against.
    """
    headers = _basic("alice", "alice-pw")

    # Seed a foreign-library saga directly via the DB.
    from uuid import uuid4

    from agora.models.lifecycle import LifecycleState
    from agora.models.request import IllRequest
    from agora.saga.db import get_sessionmaker
    from agora.saga.ledger import SagaLedger

    foreign_id = uuid4()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        req = IllRequest.model_validate(_request_payload(library_symbol="B"))
        await ledger.create_saga(
            saga_id=foreign_id,
            request_id=req.request_id,
            request_payload=req.model_dump(mode="json"),
            initial_state=LifecycleState.SUBMITTED,
        )

    r = await auth_client.get(f"/sagas/{foreign_id}", headers=headers)
    assert r.status_code == 403
    assert "principal is" in r.json()["detail"]


async def test_list_sagas_filters_to_principal_library(
    auth_client: AsyncClient,
) -> None:
    """GET /sagas only returns sagas whose requesting_library matches.

    Seed two sagas (library A + library B); list returns only A's.
    """
    headers = _basic("alice", "alice-pw")

    # Submit one saga as library A via the API.
    r1 = await auth_client.post(
        "/requests", json=_request_payload(library_symbol="A"), headers=headers
    )
    a_id = r1.json()["saga_id"]

    # Seed a foreign B saga directly.
    from uuid import uuid4

    from agora.models.lifecycle import LifecycleState
    from agora.models.request import IllRequest
    from agora.saga.db import get_sessionmaker
    from agora.saga.ledger import SagaLedger

    sm = get_sessionmaker()
    foreign_id = uuid4()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        req = IllRequest.model_validate(_request_payload(library_symbol="B"))
        await ledger.create_saga(
            saga_id=foreign_id,
            request_id=req.request_id,
            request_payload=req.model_dump(mode="json"),
            initial_state=LifecycleState.SUBMITTED,
        )

    listing = (await auth_client.get("/sagas", headers=headers)).json()
    listed_ids = {row["saga_id"] for row in listing}
    assert a_id in listed_ids
    assert str(foreign_id) not in listed_ids


# ---------------------------------------------------------------------
# Audit #2: portal HMAC tokens
# ---------------------------------------------------------------------


async def test_portal_requests_without_token_returns_404(
    portal_client: AsyncClient,
) -> None:
    """When portal signing is enabled, missing token → 404 (no info leak)."""
    r = await portal_client.get(
        "/portal/requests", params={"patron_id": "alice"}
    )
    assert r.status_code == 404


async def test_portal_requests_wrong_token_returns_404(
    portal_client: AsyncClient,
) -> None:
    """Bad HMAC → 404 (same as missing — no oracle for token validity)."""
    r = await portal_client.get(
        "/portal/requests",
        params={"patron_id": "alice", "token": "0" * 64},
    )
    assert r.status_code == 404


async def test_portal_requests_correct_token_returns_200(
    portal_client: AsyncClient,
) -> None:
    """A token correctly signing patron_id unlocks the listing."""
    key = "k" * 64
    token = mint_portal_token(key, "alice")
    r = await portal_client.get(
        "/portal/requests",
        params={"patron_id": "alice", "token": token},
    )
    assert r.status_code == 200


async def test_portal_saga_detail_token_must_bind_saga_and_patron(
    portal_client: AsyncClient,
) -> None:
    """Detail token must sign (saga_id, patron_id), not just patron_id.

    Audit #2: a token issued for one saga can't be reused on another.
    """
    from uuid import uuid4

    from agora.models.lifecycle import LifecycleState
    from agora.models.request import IllRequest
    from agora.saga.db import get_sessionmaker
    from agora.saga.ledger import SagaLedger

    saga_a = uuid4()
    saga_b = uuid4()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        for sid in (saga_a, saga_b):
            req = IllRequest.model_validate(_request_payload(patron_id="alice"))
            await ledger.create_saga(
                saga_id=sid,
                request_id=req.request_id,
                request_payload=req.model_dump(mode="json"),
                initial_state=LifecycleState.SUBMITTED,
            )

    key = "k" * 64
    token_a = mint_portal_token(key, str(saga_a), "alice")

    # Token for A unlocks A.
    r = await portal_client.get(
        f"/portal/requests/{saga_a}",
        params={"patron_id": "alice", "token": token_a},
    )
    assert r.status_code == 200, r.text

    # Token for A does NOT unlock B.
    r = await portal_client.get(
        f"/portal/requests/{saga_b}",
        params={"patron_id": "alice", "token": token_a},
    )
    assert r.status_code == 404


async def test_portal_saga_detail_token_does_not_unlock_other_patron(
    portal_client: AsyncClient,
) -> None:
    """A token signing (saga, alice) must not work with patron_id=bob.

    Audit #2: stored patron_id check is the second layer — even with a
    valid HMAC over (saga_id, patron_id_query), the saga's stored
    patron_id must equal the query param.
    """
    from uuid import uuid4

    from agora.models.lifecycle import LifecycleState
    from agora.models.request import IllRequest
    from agora.saga.db import get_sessionmaker
    from agora.saga.ledger import SagaLedger

    saga_id = uuid4()
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        req = IllRequest.model_validate(_request_payload(patron_id="alice"))
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=req.request_id,
            request_payload=req.model_dump(mode="json"),
            initial_state=LifecycleState.SUBMITTED,
        )

    key = "k" * 64
    # Token signs (saga_id, "bob") — HMAC verifies, but saga's stored
    # patron is "alice", so the second-layer check rejects.
    token = mint_portal_token(key, str(saga_id), "bob")
    r = await portal_client.get(
        f"/portal/requests/{saga_id}",
        params={"patron_id": "bob", "token": token},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Audit #11: OkapiAuth proactive expiry refresh
# ---------------------------------------------------------------------


async def test_okapi_auth_clear_token_drops_cache() -> None:
    """``clear_token`` empties both token + expiry."""
    from datetime import UTC, datetime, timedelta

    from agora.clients.okapi_auth import OkapiAuth

    auth = OkapiAuth(
        login_url="https://okapi.example/authn/login-with-expiry",
        tenant="t",
        username="u",
        password="p",
    )
    auth._token = "TOKEN-XYZ"
    auth._expires_at = datetime.now(UTC) + timedelta(hours=1)

    auth.clear_token()
    assert auth.cached_token is None
    assert auth.expires_at is None


def test_okapi_auth_extract_expiry_parses_iso8601_z_suffix() -> None:
    """ISO 8601 with trailing 'Z' is normalised to +00:00 and parsed."""
    import httpx

    from agora.clients.okapi_auth import OkapiAuth

    response = httpx.Response(
        201,
        json={"accessTokenExpiration": "2026-12-25T15:30:00Z"},
    )
    parsed = OkapiAuth._extract_expiry(response)
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.tzinfo is not None


def test_okapi_auth_extract_expiry_missing_field_returns_none() -> None:
    """Legacy /authn/login responses with no expiry field gracefully None."""
    import httpx

    from agora.clients.okapi_auth import OkapiAuth

    response = httpx.Response(201, json={})
    assert OkapiAuth._extract_expiry(response) is None
