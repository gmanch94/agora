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
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.agents.discovery import DiscoveryAgent
from agora.api.app import _build_outbox_worker, create_app
from agora.clients.crossref import MockCrossrefClient
from agora.clients.sru import MockSruClient, SruRecord
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


# ---------------------------------------------------------------------
# Audit 2026-05-09 batch 2 — input validation hardening
# ---------------------------------------------------------------------


async def test_submit_request_ignores_caller_supplied_request_id(
    client: AsyncClient,
) -> None:
    """Audit #20: caller cannot pin saga URLs by pre-seeding request_id.

    Two requests with the same request_id from the wire must produce
    two distinct sagas with two distinct request_ids — the server
    silently overwrites whatever the client sent.
    """
    pinned = "00000000-0000-0000-0000-000000000001"
    payload = _request_payload()
    payload["request_id"] = pinned

    r1 = await client.post("/requests", json=payload)
    r2 = await client.post("/requests", json=payload)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    body1 = r1.json()
    body2 = r2.json()
    # Both succeed because the server stamped fresh ids.
    assert body1["saga_id"] != body2["saga_id"]
    assert body1["request"]["request_id"] != pinned
    assert body2["request"]["request_id"] != pinned
    assert body1["request"]["request_id"] != body2["request"]["request_id"]


async def test_approve_extras_rejects_unknown_keys(client: AsyncClient) -> None:
    """Audit #15: unknown keys in ``extras`` are rejected at the API boundary.

    StepExtras has ``extra='forbid'`` so a typo or attacker-injected
    key returns a 422 instead of being silently merged into the saga
    step input dict.
    """
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1", "rogue_field": "evil"},
        },
    )
    assert r.status_code == 422, r.text


async def test_approve_extras_rejects_html_in_supplier(client: AsyncClient) -> None:
    """Audit #15: chosen_supplier rejects characters that could XSS templates.

    Pre-fix a payload like ``</script><script>alert(1)</script>`` would
    flow into ``payload['supplier_symbol']`` and into staff-console
    rationale strings. The regex anchors prevent angle brackets and
    quotes from making it past the API boundary.
    """
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {
                "chosen_supplier": "</script><script>alert(1)</script>",
            },
        },
    )
    assert r.status_code == 422, r.text


async def test_approve_extras_rejects_huge_loan_period(client: AsyncClient) -> None:
    """Audit #15: loan_period_days bounded at 365 to refuse millennium-loans."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {
                "chosen_supplier": "MEMBER1",
                "loan_period_days": 999_999_999,
            },
        },
    )
    assert r.status_code == 422, r.text


async def test_reject_terminal_saga_returns_409(
    app: FastAPI, client: AsyncClient
) -> None:
    """Audit #30: rejecting a terminal saga is meaningless and returns 409.

    Drive the saga: ROUTE forward → APPROVE forward (lands in APPROVING)
    → drain outbox to advance to APPROVED → compensate APPROVE (lands
    in CANCELLED, terminal). Then attempt to reject SHIP — refused.
    """
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "ok"},
    )
    assert r.status_code == 200, r.text
    await _drive_outbox_worker(app)

    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "approve", "actor": "staff:test", "rationale": "patron withdrew"},
    )
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "cancelled"

    r = await client.post(
        f"/sagas/{saga_id}/reject",
        json={"step": "ship", "actor": "staff:test", "rationale": "n/a"},
    )
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"]


async def test_reject_after_committed_forward_returns_409(
    client: AsyncClient,
) -> None:
    """Audit #30: rejecting a step that already committed forward is refused."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    assert r.status_code == 200, r.text

    # ROUTE has committed; rejecting it now is incoherent.
    r = await client.post(
        f"/sagas/{saga_id}/reject",
        json={"step": "route", "actor": "staff:test", "rationale": "changed mind"},
    )
    assert r.status_code == 409
    assert "already committed" in r.json()["detail"]


async def test_portal_requests_rejects_malformed_patron_id(
    client: AsyncClient,
) -> None:
    """Audit #17: patron_id with HTML / control chars returns a 422."""
    r = await client.get("/portal/requests", params={"patron_id": "<script>x"})
    assert r.status_code == 422
    r = await client.get("/portal/requests", params={"patron_id": "..\x00"})
    assert r.status_code == 422


async def test_compensate_idempotent_on_repeat_call(
    app: FastAPI, client: AsyncClient
) -> None:
    """Audit #5: a second /compensate must NOT create a second compensator event.

    The compensator now uses a deterministic idempotency key
    ``f"comp-{step}-{saga_id}"`` so a duplicate call collides on
    ``saga_event.UNIQUE(idempotency_key)`` and ``ledger.append`` returns
    the prior event without re-firing the compensator. Defense in
    depth alongside the terminal-state guard at ``ledger.py:91-99``.
    """
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    # ROUTE forward.
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff:test",
            "rationale": "ok",
            "extras": {"chosen_supplier": "MEMBER1"},
        },
    )
    assert r.status_code == 200, r.text

    # First compensate — succeeds, saga goes to UNFILLED (ROUTE
    # compensator's terminal target per flows.py).
    r1 = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "route", "actor": "staff:test", "rationale": "withdraw"},
    )
    assert r1.status_code == 200, r1.text
    seq1 = r1.json()["seq"]

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    initial_state = detail["saga"]["current_state"]
    initial_event_count = len(detail["events"])

    # Second compensate — terminal-state guard would normally reject
    # via 409, but the deterministic idempotency key means even if the
    # guard ever falters, the saga_event UNIQUE constraint absorbs the
    # duplicate. Either outcome is acceptable; what's NOT acceptable is
    # a second COMPENSATOR event landing.
    r2 = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "route", "actor": "staff:test", "rationale": "withdraw again"},
    )
    # Terminal-state guard wins first; on a non-terminal compensator
    # in a future flow, the idempotency-key collision would surface as
    # a 200 returning the prior event.
    assert r2.status_code in (200, 409), r2.text

    # Either way: no NEW compensator event landed.
    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == initial_state
    assert len(detail["events"]) == initial_event_count
    compensator_events = [
        e for e in detail["events"]
        if e["kind"] == "compensator" and e["step"] == "route"
    ]
    assert len(compensator_events) == 1
    assert compensator_events[0]["seq"] == seq1


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


async def test_discover_writes_observation_with_candidates(
    app: FastAPI, client: AsyncClient
) -> None:
    """Happy path: agent finds holders, OBSERVATION event written, no state change."""
    sru = MockSruClient(
        records=[
            SruRecord(
                title="Brave New World",
                authors=["Huxley"],
                isbn="9780060850524",
                issn=None,
                holdings=["MEMBER1", "OTHER1"],
                raw_marcxml="",
            )
        ]
    )
    app.state.discovery = DiscoveryAgent(
        sru, crossref=MockCrossrefClient(), consortium_members={"MEMBER1"}
    )

    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    r = await client.post(f"/sagas/{saga_id}/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saga_id"] == saga_id
    assert {c["symbol"] for c in body["candidates"]} == {"MEMBER1", "OTHER1"}
    member = next(c for c in body["candidates"] if c["symbol"] == "MEMBER1")
    assert member["is_consortium_member"] is True
    assert "MEMBER1" in body["rationale"] or "1 in-consortium" in body["rationale"]

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    # Saga still in submitted state — discovery is advisory.
    assert detail["saga"]["current_state"] == "submitted"
    obs = [
        e for e in detail["events"] if e["kind"] == "observation" and e["step"] == "route"
    ]
    assert len(obs) == 1
    assert obs[0]["payload"]["kind"] == "discovery"
    assert obs[0]["idempotency_key"].startswith("discovery-")


async def test_discover_with_no_holders_returns_empty_with_diagnostic(
    client: AsyncClient,
) -> None:
    """Default factory wiring (empty MockSruClient) yields zero candidates + diagnostic."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    r = await client.post(f"/sagas/{saga_id}/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates"] == []
    assert any("zero holders" in d for d in body["diagnostics"])


async def test_discover_is_rerunnable_each_call_new_event(client: AsyncClient) -> None:
    """Two discover calls produce two distinct OBSERVATION events (fresh ULID keys)."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    first = await client.post(f"/sagas/{saga_id}/discover")
    second = await client.post(f"/sagas/{saga_id}/discover")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["event"]["idempotency_key"] != second.json()["event"]["idempotency_key"]

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    obs = [e for e in detail["events"] if e["kind"] == "observation"]
    assert len(obs) == 2


async def test_discover_unknown_saga_returns_404(client: AsyncClient) -> None:
    r = await client.post(f"/sagas/{uuid4()}/discover")
    assert r.status_code == 404


async def test_discover_on_terminal_saga_returns_409(
    app: FastAPI, client: AsyncClient
) -> None:
    """Terminal sagas reject discovery — advisory only on active sagas."""
    from agora.models.events import NewSagaEvent
    from agora.models.lifecycle import (
        EventKind,
        LifecycleState,
        StepName,
        StepOutcome,
    )
    from agora.saga.db import get_sessionmaker
    from agora.saga.idempotency import new_idempotency_key
    from agora.saga.ledger import SagaLedger

    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]

    # Force the saga to a terminal state by appending a CANCEL forward.
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.append(
            NewSagaEvent(
                saga_id=UUID(saga_id),
                kind=EventKind.FORWARD,
                step=StepName.CANCEL,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.CANCELLED,
                actor="staff:test",
                idempotency_key=new_idempotency_key(prefix="cancel"),
                payload={},
                outcome=StepOutcome.COMMITTED,
                rationale="test",
            )
        )

    r = await client.post(f"/sagas/{saga_id}/discover")
    assert r.status_code == 409
    assert "terminal" in r.json()["detail"]


async def test_create_app_threads_consortium_members_from_settings(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> None:
    """``AGORA_CONSORTIUM_MEMBERS`` env → ``app.state.discovery._members``.

    Pinning the wiring at integration scope (not just at the Settings
    property) so a future refactor of ``create_app`` that drops the
    ``settings.consortium_members`` lookup is caught.
    """
    from agora.config import get_settings

    monkeypatch.setenv("AGORA_CONSORTIUM_MEMBERS", "MEMBER1, MEMBER2 ,MEMBER1")
    get_settings.cache_clear()
    try:
        fresh_app = create_app()
        # Internal attribute is the cleanest assertion target —
        # DiscoveryAgent has no public roster accessor today.
        assert fresh_app.state.discovery._members == {"MEMBER1", "MEMBER2"}
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


# ---------------------------------------------------------------------------
# POST /sagas/{id}/override
# ---------------------------------------------------------------------------


async def _drive_to_disputed(app: FastAPI, client: AsyncClient) -> str:
    """Drive a new saga to DISPUTED via the receive compensator.

    Full path: Submit → Route → Approve (+ worker) → Ship (+ worker)
    → Receive (+ worker) → Compensate-receive → DISPUTED.
    """
    saga_id: str = (await client.post("/requests", json=_request_payload())).json()[
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
    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "ok"},
    )
    await _drive_outbox_worker(app)

    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "ship", "actor": "staff:test", "rationale": "ok"},
    )
    await _drive_outbox_worker(app)

    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "receive", "actor": "staff:test", "rationale": "ok"},
    )
    await _drive_outbox_worker(app)

    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "receive", "actor": "staff:test", "rationale": "patron disputes receipt"},
    )
    assert r.json()["state_after"] == "disputed", r.text
    return saga_id


async def test_override_resolves_disputed_to_cancelled(
    app: FastAPI, client: AsyncClient
) -> None:
    """DISPUTED → CANCELLED via override writes a resolve OBSERVATION."""
    saga_id = await _drive_to_disputed(app, client)
    r = await client.post(
        f"/sagas/{saga_id}/override",
        json={
            "target_state": "cancelled",
            "actor": "staff:alice",
            "rationale": "item lost in transit; patron satisfied",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "observation"
    assert body["step"] == "resolve"
    assert body["state_before"] == "disputed"
    assert body["state_after"] == "cancelled"
    assert body["outcome"] == "committed"

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "cancelled"


async def test_override_resolves_disputed_to_unfilled(
    app: FastAPI, client: AsyncClient
) -> None:
    """DISPUTED → UNFILLED via override advances current_state."""
    saga_id = await _drive_to_disputed(app, client)
    r = await client.post(
        f"/sagas/{saga_id}/override",
        json={
            "target_state": "unfilled",
            "actor": "staff:bob",
            "rationale": "item never physically arrived",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["state_after"] == "unfilled"

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "unfilled"


async def test_override_rejects_non_disputed_state(client: AsyncClient) -> None:
    """Override on a non-DISPUTED saga returns 409."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    # Saga is SUBMITTED — not DISPUTED.
    r = await client.post(
        f"/sagas/{saga_id}/override",
        json={"target_state": "cancelled", "actor": "staff:test", "rationale": "wrong state"},
    )
    assert r.status_code == 409
    assert "disputed" in r.json()["detail"]


async def test_override_rejects_disallowed_target_state(client: AsyncClient) -> None:
    """target_state must be 'cancelled' or 'unfilled'; 'shipped' returns 400."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/override",
        json={"target_state": "shipped", "actor": "staff:test", "rationale": "bogus"},
    )
    assert r.status_code == 400
    assert "allowed" in r.json()["detail"]


async def test_override_rejects_unknown_target_state(client: AsyncClient) -> None:
    """Unrecognised target_state string returns 400."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/sagas/{saga_id}/override",
        json={"target_state": "not-a-state", "actor": "staff:test", "rationale": "bogus"},
    )
    assert r.status_code == 400
    assert "invalid target_state" in r.json()["detail"]


async def test_override_unknown_saga_returns_404(client: AsyncClient) -> None:
    r = await client.post(
        f"/sagas/{uuid4()}/override",
        json={"target_state": "cancelled", "actor": "staff:test", "rationale": "bogus"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# UI form endpoint: POST /ui/sagas/{id}/override
# ---------------------------------------------------------------------------


async def test_ui_override_resolves_disputed_to_cancelled(
    app: FastAPI, client: AsyncClient
) -> None:
    """Form POST to /ui/sagas/{id}/override resolves DISPUTED → CANCELLED.

    Asserts 303 redirect to the detail view and that the saga's
    current_state advances atomically.
    """
    saga_id = await _drive_to_disputed(app, client)
    r = await client.post(
        f"/ui/sagas/{saga_id}/override",
        data={
            "target_state": "cancelled",
            "rationale": "patron withdrew dispute",
            "actor": "staff",
        },
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/sagas/{saga_id}/view"

    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "cancelled"
    resolve_ev = next(
        e for e in detail["events"] if e["step"] == "resolve"
    )
    assert resolve_ev["state_before"] == "disputed"
    assert resolve_ev["state_after"] == "cancelled"
    assert resolve_ev["outcome"] == "committed"


async def test_ui_override_wrong_state_returns_409(client: AsyncClient) -> None:
    """Form POST on a non-DISPUTED saga returns 409."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    # Saga is SUBMITTED — not DISPUTED.
    r = await client.post(
        f"/ui/sagas/{saga_id}/override",
        data={"target_state": "cancelled", "rationale": "wrong state", "actor": "staff"},
    )
    assert r.status_code == 409
    assert "disputed" in r.json()["detail"]


async def test_ui_override_invalid_target_returns_400(client: AsyncClient) -> None:
    """Form POST with an unrecognised target_state returns 400."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.post(
        f"/ui/sagas/{saga_id}/override",
        data={"target_state": "not-a-state", "rationale": "bogus", "actor": "staff"},
    )
    assert r.status_code == 400
    assert "invalid target_state" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Saga browser: GET /browser
# ---------------------------------------------------------------------------


async def test_browser_no_filters_returns_all_sagas(client: AsyncClient) -> None:
    """GET /browser with no filters returns all sagas as HTML."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.get("/browser")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert saga_id[:8] in r.text


async def test_browser_state_filter_matches(client: AsyncClient) -> None:
    """state=submitted returns only submitted sagas."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    r = await client.get("/browser?state=submitted")
    assert r.status_code == 200
    assert saga_id[:8] in r.text


async def test_browser_state_filter_excludes(client: AsyncClient) -> None:
    """state=shipped returns empty when only submitted sagas exist."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?state=shipped")
    assert r.status_code == 200
    assert "No sagas match" in r.text


async def test_browser_invalid_state_silently_ignored(client: AsyncClient) -> None:
    """Unrecognised state value is dropped; all sagas returned."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?state=not-a-state")
    assert r.status_code == 200
    # No 400; falls back to unfiltered list.
    assert "Saga browser" in r.text


async def test_browser_library_filter_matches(client: AsyncClient) -> None:
    """library=A matches sagas whose patron_label contains 'a' (case-insensitive)."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()[
        "saga_id"
    ]
    # _request_payload uses library_symbol "A" — patron_label = "p1 @ A"
    r = await client.get("/browser?library=A")
    assert r.status_code == 200
    assert saga_id[:8] in r.text


async def test_browser_library_filter_excludes(client: AsyncClient) -> None:
    """library=ZZZNOMATCH returns empty."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?library=ZZZNOMATCH")
    assert r.status_code == 200
    assert "No sagas match" in r.text


async def test_browser_future_date_from_returns_empty(client: AsyncClient) -> None:
    """date_from far in the future excludes all existing sagas."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?date_from=2099-01-01")
    assert r.status_code == 200
    assert "No sagas match" in r.text


# ---------------------------------------------------------------------------
# GET /sagas — JSON list (lines 1002-1005)
# ---------------------------------------------------------------------------


async def test_get_sagas_list_returns_empty_json(client: AsyncClient) -> None:
    """GET /sagas returns an empty JSON array when no sagas exist (lines 1002-1005)."""
    r = await client.get("/sagas")
    assert r.status_code == 200
    assert r.json() == []


async def test_get_sagas_list_includes_submitted_saga(client: AsyncClient) -> None:
    """GET /sagas lists newly submitted sagas."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    r = await client.get("/sagas")
    assert r.status_code == 200
    ids = [s["saga_id"] for s in r.json()]
    assert saga_id in ids


# ---------------------------------------------------------------------------
# GET /sagas/{id} — not found (line 1015)
# ---------------------------------------------------------------------------


async def test_get_saga_unknown_id_returns_404(client: AsyncClient) -> None:
    """GET /sagas/{id} returns 404 for an unknown saga (line 1015)."""
    r = await client.get(f"/sagas/{uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /browser — invalid date filters silently ignored (lines 554-567)
# ---------------------------------------------------------------------------


async def test_browser_invalid_date_from_silently_ignored(client: AsyncClient) -> None:
    """Unparseable date_from is ignored; browser still responds 200 (lines 554-555)."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?date_from=not-a-date")
    assert r.status_code == 200


async def test_browser_invalid_date_to_silently_ignored(client: AsyncClient) -> None:
    """Unparseable date_to is ignored; browser still responds 200 (lines 558-567)."""
    await client.post("/requests", json=_request_payload())
    r = await client.get("/browser?date_to=not-a-date")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# _require_console_auth — wrong credentials returns 401 (line 491)
# ---------------------------------------------------------------------------


async def test_ui_wrong_credentials_returns_401(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI endpoint with wrong Basic auth credentials returns 401 (line 491)."""
    from agora.config import get_settings

    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "secret")
    get_settings.cache_clear()
    try:
        app_auth = create_app()
        transport = ASGITransport(app=app_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                f"/ui/sagas/{uuid4()}/reject",
                data={"step": "route", "rationale": "x"},
                auth=("staff", "wrong-password"),
            )
        assert r.status_code == 401
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# POST /ui/sagas/{id}/approve — error paths (lines 710, 741-742)
# ---------------------------------------------------------------------------


async def test_ui_approve_non_approvable_step_returns_400(
    client: AsyncClient,
) -> None:
    """UI approve with a non-approvable step returns 400 (line 710)."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    r = await client.post(
        f"/ui/sagas/{saga_id}/approve",
        data={"step": "submit", "rationale": "x"},
    )
    assert r.status_code == 400


async def test_ui_approve_unknown_saga_returns_409(
    client: AsyncClient,
) -> None:
    """UI approve on unknown saga → SagaNotFoundError → 409 (lines 741-742)."""
    r = await client.post(
        f"/ui/sagas/{uuid4()}/approve",
        data={"step": "route", "rationale": "x"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /ui/sagas/{id}/reject — SagaNotFoundError returns 404 (lines 851-852)
# ---------------------------------------------------------------------------


async def test_ui_reject_unknown_saga_returns_404(client: AsyncClient) -> None:
    """UI reject on unknown saga returns 404 (lines 851-852)."""
    r = await client.post(
        f"/ui/sagas/{uuid4()}/reject",
        data={"step": "route", "rationale": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /ui/sagas/{id}/compensate — SagaNotFoundError returns 404 (lines 887-888)
# ---------------------------------------------------------------------------


async def test_ui_compensate_unknown_saga_returns_404(client: AsyncClient) -> None:
    """UI compensate on unknown saga returns 404 (lines 887-888)."""
    r = await client.post(
        f"/ui/sagas/{uuid4()}/compensate",
        data={"step": "route", "rationale": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /ui/sagas/{id}/discover — terminal saga returns 409 (line 770)
# ---------------------------------------------------------------------------


async def test_ui_discover_terminal_saga_returns_409(
    app: FastAPI, client: AsyncClient
) -> None:
    """UI discover on a terminal saga returns 409 (line 770)."""
    from agora.models.events import NewSagaEvent
    from agora.models.lifecycle import (
        EventKind,
        LifecycleState,
        StepName,
        StepOutcome,
    )
    from agora.saga.db import get_sessionmaker
    from agora.saga.idempotency import new_idempotency_key
    from agora.saga.ledger import SagaLedger

    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.append(
            NewSagaEvent(
                saga_id=UUID(saga_id),
                kind=EventKind.FORWARD,
                step=StepName.CANCEL,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.CANCELLED,
                actor="staff:test",
                idempotency_key=new_idempotency_key(prefix="cancel-ui-disc"),
                payload={},
                outcome=StepOutcome.COMMITTED,
                rationale="test",
            )
        )
    r = await client.post(f"/ui/sagas/{saga_id}/discover")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /ui/sagas/{id}/override — disallowed target + unknown saga (918, 930-931)
# ---------------------------------------------------------------------------


async def test_ui_override_disallowed_target_state_returns_400(
    client: AsyncClient,
) -> None:
    """UI override with a valid enum value but disallowed target returns 400 (line 918)."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    r = await client.post(
        f"/ui/sagas/{saga_id}/override",
        data={"target_state": "submitted", "rationale": "x"},
    )
    assert r.status_code == 400


async def test_ui_override_unknown_saga_returns_404(client: AsyncClient) -> None:
    """UI override on unknown saga returns 404 (lines 930-931)."""
    r = await client.post(
        f"/ui/sagas/{uuid4()}/override",
        data={"target_state": "cancelled", "rationale": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /sagas/{id}/approve JSON — TerminalStateError returns 409 (line 1091)
# ---------------------------------------------------------------------------


async def test_json_approve_terminal_saga_returns_409(
    app: FastAPI, client: AsyncClient
) -> None:
    """JSON approve on a terminal saga → TerminalStateError → 409 (line 1091)."""
    from agora.models.events import NewSagaEvent
    from agora.models.lifecycle import (
        EventKind,
        LifecycleState,
        StepName,
        StepOutcome,
    )
    from agora.saga.db import get_sessionmaker
    from agora.saga.idempotency import new_idempotency_key
    from agora.saga.ledger import SagaLedger

    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.append(
            NewSagaEvent(
                saga_id=UUID(saga_id),
                kind=EventKind.FORWARD,
                step=StepName.CANCEL,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.CANCELLED,
                actor="staff:test",
                idempotency_key=new_idempotency_key(prefix="cancel-json-app"),
                payload={},
                outcome=StepOutcome.COMMITTED,
                rationale="test",
            )
        )
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "route", "actor": "staff:test", "rationale": "x",
              "extras": {"chosen_supplier": "LIB-A"}},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /sagas/{id}/compensate JSON — SagaNotFoundError returns 404 (line 1238)
# ---------------------------------------------------------------------------


async def test_json_compensate_unknown_saga_returns_404(client: AsyncClient) -> None:
    """JSON compensate on unknown saga returns 404 (line 1238)."""
    r = await client.post(
        f"/sagas/{uuid4()}/compensate",
        json={"step": "route", "actor": "staff:test", "rationale": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tracking scanner disabled via env var (lines 408-410)
# ---------------------------------------------------------------------------


async def test_tracking_scanner_disabled_via_settings(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AGORA_TRACKING_SCANNER_ENABLED=0`` skips spawning the scanner task
    (lines 408-410)."""
    from agora.config import get_settings

    monkeypatch.setenv("AGORA_TRACKING_SCANNER_ENABLED", "0")
    get_settings.cache_clear()
    try:
        app_noscan = create_app()
        async with app_noscan.router.lifespan_context(app_noscan):
            assert app_noscan.state.tracking_scanner is None
            assert app_noscan.state.tracking_scanner_task is None
    finally:
        get_settings.cache_clear()
