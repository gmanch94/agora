"""Saga coordinator — the small explicit engine over the ledger.

The coordinator decides which step to run next, dispatches to the
registered step function, captures the result into the ledger, and
manages human-approval gates and compensator runs.

Crucially, the coordinator does not autonomously *initiate* forward
steps after the first one — it waits for a staff member to commit a
gate event for the next step. This is the human-in-the-loop default
mandated by ADR-0005.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agora.logging import get_logger
from agora.models.events import NewSagaEvent, SagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.saga.context import SagaContext
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger
from agora.saga.steps import StepRegistry, get_global_registry

log = get_logger(__name__)


class CoordinatorError(Exception):
    """Base for coordinator-side errors."""


class GateRequiredError(CoordinatorError):
    """Raised when a forward step is requested before its gate is committed."""


class Coordinator:
    """Drives a single saga forward by reading the ledger and registry.

    The coordinator is intentionally stateless across calls; everything
    it needs lives in the ledger and the registry. This makes restart
    and replay trivially correct.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        registry: StepRegistry | None = None,
    ):
        self._session = session
        self._registry = registry or get_global_registry()
        self._ledger = SagaLedger(session)

    async def open_gate(
        self,
        *,
        saga_id: UUID,
        step: StepName,
        actor: str,
        rationale: str | None = None,
    ) -> None:
        """Append a pending gate event awaiting human approval."""
        saga = await self._ledger.get_saga(saga_id)
        current = LifecycleState(saga.current_state)
        await self._ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.GATE,
                step=step,
                state_before=current,
                state_after=current,
                actor=actor,
                idempotency_key=new_idempotency_key(prefix=f"gate-{step.value}"),
                outcome=StepOutcome.PENDING,
                rationale=rationale,
            )
        )

    async def commit_gate(
        self,
        *,
        saga_id: UUID,
        step: StepName,
        actor: str,
        rationale: str,
    ) -> None:
        """Record staff approval; clears the gate so forward step can run."""
        saga = await self._ledger.get_saga(saga_id)
        current = LifecycleState(saga.current_state)
        await self._ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.GATE,
                step=step,
                state_before=current,
                state_after=current,
                actor=actor,
                idempotency_key=new_idempotency_key(prefix=f"gate-commit-{step.value}"),
                outcome=StepOutcome.COMMITTED,
                rationale=rationale,
            )
        )

    async def run_forward(
        self,
        *,
        ctx: SagaContext,
        step: StepName,
        require_gate: bool = True,
    ) -> SagaEvent:
        """Execute a forward step.

        If ``require_gate`` is true (default), the most recent ``GATE``
        event for this step must be ``COMMITTED``; otherwise raises
        ``GateRequiredError``.

        Returns the persisted ``SagaEvent`` (with ``seq`` and ``ts``
        populated). On forward failure, a ``FAILED`` event is recorded
        and the original exception re-raised.
        """
        if require_gate and not await self._gate_is_committed(ctx.saga_id, step):
            raise GateRequiredError(
                f"step {step.value} requires committed gate before running"
            )

        defn = self._registry.get(step)
        log.info(
            "saga.forward.start",
            saga_id=str(ctx.saga_id),
            step=step.value,
            idempotency_key=ctx.idempotency_key,
            actor=ctx.actor,
        )

        try:
            result = await defn.forward(ctx)
            outcome = StepOutcome.COMMITTED
            event = NewSagaEvent(
                saga_id=ctx.saga_id,
                kind=EventKind.FORWARD,
                step=step,
                state_before=ctx.current_state,
                state_after=result.state_after,
                actor=ctx.actor,
                idempotency_key=ctx.idempotency_key,
                iso_message_id=result.iso_message_id,
                payload=result.payload,
                outcome=outcome,
                rationale=result.rationale,
            )
            persisted = await self._ledger.append(event)
            log.info(
                "saga.forward.committed",
                saga_id=str(ctx.saga_id),
                step=step.value,
                state_after=result.state_after.value,
            )
            assert persisted is not None  # ledger.append never returns None in practice
            return persisted
        except Exception as exc:
            failed = NewSagaEvent(
                saga_id=ctx.saga_id,
                kind=EventKind.FORWARD,
                step=step,
                state_before=ctx.current_state,
                state_after=ctx.current_state,
                actor=ctx.actor,
                idempotency_key=ctx.idempotency_key,
                payload={"error": str(exc)},
                outcome=StepOutcome.FAILED,
                rationale=f"forward failed: {exc!s}",
            )
            await self._ledger.append(failed)
            log.error(
                "saga.forward.failed",
                saga_id=str(ctx.saga_id),
                step=step.value,
                error=str(exc),
            )
            raise

    async def run_compensator(
        self,
        *,
        ctx: SagaContext,
        step: StepName,
    ) -> SagaEvent:
        """Run the compensator paired with the most recent committed forward.

        Returns the persisted ``SagaEvent`` (with ``seq`` and ``ts``
        populated).
        """
        forward_event = await self._ledger.find_committed_forward(ctx.saga_id, step.value)
        if forward_event is None:
            raise CoordinatorError(
                f"no committed forward for step {step.value} on saga {ctx.saga_id}"
            )

        defn = self._registry.get(step)
        if defn.compensator is None:
            raise CoordinatorError(f"step {step.value} has no compensator registered")

        log.info(
            "saga.compensator.start",
            saga_id=str(ctx.saga_id),
            step=step.value,
            forward_seq=forward_event.seq,
        )

        result = await defn.compensator(ctx, forward_event.payload)
        event = NewSagaEvent(
            saga_id=ctx.saga_id,
            kind=EventKind.COMPENSATOR,
            step=step,
            state_before=ctx.current_state,
            state_after=result.state_after,
            actor=ctx.actor,
            idempotency_key=ctx.idempotency_key,
            iso_message_id=result.iso_message_id,
            payload=result.payload,
            outcome=StepOutcome.COMMITTED,
            rationale=result.rationale,
        )
        persisted = await self._ledger.append(event)
        log.info(
            "saga.compensator.committed",
            saga_id=str(ctx.saga_id),
            step=step.value,
            state_after=result.state_after.value,
        )
        assert persisted is not None
        return persisted

    async def record_observation(
        self,
        *,
        saga_id: UUID,
        step: StepName,
        actor: str,
        payload: dict[str, Any],
        rationale: str | None = None,
    ) -> None:
        """Append a non-state-changing observation (e.g. agent rationale)."""
        saga = await self._ledger.get_saga(saga_id)
        current = LifecycleState(saga.current_state)
        await self._ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=step,
                state_before=current,
                state_after=current,
                actor=actor,
                idempotency_key=new_idempotency_key(prefix=f"obs-{step.value}"),
                payload=payload,
                outcome=StepOutcome.COMMITTED,
                rationale=rationale,
            )
        )

    async def _gate_is_committed(self, saga_id: UUID, step: StepName) -> bool:
        """Return True if the most-recent gate for ``step`` is committed."""
        events = await self._ledger.events_for(saga_id)
        latest_gate = None
        for ev in events:
            if ev.kind == EventKind.GATE and ev.step == step:
                latest_gate = ev
        return latest_gate is not None and latest_gate.outcome == StepOutcome.COMMITTED
