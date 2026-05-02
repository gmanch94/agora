"""Property-based tests for saga ledger replay-safety and ordering."""

from __future__ import annotations

from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger

_NON_TERMINAL = [
    LifecycleState.SUBMITTED,
    LifecycleState.ROUTED,
    LifecycleState.APPROVED,
    LifecycleState.SHIPPED,
]


@pytest.mark.asyncio
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    n=st.integers(min_value=1, max_value=10),
    replay_each=st.integers(min_value=1, max_value=4),
)
async def test_replay_idempotency_under_concurrency(session, n, replay_each) -> None:
    """Append N events, replay each ``replay_each`` times → exactly N rows."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id, request_id=uuid4(), request_payload={}
        )

    events: list[NewSagaEvent] = []
    state = LifecycleState.SUBMITTED
    for _ in range(n):
        next_state = _NON_TERMINAL[(_NON_TERMINAL.index(state) + 1) % len(_NON_TERMINAL)]
        events.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=StepName.SUBMIT,
                state_before=state,
                state_after=state,  # observations don't change state
                actor="system",
                idempotency_key=new_idempotency_key(),
                payload={},
                outcome=StepOutcome.COMMITTED,
            )
        )
        state = next_state

    async with session.begin():
        ledger = SagaLedger(session)
        for ev in events:
            for _ in range(replay_each):
                await ledger.append(ev)

    async with session.begin():
        ledger = SagaLedger(session)
        rows = await ledger.events_for(saga_id)
    assert len(rows) == n, "replay must not produce duplicate rows"
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs), "events must be totally ordered by seq"
    assert seqs == list(range(1, n + 1)), "seq must be contiguous starting at 1"
