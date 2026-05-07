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
