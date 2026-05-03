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
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agora.agents.transaction import TransactionAgent
from agora.clients.reshare import MockReShareClient
from agora.models.events import NewSagaEvent, SagaEvent
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
from agora.saga.steps import StepRegistry

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
async def test_replay_idempotency_under_concurrency(
    session: AsyncSession, n: int, replay_each: int
) -> None:
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
    # Number of outbox intents emitted by a single forward run. Default 1
    # for legacy single-target steps (APPROVE → reshare); SHIP and RETURN
    # emit 2 (reshare + ncip per the NCIP-flow integration).
    forward_outbox_count: int = 1


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
        # ADR-0012: forward lands in APPROVING (intermediate). The
        # supplier ack — projected by the outbox worker as an
        # OBSERVATION — is what advances the saga to APPROVED. These
        # property tests don't drive the worker, so the comp is
        # exercised against a saga in APPROVING with reshare_id
        # supplied via ``extras_seed`` (mirroring what
        # ``_derive_extras`` would assemble in the API).
        forward_state=LifecycleState.APPROVING,
        comp_state=LifecycleState.CANCELLED,
        forward_has_outbox=True,
        comp_has_outbox=True,
        extras_seed=(
            ("chosen_supplier", "B"),
            ("reshare_id", "rs-prop-1"),
        ),
    ),
    StepName.SHIP: _StepSpec(
        step=StepName.SHIP,
        pre_state=LifecycleState.APPROVED,
        forward_state=LifecycleState.SHIPPED,
        comp_state=LifecycleState.DISPUTED,
        forward_has_outbox=True,
        comp_has_outbox=True,
        extras_seed=(("reshare_id", "rs-prop-1"),),
        forward_outbox_count=2,  # reshare confirm_shipment + ncip check_out
    ),
    StepName.RECEIVE: _StepSpec(
        step=StepName.RECEIVE,
        pre_state=LifecycleState.SHIPPED,
        forward_state=LifecycleState.RECEIVED,
        comp_state=LifecycleState.DISPUTED,
        forward_has_outbox=False,  # pure ledger write — borrower confirm
        comp_has_outbox=False,
        extras_seed=(("reshare_id", "rs-prop-1"),),
    ),
    StepName.RETURN_ITEM: _StepSpec(
        step=StepName.RETURN_ITEM,
        # RECEIVE now sits between SHIP and RETURN; RETURN's pre-state
        # is RECEIVED (lifecycle order). The property test creates the
        # saga directly at this state so the chaining is implicit.
        pre_state=LifecycleState.RECEIVED,
        forward_state=LifecycleState.RETURNED,
        comp_state=LifecycleState.DISPUTED,
        forward_has_outbox=True,
        comp_has_outbox=False,
        extras_seed=(("reshare_id", "rs-prop-1"),),
        forward_outbox_count=2,  # reshare confirm_return + ncip check_in
    ),
}

# Steps whose forward leaves the saga in a non-terminal state — those
# are the only ones whose compensator can actually run (the ledger
# blocks transitions out of TERMINAL_STATES).
_COMP_RUNNABLE = [StepName.ROUTE, StepName.APPROVE, StepName.SHIP, StepName.RECEIVE]
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


async def _create_saga(
    session: AsyncSession,
    *,
    saga_id: UUID,
    request: IllRequest,
    initial_state: LifecycleState,
) -> None:
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=initial_state,
        )


async def _gate(
    session: AsyncSession,
    registry: StepRegistry,
    saga_id: UUID,
    step: StepName,
) -> None:
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=step, actor="staff:t")
        await coord.commit_gate(
            saga_id=saga_id, step=step, actor="staff:t", rationale="ok"
        )


async def _forward(
    session: AsyncSession,
    registry: StepRegistry,
    saga_id: UUID,
    request: IllRequest,
    step: StepName,
    extras: dict[str, Any],
    from_state: LifecycleState,
    key: str,
    *,
    require_gate: bool = True,
) -> SagaEvent | None:
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


async def _comp(
    session: AsyncSession,
    registry: StepRegistry,
    saga_id: UUID,
    request: IllRequest,
    step: StepName,
    extras: dict[str, Any],
    current_state: LifecycleState,
    key: str,
) -> SagaEvent | None:
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
async def test_compensator_lands_in_documented_state(session: AsyncSession, step: StepName) -> None:
    """For every runnable (forward, compensator) pair, comp lands in
    its documented ``comp_state``."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))

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

    # ``extras_seed`` already contains everything the comp needs —
    # ``reshare_id`` for APPROVE/SHIP/RETURN_ITEM, ``chosen_supplier``
    # for ROUTE — so a single pass through ``_seed_to_dict`` is
    # sufficient. Pre-PR-B this branch had a special-case for APPROVE
    # that pulled ``reshare_id`` off the live forward payload (the
    # inline-supplier-call era); after ADR-0012 reshare_id rides on
    # an OBSERVATION the worker writes asynchronously, so the test
    # supplies it via the seed instead.
    comp_extras = _seed_to_dict(spec.extras_seed)

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
async def test_compensator_without_committed_forward_raises(session: AsyncSession, step: StepName) -> None:
    """``run_compensator`` must refuse if no committed forward exists."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))

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
async def test_compensator_replay_is_idempotent(
    session: AsyncSession, step: StepName, replays: int
) -> None:
    """N comp runs with the same idempotency_key → 1 ledger event +
    (at most) 1 outbox row for the comp's intent."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))

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

    # See note in ``test_compensator_lands_in_documented_state``: the
    # APPROVE special-case is gone; ``extras_seed`` carries the
    # reshare_id post-ADR-0012.
    comp_extras = _seed_to_dict(spec.extras_seed)

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
    session: AsyncSession, step: StepName, replays: int
) -> None:
    """Replaying a forward with the same idempotency_key must enqueue
    exactly one outbox row."""
    spec = _SPECS[step]
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))

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
    assert len(rows) == spec.forward_outbox_count, (
        "replayed forward must not double-enqueue any outbox intent; "
        f"expected {spec.forward_outbox_count} row(s), got {len(rows)}"
    )


@pytest.mark.asyncio
async def test_terminal_forward_blocks_compensator(session: AsyncSession) -> None:
    """RETURN_ITEM forward → RETURNED is terminal; the ledger must
    refuse the paired compensator."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
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
