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

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import _build_outbox_worker, create_app
from agora.saga.db import get_sessionmaker

# Tests inherit ``engine`` (auto-uses :memory: SQLite) so the API's
# ``get_sessionmaker()`` resolves to that DB.


async def _drive_outbox_worker(app: FastAPI) -> None:
    """Drain the outbox once with the app's reshare/ncip clients.

    The ASGI test transport skips the FastAPI lifespan, so the
    background outbox worker never spawns. Tests that depend on
    ADR-0012 projection behaviour (APPROVE forward → APPROVING,
    worker drains, OBSERVATION advances to APPROVED) call this
    helper between the APPROVE request and any subsequent SHIP /
    compensate request.

    Wires exactly the same handler + ``on_success`` map the lifespan
    builds via :func:`_build_outbox_worker`, so the test exercises
    the production projection contract.
    """
    worker = _build_outbox_worker(
        get_sessionmaker(),
        reshare=app.state.reshare,
        ncip=app.state.ncip,
        max_attempts=5,
    )
    await worker.drain_until_empty()


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> FastAPI:
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


async def test_full_lifecycle_via_approve_endpoints(
    app: FastAPI, client: AsyncClient
) -> None:
    """Submitted → Routed → Approving → Approved → Shipped → Received → Returned.

    Per ADR-0012 the APPROVE endpoint returns immediately with state
    ``approving``; the supplier round-trip happens off the request
    path via the outbox worker, which writes an OBSERVATION to
    advance the saga to ``approved``. This test drives the worker
    explicitly between APPROVE and SHIP so the saga reaches the
    state SHIP requires. Borrower-side NCIP ``check_out`` is anchored
    on the RECEIVE forward (re-anchored from SHIP) so the patron's
    ILS record reflects the loan from physical-receipt.
    """
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

    # APPROVE — forward returns immediately with state APPROVING. The
    # forward payload no longer carries reshare_id (ADR-0012); the
    # worker projects it onto an OBSERVATION below.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "demo approve"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "approving"
    assert "reshare_id" not in r.json()["payload"]

    # Drive the worker so the supplier ack lands and the projection
    # advances the saga to APPROVED.
    await _drive_outbox_worker(app)

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "approved"
    approve_obs = next(
        e for e in detail["events"]
        if e["kind"] == "observation" and e["step"] == "approve"
    )
    assert approve_obs["state_after"] == "approved"
    assert approve_obs["payload"]["reshare_id"]

    # SHIP — reshare_id derived from APPROVE OBSERVATION via _derive_extras.
    # Post NCIP-checkout-re-anchor SHIP forward emits a single ReShare
    # ``confirm_shipment`` intent — borrower-side ``check_out`` moved
    # to RECEIVE forward.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "ship", "actor": "staff:test", "rationale": "demo ship"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "shipped"
    await _drive_outbox_worker(app)

    # RECEIVE — borrower confirms physical receipt. RECEIVE forward
    # emits a single ``target='ncip'`` ``check_out`` intent (re-anchored
    # from SHIP); the patron's ILS record opens at this point. Drive
    # the worker so the call lands on the mock NCIP client.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "receive", "actor": "staff:test", "rationale": "demo receive"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "received"
    await _drive_outbox_worker(app)

    # RETURN — fans out to two outbox intents: reshare confirm_return
    # + ncip check_in.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "return", "actor": "staff:test", "rationale": "demo return"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "returned"
    await _drive_outbox_worker(app)

    # Final saga state matches the last forward.
    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "returned"

    # Borrower-side NCIP: the mock client's private dedup map (_idem)
    # is populated when the worker dispatches each call. Two entries
    # (one check_out for SHIP, one check_in for RETURN) with the same
    # item_id == reshare_id proves the saga's NCIP fan-out landed.
    # Accessing the private attribute mirrors the same pattern used in
    # tests/test_coordinator.py for ReShareClient verification.
    reshare_id = approve_obs["payload"]["reshare_id"]
    ncip_state = app.state.ncip._idem
    states = sorted(r.state for r in ncip_state.values())
    assert states == ["checked_in", "checked_out"], (
        f"expected NCIP fan-out to record check_out + check_in, got {states}"
    )
    assert all(r.item_id == reshare_id for r in ncip_state.values())


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
    app: FastAPI, client: AsyncClient
) -> None:
    """Drive the saga to APPROVED via worker, then compensate to CANCELLED."""
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
    # ADR-0012: forward lands in APPROVING; the worker projects the
    # supplier ack to advance to APPROVED.
    assert r.json()["state_after"] == "approving"

    await _drive_outbox_worker(app)

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "approved"

    # Compensate APPROVE: should cancel at supplier and move to CANCELLED.
    # ``reshare_id`` is sourced from the APPROVE OBSERVATION via
    # ``api._derive_extras`` — no manual ``extras`` needed.
    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "approve", "actor": "staff:test", "rationale": "patron withdrew"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "compensator"
    assert r.json()["state_after"] == "cancelled"

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "cancelled"


async def test_compensate_during_approving_returns_400(client: AsyncClient) -> None:
    """Compensating before the worker drains is rejected with a clear error.

    Per the compensator's guard (saga/flows.py): without a
    ``reshare_id`` there is nothing concrete at the supplier to
    cancel. The endpoint must surface this as a 400 rather than
    enqueue a malformed cancel intent. The test deliberately omits
    the outbox drain so the saga sits in APPROVING with the outbox
    row still pending.
    """
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "ok"},
    )
    assert r.json()["state_after"] == "approving"

    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "approve", "actor": "staff:test", "rationale": "too soon"},
    )
    assert r.status_code == 400, r.text
    assert "supplier ack pending" in r.json()["detail"]


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


async def test_outbox_worker_starts_and_stops_with_lifespan(engine: AsyncEngine) -> None:
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


async def test_outbox_worker_disabled_via_settings(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
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
