"""HTTP-layer tests for the FastAPI staff console.

Exercises the full saga lifecycle through real ASGI calls so the
``/approve`` and ``/compensate`` endpoints (which wire ``Coordinator``
into the request flow) are covered end-to-end against an in-memory
SQLite database. The ``engine`` fixture from ``conftest.py`` overrides
the module-level engine before the API picks it up via
``get_sessionmaker()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agora.api.app import create_app

# Tests inherit ``engine`` (auto-uses :memory: SQLite) so the API's
# ``get_sessionmaker()`` resolves to that DB.


@pytest_asyncio.fixture
async def app(engine) -> FastAPI:
    """Build a fresh FastAPI app per test (private step registry)."""
    return create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _request_payload() -> dict[str, Any]:
    """Minimal valid IllRequest body that satisfies pydantic."""
    return {
        "request_type": "loan",
        "patron": {"library_symbol": "A", "patron_id": "p1"},
        "requesting_library": {"symbol": "A", "name": "Library A"},
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


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


async def test_submit_creates_saga_in_submitted_state(client: AsyncClient) -> None:
    r = await client.post("/requests", json=_request_payload())
    assert r.status_code == 201, r.text
    saga_id = r.json()["saga_id"]

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "submitted"
    # First event is the submit forward.
    assert len(detail["events"]) == 1
    assert detail["events"][0]["step"] == "submit"
    assert detail["events"][0]["kind"] == "forward"
    assert detail["events"][0]["outcome"] == "committed"


async def test_full_lifecycle_via_approve_endpoints(client: AsyncClient) -> None:
    """Submitted -> Routed -> Approved -> Shipped -> Returned via API."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    # ROUTE — caller must supply chosen_supplier (no prior event has it).
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "demo route",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "routed"

    # APPROVE — chosen_supplier derived from prior ROUTE forward.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "demo approve"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "approved"
    assert r.json()["payload"]["reshare_id"]

    # SHIP — reshare_id derived from APPROVE forward.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "ship", "actor": "staff:test", "rationale": "demo ship"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "shipped"

    # RETURN
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "return", "actor": "staff:test", "rationale": "demo return"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "returned"

    # Final saga state matches the last forward.
    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "returned"


async def test_approve_route_without_chosen_supplier_returns_400(
    client: AsyncClient,
) -> None:
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "route", "actor": "staff:test", "rationale": "missing extras"},
    )
    assert r.status_code == 400
    assert "chosen_supplier" in r.json()["detail"]


async def test_approve_rejects_unapprovable_step(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "submit", "actor": "staff:test", "rationale": "nope"},
    )
    assert r.status_code == 400
    assert "not approvable" in r.json()["detail"]


async def test_approve_unknown_saga_returns_404(client: AsyncClient) -> None:
    fake_id = "00000000-0000-0000-0000-000000000000"
    r = await client.post(
        f"/sagas/{fake_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ghost",
            "extras": {"chosen_supplier": "X"},
        },
    )
    assert r.status_code == 404


async def test_compensate_after_approve_cancels_at_supplier(
    client: AsyncClient,
) -> None:
    """Drive the saga to APPROVED, then compensate to CANCELLED."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    # ROUTE + APPROVE forwards.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    assert r.status_code == 200
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "ok"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "approved"

    # Compensate APPROVE: should cancel at supplier and move to CANCELLED.
    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "approve", "actor": "staff:test", "rationale": "patron withdrew"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "compensator"
    assert r.json()["state_after"] == "cancelled"

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "cancelled"


async def test_compensate_without_committed_forward_returns_409(
    client: AsyncClient,
) -> None:
    """Compensating a step that never ran is a 409, not a 500."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    # No ROUTE forward yet -- compensator has nothing to undo.
    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "route", "actor": "staff:test", "rationale": "nothing to undo"},
    )
    assert r.status_code == 409
    assert "no committed forward" in r.json()["detail"]


async def test_compensate_unknown_step_returns_400(client: AsyncClient) -> None:
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "not-a-real-step", "actor": "staff:test", "rationale": "nope"},
    )
    assert r.status_code == 400


async def test_outbox_worker_starts_and_stops_with_lifespan(engine) -> None:
    """``create_app`` lifespan spawns the outbox worker task and cancels it."""
    app = create_app()
    # Before lifespan runs, attribute exists but no task.
    assert app.state.outbox_worker_task is None

    async with app.router.lifespan_context(app):
        task = app.state.outbox_worker_task
        assert task is not None, "lifespan must create the worker task"
        assert not task.done(), "worker task must be running inside lifespan"
        # Yield briefly so the worker reaches its first await.
        await asyncio.sleep(0)
        assert app.state.outbox_worker is not None

    # After lifespan exits, task is cancelled.
    task_after = app.state.outbox_worker_task
    assert task_after is not None
    assert task_after.cancelled() or task_after.done()


async def test_outbox_worker_disabled_via_settings(engine, monkeypatch) -> None:
    """``AGORA_OUTBOX_WORKER_ENABLED=0`` skips spawning the worker."""
    from agora.config import get_settings

    monkeypatch.setenv("AGORA_OUTBOX_WORKER_ENABLED", "0")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            assert app.state.outbox_worker is None
            assert app.state.outbox_worker_task is None
    finally:
        get_settings.cache_clear()


async def test_reject_records_failed_gate(client: AsyncClient) -> None:
    """Sanity check that the existing /reject endpoint still records a gate."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/reject",
        json={"step": "route", "actor": "staff:test", "rationale": "policy violation"},
    )
    assert r.status_code == 204

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    gate_events = [
        e for e in detail["events"] if e["kind"] == "gate" and e["step"] == "route"
    ]
    assert any(e["outcome"] == "failed" for e in gate_events)
