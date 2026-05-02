"""End-to-end happy-path demo.

Runs the full lifecycle Submitted → Routed → Approved → Shipped →
Returned against an in-memory SQLite + the MockReShareClient. Prints
the resulting saga ledger.

Usage:
    python -m agora.demos.happy_path
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agora.agents.discovery import DiscoveryAgent
from agora.agents.policy import PolicyAgent
from agora.agents.routing import RoutingAgent
from agora.agents.transaction import TransactionAgent
from agora.clients.openurl import parse_openurl
from agora.clients.reshare import MockReShareClient
from agora.clients.sru import MockSruClient, SruRecord
from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.models.request import IllRequest, LibraryRef, PatronRef, RequestType
from agora.saga.context import SagaContext
from agora.saga.coordinator import Coordinator
from agora.saga.db import Base, override_engine
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger

SAMPLE_OPENURL = (
    "ctx_ver=Z39.88-2004&rft.genre=book&rft.btitle=Brave+New+World"
    "&rft.au=Huxley&rft.isbn=9780060850524&rft.date=2006"
)


async def main() -> None:
    # --- bootstrap in-memory DB --------------------------------------
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    override_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)

    # --- agents + clients --------------------------------------------
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
    reshare = MockReShareClient()
    discovery = DiscoveryAgent(sru, consortium_members={"MEMBER1"})
    routing = RoutingAgent()
    policy = PolicyAgent()
    tx = TransactionAgent(reshare)  # type: ignore[arg-type]
    registry = build_registry(tx)

    # --- build request -----------------------------------------------
    item, citation = parse_openurl(SAMPLE_OPENURL)
    request = IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="alice"),
        requesting_library=LibraryRef(symbol="A", name="Library A"),
        item=item,
        citation=citation,
    )

    # --- run agents (advisory) ---------------------------------------
    discovery_rec = await discovery.run(request)
    routing_rec = await routing.run(discovery_rec.candidates)
    policy_decision = await policy.run(request)

    print("=" * 60)
    print("AGENT RECOMMENDATIONS")
    print("=" * 60)
    print(f"  discovery: {discovery_rec.rationale}")
    print(f"  routing:   {routing_rec.rationale}")
    print(f"  policy:    {policy_decision.rationale}")
    if policy_decision.hard_flags:
        print("  policy BLOCKED -- aborting demo")
        await engine.dispose()
        return
    if not routing_rec.chosen:
        print("  no supplier chosen -- aborting")
        await engine.dispose()
        return
    chosen_supplier = routing_rec.chosen.symbol
    print(f"  -> chosen supplier: {chosen_supplier}")

    # --- create saga + initial submit event --------------------------
    saga_id = uuid4()
    async with sessionmaker() as session, session.begin():
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
                actor="patron:alice",
                idempotency_key=new_idempotency_key(prefix="submit"),
                payload={"submitted_at": datetime.now(UTC).isoformat()},
                outcome=StepOutcome.COMMITTED,
                rationale="Patron submitted via OpenURL.",
            )
        )

    # --- drive lifecycle through gates --------------------------------
    extras: dict = {"chosen_supplier": chosen_supplier}
    for step in (StepName.ROUTE, StepName.APPROVE, StepName.SHIP, StepName.RETURN_ITEM):
        # Open + commit gate (simulating staff click).
        async with sessionmaker() as session, session.begin():
            coord = Coordinator(session=session, registry=registry)
            await coord.open_gate(saga_id=saga_id, step=step, actor="agent:advisory")
            await coord.commit_gate(
                saga_id=saga_id,
                step=step,
                actor="staff:demo",
                rationale=f"approve {step.value}",
            )

        # Run forward step.
        async with sessionmaker() as session, session.begin():
            coord = Coordinator(session=session, registry=registry)
            ledger = SagaLedger(session)
            saga = await ledger.get_saga(saga_id)
            ctx = SagaContext(
                saga_id=saga_id,
                request=request,
                current_state=LifecycleState(saga.current_state),
                idempotency_key=new_idempotency_key(prefix=step.value),
                actor="agent:transaction",
                extras=dict(extras),
            )
            ev = await coord.run_forward(ctx=ctx, step=step)

        # Capture reshare_id from APPROVE forward result for later steps.
        if step == StepName.APPROVE and ev is not None:
            extras["reshare_id"] = ev.payload["reshare_id"]

    # --- print final ledger ------------------------------------------
    async with sessionmaker() as session, session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        events = await ledger.events_for(saga_id)

    print()
    print("=" * 60)
    print(f"SAGA {saga_id} -- final state: {saga.current_state}")
    print("=" * 60)
    for ev in events:
        print(
            f"  seq={ev.seq:02d} "
            f"{ev.kind.value:<12} {ev.step.value:<10} "
            f"{ev.state_before.value:>10} -> {ev.state_after.value:<10} "
            f"outcome={ev.outcome.value:<10} actor={ev.actor}"
        )
    print()
    print("Final ledger projection (JSON):")
    print(json.dumps([e.model_dump(mode="json") for e in events], indent=2, default=str))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
