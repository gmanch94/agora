"""Tests for the RENEW saga step (loan extension)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agora.agents.transaction import TransactionAgent
from agora.api.app import _build_outbox_worker, create_app
from agora.clients.ncip import MockNcipClient
from agora.clients.reshare import MockReShareClient
from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome
from agora.models.request import (
    Citation,
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)
from agora.saga.context import SagaContext
from agora.saga.coordinator import Coordinator, GateRequiredError
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger
from agora.saga.outbox import (
    OutboxWorker,
    make_ncip_handler,
    make_reshare_handler,
    make_reshare_on_success,
)


def _build_request() -> IllRequest:
    return IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p-renew"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="Fahrenheit 451", author="Bradbury", isbn="9781451673319"),
        citation=Citation(
            raw="ctx_ver=Z39.88-2004",
            parsed_from="openurl",
            parsed_at=datetime.now(UTC),
        ),
    )


async def _drain(session: AsyncSession, reshare: MockReShareClient) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(bind=session.bind, expire_on_commit=False)
    worker = OutboxWorker(
        sm,
        handlers={
            "reshare": make_reshare_handler(reshare),
            "ncip": make_ncip_handler(MockNcipClient()),
        },
        on_success={"reshare": make_reshare_on_success()},
    )
    await worker.drain_until_empty()


async def _gate_and_run(
    session: AsyncSession,
    registry: Any,
    saga_id: Any,
    request: IllRequest,
    step: StepName,
    extras: dict[str, Any],
    *,
    from_state: LifecycleState | None = None,
) -> Any:
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=step, actor="staff:test")
        await coord.commit_gate(
            saga_id=saga_id, step=step, actor="staff:test", rationale=f"approve {step.value}"
        )

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=from_state or LifecycleState(saga.current_state),
            idempotency_key=new_idempotency_key(prefix=step.value),
            actor="staff:test",
            extras=dict(extras),
        )
        return await coord.run_forward(ctx=ctx, step=step)


async def _saga_at_received(
    session: AsyncSession,
    reshare: MockReShareClient,
) -> tuple[Any, IllRequest, str]:
    """Create a saga and drive it to RECEIVED; return (saga_id, request, reshare_id)."""
    from agora.models.events import NewSagaEvent

    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.SUBMIT,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.SUBMITTED,
                actor="patron",
                idempotency_key=new_idempotency_key(),
                payload={},
                outcome=StepOutcome.COMMITTED,
            )
        )

    extras: dict[str, Any] = {"chosen_supplier": "LIB-B"}
    await _gate_and_run(session, registry, saga_id, request, StepName.ROUTE, extras)
    await _gate_and_run(session, registry, saga_id, request, StepName.APPROVE, extras)
    await _drain(session, reshare)

    # pick up reshare_id from APPROVE OBSERVATION
    async with session.begin():
        events = await SagaLedger(session).events_for(saga_id)
    obs = next(
        e for e in events
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    )
    extras["reshare_id"] = obs.payload["reshare_id"]

    await _gate_and_run(session, registry, saga_id, request, StepName.SHIP, extras)
    await _drain(session, reshare)
    await _gate_and_run(session, registry, saga_id, request, StepName.RECEIVE, extras)
    await _drain(session, reshare)

    return saga_id, request, extras["reshare_id"]


# ------------------------------------------------------------------ coordinator


@pytest.mark.asyncio
async def test_renew_forward_stays_received(session: AsyncSession) -> None:
    """RENEW forward keeps state at RECEIVED and records extension on payload."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    ev = await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.RENEW,
        {"reshare_id": reshare_id, "extension_days": 14},
        from_state=LifecycleState.RECEIVED,
    )

    assert ev.state_after == LifecycleState.RECEIVED
    assert ev.payload["extension_days"] == 14
    assert "new_due_at" in ev.payload
    assert ev.payload["reshare_id"] == reshare_id


@pytest.mark.asyncio
async def test_renew_requires_committed_gate(session: AsyncSession) -> None:
    """RENEW forward without an approved gate raises GateRequiredError."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.RECEIVED,
            idempotency_key=new_idempotency_key(prefix="renew"),
            actor="staff:test",
            extras={"reshare_id": reshare_id, "extension_days": 28},
        )
        with pytest.raises(GateRequiredError):
            await coord.run_forward(ctx=ctx, step=StepName.RENEW)


@pytest.mark.asyncio
async def test_renew_compensator_stays_received(session: AsyncSession) -> None:
    """RENEW compensator cancels the renewal; saga stays at RECEIVED."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    fwd = await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.RENEW,
        {"reshare_id": reshare_id, "extension_days": 28},
        from_state=LifecycleState.RECEIVED,
    )
    assert fwd.state_after == LifecycleState.RECEIVED

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.RECEIVED,
            idempotency_key=new_idempotency_key(prefix="comp-renew"),
            actor="staff:test",
            extras={"reshare_id": reshare_id},
        )
        comp = await coord.run_compensator(ctx=ctx, step=StepName.RENEW)

    assert comp.state_after == LifecycleState.RECEIVED
    assert comp.payload["renewal_cancelled"] is True
    assert comp.payload["reverted_new_due_at"] == fwd.payload["new_due_at"]


@pytest.mark.asyncio
async def test_multiple_renewals_compose(session: AsyncSession) -> None:
    """Two consecutive RENEW forwards both land; each extends due date from now."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)
    extras = {"reshare_id": reshare_id, "extension_days": 7}

    ev1 = await _gate_and_run(
        session, registry, saga_id, request, StepName.RENEW, extras,
        from_state=LifecycleState.RECEIVED,
    )
    ev2 = await _gate_and_run(
        session, registry, saga_id, request, StepName.RENEW, extras,
        from_state=LifecycleState.RECEIVED,
    )

    assert ev1.state_after == LifecycleState.RECEIVED
    assert ev2.state_after == LifecycleState.RECEIVED
    # Each renewal lands its own new_due_at; second must be >= first.
    assert ev2.payload["new_due_at"] >= ev1.payload["new_due_at"]


@pytest.mark.asyncio
async def test_renew_enqueues_reshare_outbox_intent(session: AsyncSession) -> None:
    """RENEW forward emits one ReShare outbox intent for renew_request."""
    from sqlalchemy import select

    from agora.saga.db import OutboxRow

    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    async with session.begin():
        before = (
            (await session.execute(select(OutboxRow).where(OutboxRow.saga_id == saga_id)))
            .scalars()
            .all()
        )

    await _gate_and_run(
        session, registry, saga_id, request, StepName.RENEW,
        {"reshare_id": reshare_id, "extension_days": 21},
        from_state=LifecycleState.RECEIVED,
    )

    async with session.begin():
        after = (
            (await session.execute(select(OutboxRow).where(OutboxRow.saga_id == saga_id)))
            .scalars()
            .all()
        )
    new_rows = [r for r in after if r not in before]
    assert len(new_rows) == 1
    row = new_rows[0]
    assert row.target == "reshare"
    assert row.payload["action"] == "renew_request"
    assert row.payload["args"]["reshare_id"] == reshare_id
    assert row.payload["args"]["extension_days"] == 21


# ------------------------------------------------------------------ mock client


@pytest.mark.asyncio
async def test_mock_reshare_renew_request_succeeds() -> None:
    """MockReShareClient.renew_request transitions state to Loaned."""
    client = MockReShareClient()
    # Must send_request first so the mock has a record.
    result = await client.send_request(
        idempotency_key="idem-send",
        request_payload={"title": "Fahrenheit 451"},
        supplier_symbol="LIB-B",
    )
    rid = result.reshare_id

    renewed = await client.renew_request(
        idempotency_key="idem-renew",
        reshare_id=rid,
        extension_days=14,
    )
    assert renewed.reshare_id == rid
    assert renewed.state == "Loaned"


@pytest.mark.asyncio
async def test_mock_reshare_renew_idempotent() -> None:
    """MockReShareClient.renew_request replays on same idempotency key."""
    client = MockReShareClient()
    result = await client.send_request(
        idempotency_key="idem-send2",
        request_payload={"title": "Fahrenheit 451"},
        supplier_symbol="LIB-B",
    )
    rid = result.reshare_id

    r1 = await client.renew_request(idempotency_key="idem-renew2", reshare_id=rid, extension_days=7)
    r2 = await client.renew_request(idempotency_key="idem-renew2", reshare_id=rid, extension_days=7)
    assert r1.iso_message_id == r2.iso_message_id


# ------------------------------------------------------------------ API


def _request_payload() -> dict[str, Any]:
    return {
        "request_type": "loan",
        "patron": {"library_symbol": "A", "patron_id": "p-renew-api"},
        "requesting_library": {"symbol": "A", "name": "Library A"},
        "item": {"title": "Fahrenheit 451", "author": "Bradbury", "isbn": "9781451673319"},
        "citation": {
            "raw": "ctx_ver=Z39.88-2004",
            "parsed_from": "openurl",
            "parsed_at": datetime.now(UTC).isoformat(),
        },
    }


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> Any:
    return create_app()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _drive_outbox(app: Any) -> None:
    from agora.saga.db import get_sessionmaker

    worker = _build_outbox_worker(
        get_sessionmaker(),
        reshare=app.state.reshare,
        ncip=app.state.ncip,
        max_attempts=5,
    )
    await worker.drain_until_empty()


async def _api_saga_at_received(app: Any, client: AsyncClient) -> str:
    """Drive a saga to RECEIVED via the API; return saga_id."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]

    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "route", "actor": "staff:test", "rationale": "r",
              "extras": {"chosen_supplier": "LIB-B"}},
    )
    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "approve", "actor": "staff:test", "rationale": "r"},
    )
    await _drive_outbox(app)
    await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "ship", "actor": "staff:test", "rationale": "r"},
    )
    await _drive_outbox(app)
    r = await client.post(
        f"/sagas/{saga_id}/approve",
        json={"step": "receive", "actor": "staff:test", "rationale": "r"},
    )
    assert r.status_code == 200, r.text
    return str(saga_id)


async def test_api_renew_happy_path(app: Any, client: AsyncClient) -> None:
    """POST /sagas/{id}/renew on a RECEIVED saga returns 200, state stays received."""
    saga_id = await _api_saga_at_received(app, client)

    r = await client.post(
        f"/sagas/{saga_id}/renew",
        json={"actor": "staff:test", "rationale": "patron requested extension", "extension_days": 14},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state_after"] == "received"
    assert body["step"] == "renew"
    assert body["payload"]["extension_days"] == 14
    assert "new_due_at" in body["payload"]

    # Saga detail still shows received.
    detail = (await client.get(f"/sagas/{saga_id}")).json()
    assert detail["saga"]["current_state"] == "received"


async def test_api_renew_wrong_state_returns_409(client: AsyncClient) -> None:
    """POST /sagas/{id}/renew on a non-RECEIVED saga returns 409."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    # Saga is at SUBMITTED — renew must reject.
    r = await client.post(
        f"/sagas/{saga_id}/renew",
        json={"actor": "staff:test", "rationale": "bad state test", "extension_days": 28},
    )
    assert r.status_code == 409
    assert "received" in r.json()["detail"]


async def test_api_renew_unknown_saga_returns_404(client: AsyncClient) -> None:
    """POST /sagas/{id}/renew on non-existent saga returns 404."""
    r = await client.post(
        f"/sagas/{uuid4()}/renew",
        json={"actor": "staff:test", "rationale": "nope", "extension_days": 28},
    )
    assert r.status_code == 404


async def test_api_renew_extension_days_validation(client: AsyncClient) -> None:
    """extension_days outside [1, 180] returns 422."""
    saga_id = (await client.post("/requests", json=_request_payload())).json()["saga_id"]
    r = await client.post(
        f"/sagas/{saga_id}/renew",
        json={"actor": "staff:test", "rationale": "test", "extension_days": 0},
    )
    assert r.status_code == 422


# ------------------------------------------------------------------ flows guard

@pytest.mark.asyncio
async def test_renew_forward_missing_reshare_id_raises(session: AsyncSession) -> None:
    """renew_forward raises ValueError when reshare_id is absent from extras (line 503)."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, _reshare_id = await _saga_at_received(session, reshare)

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=StepName.RENEW, actor="staff:test")
        await coord.commit_gate(
            saga_id=saga_id,
            step=StepName.RENEW,
            actor="staff:test",
            rationale="test missing reshare_id",
        )

    with pytest.raises(ValueError, match="reshare_id"):
        async with session.begin():
            coord = Coordinator(session=session, registry=registry)
            ctx = SagaContext(
                saga_id=saga_id,
                request=request,
                current_state=LifecycleState.RECEIVED,
                idempotency_key=new_idempotency_key(prefix="renew"),
                actor="staff:test",
                extras={},  # intentionally empty — no reshare_id
            )
            await coord.run_forward(ctx=ctx, step=StepName.RENEW)


# ---------------------------- extension_days bounds (single chokepoint)
# Validation lives in renew_forward itself so JSON (RenewBody) and HTMX
# (Form) callers cannot diverge. Pre-fix behaviour: `int(x) or DEFAULT`
# silently rewrote 0 → 28 and let -5 through.


@pytest.mark.parametrize("bad_days", [0, -1, -5, 181, 1_000])
@pytest.mark.asyncio
async def test_renew_forward_rejects_out_of_range_extension_days(
    session: AsyncSession, bad_days: int
) -> None:
    """renew_forward raises ValueError on extension_days outside [1, 180]."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=StepName.RENEW, actor="staff:test")
        await coord.commit_gate(
            saga_id=saga_id,
            step=StepName.RENEW,
            actor="staff:test",
            rationale=f"test bad extension_days={bad_days}",
        )

    with pytest.raises(ValueError, match="extension_days"):
        async with session.begin():
            coord = Coordinator(session=session, registry=registry)
            ctx = SagaContext(
                saga_id=saga_id,
                request=request,
                current_state=LifecycleState.RECEIVED,
                idempotency_key=new_idempotency_key(prefix="renew"),
                actor="staff:test",
                extras={"reshare_id": reshare_id, "extension_days": bad_days},
            )
            await coord.run_forward(ctx=ctx, step=StepName.RENEW)


@pytest.mark.asyncio
async def test_renew_forward_extension_days_none_uses_default(
    session: AsyncSession,
) -> None:
    """Missing extension_days falls back to DEFAULT_LOAN_PERIOD_DAYS (28)."""
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))
    saga_id, request, reshare_id = await _saga_at_received(session, reshare)

    ev = await _gate_and_run(
        session, registry, saga_id, request, StepName.RENEW,
        {"reshare_id": reshare_id},  # no extension_days
        from_state=LifecycleState.RECEIVED,
    )
    assert ev.payload["extension_days"] == 28


async def test_ui_renew_zero_days_returns_400(app: Any, client: AsyncClient) -> None:
    """HTMX form path: extension_days=0 must reject — Form() has no Pydantic gate."""
    saga_id = await _api_saga_at_received(app, client)
    r = await client.post(
        f"/ui/sagas/{saga_id}/renew",
        data={"extension_days": "0", "rationale": "should be rejected"},
    )
    assert r.status_code == 400, r.text
    assert "extension_days" in r.json()["detail"]


async def test_ui_renew_negative_days_returns_400(app: Any, client: AsyncClient) -> None:
    """HTMX form path: extension_days=-5 must reject — pre-fix would have produced a past due date."""
    saga_id = await _api_saga_at_received(app, client)
    r = await client.post(
        f"/ui/sagas/{saga_id}/renew",
        data={"extension_days": "-5", "rationale": "should be rejected"},
    )
    assert r.status_code == 400, r.text
    assert "extension_days" in r.json()["detail"]
