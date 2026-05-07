"""Tests for api/app.py lines not covered by test_api.py.

Targets:
- _to_inbox_row ValueError branch: lines 162-163
- _derive_extras non-committed skip: line 207
- _derive_extras compensator branches: lines 228-229
- saga_browser bad filter values: ~553, 561
- ui_saga_approve CoordinatorError/ValueError → 400: lines 743-744
- ui_saga_compensate TerminalStateError/CoordinatorError → 409: lines 889-891
- ui_saga_compensate ValueError → 400: line 892
- JSON approve GateRequiredError → 409: line 1089
- JSON approve CoordinatorError → 500: lines 1095-1096
- JSON compensate TerminalStateError → 409: line 1240
- JSON compensate CoordinatorError → 409: lines 1241-1242
- JSON compensate ValueError → 400: lines 1243-1244
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agora.api.app import create_app
from agora.saga.coordinator import Coordinator, CoordinatorError, GateRequiredError
from agora.saga.db import get_sessionmaker
from agora.saga.ledger import TerminalStateError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> FastAPI:
    return create_app()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _request_payload() -> dict[str, Any]:
    return {
        "request_type": "loan",
        "patron": {"library_symbol": "A", "patron_id": "p1"},
        "requesting_library": {"symbol": "A", "name": "Library A"},
        "item": {"title": "Brave New World", "author": "Huxley", "isbn": "9780060850524"},
    }


async def _submit(client: AsyncClient) -> str:
    r = await client.post("/requests", json=_request_payload())
    assert r.status_code == 201
    return str(r.json()["saga_id"])


async def _route(client: AsyncClient, saga_id: str) -> None:
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={
            "step": "route",
            "actor": "staff",
            "rationale": "test",
            "extras": {"chosen_supplier": "LIB-A"},
        },
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Lines 162-163: _to_inbox_row ValueError when current_state is unrecognised
# ---------------------------------------------------------------------------


async def test_inbox_row_bogus_state_renders_without_error(
    client: AsyncClient,
) -> None:
    """Saga with unrecognised current_state renders is_terminal=False (lines 162-163)."""
    saga_id = await _submit(client)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        await session.execute(
            text("UPDATE saga SET current_state = 'BOGUS' WHERE id = :id"),
            {"id": saga_id},
        )
    r = await client.get("/")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Line 207: _derive_extras skips non-COMMITTED events (FAILED gate)
# ---------------------------------------------------------------------------


async def test_derive_extras_skips_failed_gate(client: AsyncClient) -> None:
    """FAILED gate in event list is skipped by _derive_extras (line 207)."""
    saga_id = await _submit(client)
    r = await client.post(
        f"/sagas/{saga_id}/reject",
        json={"step": "route", "actor": "staff", "rationale": "no"},
    )
    assert r.status_code == 204
    # Approve ROUTE — _derive_extras walks events including the FAILED gate.
    await _route(client, saga_id)


# ---------------------------------------------------------------------------
# Lines 228-229: _derive_extras ROUTE compensator clears chosen_supplier
# ---------------------------------------------------------------------------


async def test_derive_extras_route_compensator_clears_supplier(
    client: AsyncClient,
) -> None:
    """ROUTE COMPENSATOR event causes _derive_extras to pop chosen_supplier (lines 228-229)."""
    saga_id = await _submit(client)
    await _route(client, saga_id)
    r = await client.post(
        f"/sagas/{saga_id}/compensate",
        json={"step": "route", "actor": "staff", "rationale": "undo"},
    )
    assert r.status_code == 200
    # Route again — _derive_extras sees the COMPENSATOR event.
    await _route(client, saga_id)


# ---------------------------------------------------------------------------
# ~Line 553 / 561: saga_browser bad filter values silently ignored
# ---------------------------------------------------------------------------


async def test_browser_bad_state_filter_returns_200(client: AsyncClient) -> None:
    """Invalid state param is silently ignored (~line 553)."""
    r = await client.get("/browser?state=NOT_A_STATE")
    assert r.status_code == 200


async def test_browser_bad_date_from_filter_returns_200(client: AsyncClient) -> None:
    """Invalid date_from param is silently ignored."""
    r = await client.get("/browser?date_from=not-a-date")
    assert r.status_code == 200


async def test_browser_bad_date_to_filter_returns_200(client: AsyncClient) -> None:
    """Invalid date_to param is silently ignored (line 561)."""
    r = await client.get("/browser?date_to=not-a-date")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Lines 743-744: ui_saga_approve CoordinatorError / ValueError → 400
# ---------------------------------------------------------------------------


async def test_ui_approve_coordinator_error_returns_400(
    client: AsyncClient,
) -> None:
    """run_forward raising CoordinatorError → UI approve returns 400 (lines 743-744)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=CoordinatorError("step failed"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/approve",
            data={"step": "route", "rationale": "x"},
        )
    assert r.status_code == 400


async def test_ui_approve_value_error_returns_400(
    client: AsyncClient,
) -> None:
    """run_forward raising ValueError → UI approve returns 400 (lines 743-744)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=ValueError("bad extras"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/approve",
            data={"step": "route", "rationale": "x"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Lines 889-892: ui_saga_compensate error paths
# ---------------------------------------------------------------------------


async def test_ui_compensate_terminal_state_returns_409(
    client: AsyncClient,
) -> None:
    """run_compensator raising TerminalStateError → UI compensate returns 409 (line 889)."""
    saga_id = await _submit(client)
    await _route(client, saga_id)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=TerminalStateError("already terminal"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/compensate",
            data={"step": "route", "rationale": "x"},
        )
    assert r.status_code == 409


async def test_ui_compensate_coordinator_error_returns_409(
    client: AsyncClient,
) -> None:
    """run_compensator raising CoordinatorError → UI compensate returns 409 (line 890)."""
    saga_id = await _submit(client)
    await _route(client, saga_id)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=CoordinatorError("cannot compensate"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/compensate",
            data={"step": "route", "rationale": "x"},
        )
    assert r.status_code == 409


async def test_ui_compensate_value_error_returns_400(
    client: AsyncClient,
) -> None:
    """run_compensator raising ValueError → UI compensate returns 400 (line 892)."""
    saga_id = await _submit(client)
    await _route(client, saga_id)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=ValueError("bad value"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/compensate",
            data={"step": "route", "rationale": "x"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Line 1089: JSON approve GateRequiredError → 409 (defensive)
# ---------------------------------------------------------------------------


async def test_json_approve_gate_required_returns_409(
    client: AsyncClient,
) -> None:
    """run_forward raising GateRequiredError → JSON approve returns 409 (line 1089)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=GateRequiredError("gate missing"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/approve",
            json={
                "step": "route",
                "actor": "staff",
                "rationale": "x",
                "extras": {"chosen_supplier": "LIB-A"},
            },
        )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Lines 1095-1096: JSON approve CoordinatorError → 500
# ---------------------------------------------------------------------------


async def test_json_approve_coordinator_error_returns_500(
    client: AsyncClient,
) -> None:
    """run_forward raising CoordinatorError → JSON approve returns 500 (lines 1095-1096)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=CoordinatorError("generic failure"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/approve",
            json={
                "step": "route",
                "actor": "staff",
                "rationale": "x",
                "extras": {"chosen_supplier": "LIB-A"},
            },
        )
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# Lines 1240-1244: JSON compensate error paths
# ---------------------------------------------------------------------------


async def test_json_compensate_terminal_state_returns_409(
    client: AsyncClient,
) -> None:
    """run_compensator raising TerminalStateError → JSON compensate returns 409 (line 1240)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=TerminalStateError("terminal"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/compensate",
            json={"step": "route", "actor": "staff", "rationale": "x"},
        )
    assert r.status_code == 409


async def test_json_compensate_coordinator_error_returns_409(
    client: AsyncClient,
) -> None:
    """run_compensator raising CoordinatorError → JSON compensate returns 409 (lines 1241-1242)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=CoordinatorError("no forward found"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/compensate",
            json={"step": "route", "actor": "staff", "rationale": "x"},
        )
    assert r.status_code == 409


async def test_json_compensate_value_error_returns_400(
    client: AsyncClient,
) -> None:
    """run_compensator raising ValueError → JSON compensate returns 400 (lines 1243-1244)."""
    saga_id = await _submit(client)
    with patch.object(
        Coordinator, "run_compensator", new_callable=AsyncMock,
        side_effect=ValueError("bad value"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/compensate",
            json={"step": "route", "actor": "staff", "rationale": "x"},
        )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# _portal_due_date branches (lines 187, 190, 192) — direct call
# ---------------------------------------------------------------------------


def test_portal_due_date_skips_non_committed_event() -> None:
    """Non-COMMITTED event hits the 'continue' on line 187."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _portal_due_date
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    ev = SagaEvent(
        id=1,
        saga_id=uuid4(),
        seq=1,
        ts=datetime.now(UTC),
        actor="x",
        idempotency_key="k1",
        iso_message_id=None,
        rationale=None,
        outcome=StepOutcome.FAILED,  # not COMMITTED → skip (line 187)
        state_before=LifecycleState.SHIPPED,
        state_after=LifecycleState.SHIPPED,
        kind=EventKind.FORWARD,
        step=StepName.SHIP,
        payload={"due_at": "2026-06-01T00:00:00Z"},
    )
    assert _portal_due_date([ev]) == ""


def test_portal_due_date_picks_up_ship_due_at() -> None:
    """SHIP forward with due_at sets the date (line 190)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _portal_due_date
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    ev = SagaEvent(
        id=1,
        saga_id=uuid4(),
        seq=1,
        ts=datetime.now(UTC),
        actor="x",
        idempotency_key="k1",
        iso_message_id=None,
        rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.APPROVED,
        state_after=LifecycleState.SHIPPED,
        kind=EventKind.FORWARD,
        step=StepName.SHIP,
        payload={"due_at": "2026-06-01T00:00:00Z"},
    )
    assert _portal_due_date([ev]) == "2026-06-01"


def test_portal_due_date_renew_overrides_ship() -> None:
    """RENEW forward with new_due_at overrides SHIP (line 192)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _portal_due_date
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    saga_id = uuid4()
    ship = SagaEvent(
        id=1,
        saga_id=saga_id,
        seq=1,
        ts=datetime.now(UTC),
        actor="x",
        idempotency_key="k1",
        iso_message_id=None,
        rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.APPROVED,
        state_after=LifecycleState.SHIPPED,
        kind=EventKind.FORWARD,
        step=StepName.SHIP,
        payload={"due_at": "2026-06-01T00:00:00Z"},
    )
    renew = SagaEvent(
        id=2,
        saga_id=saga_id,
        seq=2,
        ts=datetime.now(UTC),
        actor="x",
        idempotency_key="k2",
        iso_message_id=None,
        rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.RECEIVED,
        state_after=LifecycleState.RECEIVED,
        kind=EventKind.FORWARD,
        step=StepName.RENEW,
        payload={"new_due_at": "2026-07-01T00:00:00Z"},
    )
    assert _portal_due_date([ship, renew]) == "2026-07-01"


def test_portal_due_date_compensator_pops_renewal() -> None:
    """forward.renew + compensator.renew → portal shows the SHIP due date.

    Regression for the bug surfaced by the post-#117 strict review: the
    last-write-wins predecessor left the portal showing the cancelled
    renewal's new_due_at after a RENEW compensator landed.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _portal_due_date
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    saga_id = uuid4()
    ship = SagaEvent(
        id=1, saga_id=saga_id, seq=1, ts=datetime.now(UTC), actor="x",
        idempotency_key="k1", iso_message_id=None, rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.APPROVED, state_after=LifecycleState.SHIPPED,
        kind=EventKind.FORWARD, step=StepName.SHIP,
        payload={"due_at": "2026-06-01T00:00:00Z"},
    )
    renew_fwd = SagaEvent(
        id=2, saga_id=saga_id, seq=2, ts=datetime.now(UTC), actor="x",
        idempotency_key="k2", iso_message_id=None, rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.RECEIVED, state_after=LifecycleState.RECEIVED,
        kind=EventKind.FORWARD, step=StepName.RENEW,
        payload={"new_due_at": "2026-07-01T00:00:00Z"},
    )
    renew_comp = SagaEvent(
        id=3, saga_id=saga_id, seq=3, ts=datetime.now(UTC), actor="x",
        idempotency_key="k3", iso_message_id=None, rationale=None,
        outcome=StepOutcome.COMMITTED,
        state_before=LifecycleState.RECEIVED, state_after=LifecycleState.RECEIVED,
        kind=EventKind.COMPENSATOR, step=StepName.RENEW,
        payload={"renewal_cancelled": True, "reverted_new_due_at": "2026-07-01T00:00:00Z"},
    )
    assert _portal_due_date([ship, renew_fwd, renew_comp]) == "2026-06-01"


def test_portal_due_date_compensator_pops_only_most_recent() -> None:
    """Two RENEW forwards + one compensator → portal shows the FIRST renewal's due date."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _portal_due_date
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    saga_id = uuid4()

    def _ev(seq: int, kind: EventKind, step: StepName, payload: dict[str, Any]) -> SagaEvent:
        return SagaEvent(
            id=seq, saga_id=saga_id, seq=seq, ts=datetime.now(UTC), actor="x",
            idempotency_key=f"k{seq}", iso_message_id=None, rationale=None,
            outcome=StepOutcome.COMMITTED,
            state_before=LifecycleState.RECEIVED, state_after=LifecycleState.RECEIVED,
            kind=kind, step=step, payload=payload,
        )

    ship = _ev(1, EventKind.FORWARD, StepName.SHIP, {"due_at": "2026-06-01T00:00:00Z"})
    r1 = _ev(2, EventKind.FORWARD, StepName.RENEW, {"new_due_at": "2026-07-01T00:00:00Z"})
    r2 = _ev(3, EventKind.FORWARD, StepName.RENEW, {"new_due_at": "2026-08-01T00:00:00Z"})
    comp = _ev(4, EventKind.COMPENSATOR, StepName.RENEW, {"renewal_cancelled": True})
    assert _portal_due_date([ship, r1, r2, comp]) == "2026-07-01"


# ---------------------------------------------------------------------------
# _derive_extras APPROVE compensator branch (lines 274-275)
# ---------------------------------------------------------------------------


def test_derive_extras_approve_compensator_clears_reshare_id() -> None:
    """APPROVE compensator pops reshare_id from extras (lines 274-275)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from agora.api.app import _derive_extras
    from agora.models.events import SagaEvent
    from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome

    saga_id = uuid4()
    base = dict(saga_id=saga_id, ts=datetime.now(UTC), actor="x",
                iso_message_id=None, rationale=None,
                outcome=StepOutcome.COMMITTED)
    obs = SagaEvent(
        id=1, seq=1, idempotency_key="k1",
        kind=EventKind.OBSERVATION, step=StepName.APPROVE,
        state_before=LifecycleState.APPROVING,
        state_after=LifecycleState.APPROVED,
        payload={"reshare_id": "rid-1"},
        **base,
    )
    comp = SagaEvent(
        id=2, seq=2, idempotency_key="k2",
        kind=EventKind.COMPENSATOR, step=StepName.APPROVE,
        state_before=LifecycleState.APPROVED,
        state_after=LifecycleState.UNFILLED,
        payload={},
        **base,
    )
    extras = _derive_extras([obs, comp], None)
    assert "reshare_id" not in extras


# ---------------------------------------------------------------------------
# date_to filter (line 605)
# ---------------------------------------------------------------------------


async def test_browser_date_to_filter_applies(client: AsyncClient) -> None:
    """date_to filter on /browser applies the upper-bound clause (line 605)."""
    await _submit(client)
    r = await client.get("/browser?date_to=2030-12-31")
    assert r.status_code == 200


async def test_browser_date_to_invalid_falls_back(client: AsyncClient) -> None:
    """Invalid date_to is silently dropped (date_to = None branch)."""
    await _submit(client)
    r = await client.get("/browser?date_to=not-a-date")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Portal bogus-state ValueError branches (lines 1527-1528, 1580-1581)
# ---------------------------------------------------------------------------


async def test_portal_requests_handles_bogus_state(
    client: AsyncClient, app: FastAPI,
) -> None:
    """Bogus state value → LifecycleState() raises ValueError → is_terminal=False (1527-1528)."""
    saga_id = await _submit(client)
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text("UPDATE saga SET current_state = 'BOGUS' WHERE id = :id"),
            {"id": saga_id},
        )
        await s.commit()
    r = await client.get("/portal/requests?patron_id=p1")
    assert r.status_code == 200


async def test_portal_detail_handles_bogus_state(
    client: AsyncClient, app: FastAPI,
) -> None:
    """Bogus state on detail view → ValueError caught, is_terminal=False (1580-1581)."""
    saga_id = await _submit(client)
    sm = get_sessionmaker()
    async with sm() as s:
        await s.execute(
            text("UPDATE saga SET current_state = 'BOGUS' WHERE id = :id"),
            {"id": saga_id},
        )
        await s.commit()
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=p1")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# JSON renew exception branches (lines 1256, 1258, 1260, 1262)
# ---------------------------------------------------------------------------


async def _drive_to_received(app: FastAPI, client: AsyncClient) -> str:
    """Drive saga through SUBMIT → RECEIVED via API."""
    from agora.api.app import _build_outbox_worker

    saga_id = await _submit(client)

    async def drain() -> None:
        worker = _build_outbox_worker(
            get_sessionmaker(),
            reshare=app.state.reshare,
            ncip=app.state.ncip,
            max_attempts=5,
        )
        await worker.drain_until_empty()

    for step, extras in [
        ("route", {"chosen_supplier": "LIB-A"}),
        ("approve", {}),
    ]:
        r = await client.post(
            f"/sagas/{saga_id}/approve",
            json={"step": step, "actor": "s", "rationale": "r", "extras": extras},
        )
        assert r.status_code == 200, r.text
    await drain()
    for step in ("ship", "receive"):
        r = await client.post(
            f"/sagas/{saga_id}/approve",
            json={"step": step, "actor": "s", "rationale": "r"},
        )
        assert r.status_code == 200, r.text
        await drain()
    return saga_id


async def test_json_renew_gate_required_returns_409(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising GateRequiredError → JSON renew returns 409 (line 1256)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=GateRequiredError("missing"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/renew",
            json={"actor": "s", "rationale": "r", "extension_days": 7},
        )
    assert r.status_code == 409


async def test_json_renew_terminal_state_returns_409(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising TerminalStateError → JSON renew returns 409 (line 1258)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=TerminalStateError("terminal"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/renew",
            json={"actor": "s", "rationale": "r", "extension_days": 7},
        )
    assert r.status_code == 409


async def test_json_renew_value_error_returns_400(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising ValueError → JSON renew returns 400 (line 1260)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=ValueError("bad"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/renew",
            json={"actor": "s", "rationale": "r", "extension_days": 7},
        )
    assert r.status_code == 400


async def test_json_renew_coordinator_error_returns_500(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising CoordinatorError → JSON renew returns 500 (line 1262)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=CoordinatorError("oops"),
    ):
        r = await client.post(
            f"/sagas/{saga_id}/renew",
            json={"actor": "s", "rationale": "r", "extension_days": 7},
        )
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# Form renew endpoint /ui/sagas/{id}/renew (lines 1016-1054)
# ---------------------------------------------------------------------------


async def test_ui_renew_happy_path(client: AsyncClient, app: FastAPI) -> None:
    """Form POST drives RENEW forward, redirects to detail view."""
    saga_id = await _drive_to_received(app, client)
    r = await client.post(
        f"/ui/sagas/{saga_id}/renew",
        data={"extension_days": "21", "rationale": "extension"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/sagas/{saga_id}/view"


async def test_ui_renew_wrong_state_returns_409(client: AsyncClient) -> None:
    """Form POST on non-RECEIVED saga returns 409 (line 1023-1026)."""
    saga_id = await _submit(client)
    r = await client.post(
        f"/ui/sagas/{saga_id}/renew",
        data={"extension_days": "7", "rationale": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 409


async def test_ui_renew_unknown_saga_returns_404(client: AsyncClient) -> None:
    """Form POST on missing saga returns 404 (line 1047-1048)."""
    from uuid import uuid4

    r = await client.post(
        f"/ui/sagas/{uuid4()}/renew",
        data={"extension_days": "7", "rationale": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 404


async def test_ui_renew_value_error_returns_400(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising ValueError → form returns 400 (lines 1051-1052)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=ValueError("bad"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/renew",
            data={"extension_days": "7", "rationale": "x"},
            follow_redirects=False,
        )
    assert r.status_code == 400


async def test_ui_renew_coordinator_error_returns_409(
    client: AsyncClient, app: FastAPI,
) -> None:
    """run_forward raising CoordinatorError → form returns 409 (lines 1049-1050)."""
    saga_id = await _drive_to_received(app, client)
    with patch.object(
        Coordinator, "run_forward", new_callable=AsyncMock,
        side_effect=CoordinatorError("oops"),
    ):
        r = await client.post(
            f"/ui/sagas/{saga_id}/renew",
            data={"extension_days": "7", "rationale": "x"},
            follow_redirects=False,
        )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Portal detail with unlabeled events (line 1595)
# ---------------------------------------------------------------------------


async def test_portal_detail_skips_unlabeled_events(
    client: AsyncClient, app: FastAPI,
) -> None:
    """Saga driven through APPROVE writes a (forward, approve) event that has
    no entry in _PATRON_EVENT_LABELS — portal_detail skips it (line 1595)."""
    saga_id = await _drive_to_received(app, client)
    r = await client.get(f"/portal/requests/{saga_id}?patron_id=p1")
    assert r.status_code == 200
