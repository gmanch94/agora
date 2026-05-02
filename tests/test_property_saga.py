"""Property-based tests for saga ledger replay-safety, ordering, and
compensator symmetry across all migrated step pairs.

Compensator symmetry is the saga's correctness skeleton: every committed
forward must lead — when its compensator runs — to the documented
recovery state, regardless of the saga's prior history. These tests
cover all currently-registered (forward, compensator) pairs and assert:

1. Forward → compensator lands in the documented ``comp_state``.
2. Replay-safety holds: the same compensator idempotency_key, fired N
   times, produces exactly one ledger event and at most one outbox row.
3. Compensators refuse to run without a committed forward (raises
   ``CoordinatorError``).
4. Forward outbox enqueue is replay-safe under repeated forward runs.
5. Terminal-state forward blocks its compensator at the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from agora.agents.transaction import TransactionAgent
from agora.clients.reshare import MockReShareClient
from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.models.request import (
    Citation,
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)
from agora.saga.context import SagaContext
from agora.saga.coordinator import Coordinator, CoordinatorError
from agora.saga.db import OutboxRow
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger, TerminalStateError

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


# ---------------------------------------------------------------------
# Compensator-symmetry properties
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class _StepSpec:
    """Documented (forward, compensator) contract for a step."""

    step: StepName
    pre_state: LifecycleState
    forward_state: LifecycleState
    comp_state: LifecycleState
    forward_has_outbox: bool
    comp_has_outbox: bool
    extras_seed: tuple[tuple[str, str], ...]  # immutable for @given safety


_SPECS: dict[StepName, _StepSpec] = {
    StepName.ROUTE: _StepSpec(
        step=StepName.ROUTE,
        pre_state=LifecycleState.SUBMITTED,
        forward_state=LifecycleState.ROUTED,
        comp_state=LifecycleState.SUBMITTED,
        forward_has_outbox=False,
        comp_has_outbox=False,
        extras_seed=(("chosen_supplier", "B"),),
    ),
    StepName.APPROVE: _StepSpec(
        step=StepName.APPROVE,
        pre_state=LifecycleState.ROUTED,
        forward_state=LifecycleState.APPROVED,
        comp_state=LifecycleState.CANCELLED,
        forward_has_outbox=False,  # APPROVE forward stays inline (ADR-0011)
        comp_has_outbox=True,
        extras_seed=(("chosen_supplier", "B"),),
    ),
    StepName.SHIP: _StepSpec(
        step=StepName.SHIP,
        pre_state=LifecycleState.APPROVED,
        forward_state=LifecycleState.SHIPPED,
        comp_state=LifecycleState.DISPUTED,
        forward_has_outbox=True,
        comp_has_outbox=True,
        extras_seed=(("reshare_id", "rs-prop-1"),),
    ),
    StepName.RETURN_ITEM: _StepSpec(
        step=StepName.RETURN_ITEM,
        pre_state=LifecycleState.SHIPPED,
        forward_state=LifecycleState.RETURNED,
        comp_state=LifecycleState.DISPUTED,
        forward_has_outbox=True,
        comp_has_outbox=False,
        extras_seed=(("reshare_id", "rs-prop-1"),),
    ),
}

# Steps whose forward leaves the saga in a non-terminal state — those
# are the only ones whose compensator can actually run (the ledger
# blocks transitions out of TERMINAL_STATES).
_COMP_RUNNABLE = [StepName.ROUTE, StepName.APPROVE, StepName.SHIP]
_FORWARD_OUTBOX = [s for s, sp in _SPECS.items() if sp.forward_has_outbox]
_ALL_STEPS = list(_SPECS.keys())


def _build_request() -> IllRequest:
    return IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="t", author="a", isbn="9780000000000"),
        citation=Citation(
            raw="r", parsed_from="openurl", parsed_at=datetime.now(UTC)
        ),
    )


def _seed_to_dict(seed: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {k: v for k, v in seed}


async def _create_saga(session, *, saga_id, request, initial_state) -> None:
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=initial_state,
        )


async def _gate(session, registry, saga_id, step) -> None:
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=step, actor="staff:t")
        await coord.commit_gate(
            saga_id=saga_id, step=step, actor="staff:t", rationale="ok"
        )


async def _forward(
    session, registry, saga_id, request, step, extras, from_state, key, *,
    require_gate: bool = True,
):
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=from_state,
            idempotency_key=key,
            actor="agent:transaction",
            extras=dict(extras),
        )
        return await coord.run_forward(ctx=ctx, step=step, require_gate=require_gate)


async def _comp(session, registry, saga_id, request, step, extras, current_state, key):
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=current_state,
            idempotency_key=key,
            actor="agent:reconciliation",
            extras=dict(extras),
        )
        return await coord.run_compensator(ctx=ctx, step=step)


@pytest.mark.asyncio
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(step=st.sampled_from(_COMP_RUNNABLE))
async def test_compensator_lands_in_documented_state(session, step) -> None:
    """For every runnable (forward, compensator) pair, comp lands in
    its documented ``comp_state``."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))  # type: ignore[arg-type]

    await _create_saga(
        session, saga_id=saga_id, request=request, initial_state=spec.pre_state
    )
    await _gate(session, registry, saga_id, step)
    await _forward(
        session, registry, saga_id, request, step,
        _seed_to_dict(spec.extras_seed),
        spec.pre_state,
        new_idempotency_key(prefix=step.value),
    )

    # APPROVE forward stamps the live reshare_id; comp needs it back.
    comp_extras = _seed_to_dict(spec.extras_seed)
    if step == StepName.APPROVE:
        async with session.begin():
            ledger = SagaLedger(session)
            fwd = await ledger.find_committed_forward(saga_id, step.value)
        assert fwd is not None
        comp_extras["reshare_id"] = fwd.payload["reshare_id"]

    await _comp(
        session, registry, saga_id, request, step,
        comp_extras,
        spec.forward_state,
        new_idempotency_key(prefix=f"comp-{step.value}"),
    )

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == spec.comp_state.value


@pytest.mark.asyncio
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(step=st.sampled_from(_ALL_STEPS))
async def test_compensator_without_committed_forward_raises(session, step) -> None:
    """``run_compensator`` must refuse if no committed forward exists."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))  # type: ignore[arg-type]

    await _create_saga(
        session, saga_id=saga_id, request=request, initial_state=spec.pre_state
    )

    with pytest.raises(CoordinatorError):
        await _comp(
            session, registry, saga_id, request, step,
            _seed_to_dict(spec.extras_seed),
            spec.pre_state,
            new_idempotency_key(prefix=f"comp-{step.value}"),
        )


@pytest.mark.asyncio
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    step=st.sampled_from(_COMP_RUNNABLE),
    replays=st.integers(min_value=2, max_value=4),
)
async def test_compensator_replay_is_idempotent(session, step, replays) -> None:
    """N comp runs with the same idempotency_key → 1 ledger event +
    (at most) 1 outbox row for the comp's intent."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))  # type: ignore[arg-type]

    await _create_saga(
        session, saga_id=saga_id, request=request, initial_state=spec.pre_state
    )
    await _gate(session, registry, saga_id, step)
    await _forward(
        session, registry, saga_id, request, step,
        _seed_to_dict(spec.extras_seed),
        spec.pre_state,
        new_idempotency_key(prefix=step.value),
    )

    comp_extras = _seed_to_dict(spec.extras_seed)
    if step == StepName.APPROVE:
        async with session.begin():
            ledger = SagaLedger(session)
            fwd = await ledger.find_committed_forward(saga_id, step.value)
        assert fwd is not None
        comp_extras["reshare_id"] = fwd.payload["reshare_id"]

    fixed_key = new_idempotency_key(prefix=f"comp-{step.value}-rep")
    forward_outbox_rows_before = 0
    if spec.forward_has_outbox:
        async with session.begin():
            forward_outbox_rows_before = len(
                (
                    await session.execute(
                        select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                    )
                ).scalars().all()
            )

    for i in range(replays):
        # First call sees forward_state; later calls see comp_state.
        current = spec.forward_state if i == 0 else spec.comp_state
        await _comp(
            session, registry, saga_id, request, step,
            comp_extras, current, fixed_key,
        )

    async with session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
        comp_events = [
            e for e in events
            if e.kind == EventKind.COMPENSATOR and e.step == step
        ]
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            ).scalars().all()
        )

    assert len(comp_events) == 1, (
        "replayed compensator must produce exactly one COMPENSATOR ledger event"
    )

    expected_extra_rows = 1 if spec.comp_has_outbox else 0
    assert len(rows) - forward_outbox_rows_before == expected_extra_rows, (
        f"comp replay enqueued {len(rows) - forward_outbox_rows_before} "
        f"row(s); expected {expected_extra_rows}"
    )


@pytest.mark.asyncio
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    step=st.sampled_from(_FORWARD_OUTBOX),
    replays=st.integers(min_value=2, max_value=5),
)
async def test_forward_replay_outbox_count_invariant(
    session, step, replays
) -> None:
    """Replaying a forward with the same idempotency_key must enqueue
    exactly one outbox row."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))  # type: ignore[arg-type]

    await _create_saga(
        session, saga_id=saga_id, request=request, initial_state=spec.pre_state
    )
    await _gate(session, registry, saga_id, step)

    fixed_key = new_idempotency_key(prefix=f"{step.value}-rep")
    for i in range(replays):
        current = spec.pre_state if i == 0 else spec.forward_state
        await _forward(
            session, registry, saga_id, request, step,
            _seed_to_dict(spec.extras_seed),
            current,
            fixed_key,
            require_gate=(i == 0),
        )

    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            ).scalars().all()
        )
    assert len(rows) == 1, (
        "replayed forward must not double-enqueue the same outbox intent"
    )


@pytest.mark.asyncio
async def test_terminal_forward_blocks_compensator(session) -> None:
    """RETURN_ITEM forward → RETURNED is terminal; the ledger must
    refuse the paired compensator."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))  # type: ignore[arg-type]
    spec = _SPECS[StepName.RETURN_ITEM]

    await _create_saga(
        session, saga_id=saga_id, request=request, initial_state=spec.pre_state
    )
    await _gate(session, registry, saga_id, StepName.RETURN_ITEM)
    await _forward(
        session, registry, saga_id, request, StepName.RETURN_ITEM,
        _seed_to_dict(spec.extras_seed),
        spec.pre_state,
        new_idempotency_key(prefix=StepName.RETURN_ITEM.value),
    )

    with pytest.raises(TerminalStateError):
        await _comp(
            session, registry, saga_id, request, StepName.RETURN_ITEM,
            _seed_to_dict(spec.extras_seed),
            spec.forward_state,
            new_idempotency_key(prefix="comp-return"),
        )
