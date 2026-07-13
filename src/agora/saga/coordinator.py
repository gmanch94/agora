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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agora.logging import get_logger
from agora.models.events import NewSagaEvent, SagaEvent
from agora.models.lifecycle import (
    COMPENSATOR_ALLOWED_STATES,
    FORWARD_STEP_ALLOWED_STATES,
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.saga.context import SagaContext
from agora.saga.idempotency import new_idempotency_key, outbox_enqueue
from agora.saga.ledger import IdempotencyConflictError, SagaLedger
from agora.saga.steps import StepRegistry, StepResult, get_global_registry

log = get_logger(__name__)


class CoordinatorError(Exception):
    """Base for coordinator-side errors."""


class GateRequiredError(CoordinatorError):
    """Raised when a forward step is requested before its gate is committed."""


class IllegalTransitionError(CoordinatorError):
    """Raised when a step is requested from a state it cannot legally run in.

    Carries ``step`` and ``current_state`` so callers (the API layer)
    can render a precise conflict message. Guards both directions:

    * forward steps — e.g. a second ``POST /approve`` while the saga is
      already APPROVING/APPROVED would create a second supplier
      request; approving ``step=receive`` at APPROVED would skip SHIP.
    * compensators — e.g. ``compensate step=submit`` at SHIPPED would
      terminal-cancel the saga with zero outbox intents, stranding the
      supplier-side loan.
    """

    def __init__(
        self,
        *,
        step: StepName,
        current_state: LifecycleState,
        kind: str,
        allowed: frozenset[LifecycleState] | None = None,
    ) -> None:
        self.step = step
        self.current_state = current_state
        self.kind = kind
        self.allowed = allowed
        allowed_txt = (
            ", ".join(sorted(s.value for s in allowed)) if allowed else "none"
        )
        super().__init__(
            f"illegal {kind} transition: step {step.value!r} cannot run "
            f"while saga is in state {current_state.value!r} "
            f"(allowed: {allowed_txt})"
        )


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
        (under the derived key ``{idempotency_key}:failed`` so a retry
        with the original key stays possible) and the original
        exception re-raised.

        State-machine enforcement: the saga's *persisted*
        ``current_state`` must be in ``FORWARD_STEP_ALLOWED_STATES``
        for ``step``; otherwise raises ``IllegalTransitionError``.
        Steps absent from the table are fail-closed. A benign replay
        (same idempotency key as an already-committed forward) short-
        circuits before the state/gate checks and returns the existing
        event — replay-safety is unchanged.
        """
        existing = await self._replay_short_circuit(
            ctx=ctx, step=step, kind=EventKind.FORWARD
        )
        if existing is not None:
            return existing

        saga = await self._ledger.get_saga(ctx.saga_id)
        current = LifecycleState(saga.current_state)
        allowed = FORWARD_STEP_ALLOWED_STATES.get(step)
        if allowed is None or current not in allowed:
            raise IllegalTransitionError(
                step=step, current_state=current, kind="forward", allowed=allowed
            )

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
            # Enqueue outbox intents in the same transaction. On
            # idempotency-key replay ``ledger.append`` returns the
            # already-persisted event, and ``_enqueue_outbox`` swallows
            # the per-intent ``IntegrityError`` from the outbox UNIQUE
            # constraint inside its own savepoint — so re-running the
            # same step is safe and does not double-enqueue. See
            # ADR-0011.
            await self._enqueue_outbox(ctx.saga_id, result)
            log.info(
                "saga.forward.committed",
                saga_id=str(ctx.saga_id),
                step=step.value,
                state_after=result.state_after.value,
                outbox_intents=len(result.outbox),
            )
            return persisted
        except Exception as exc:
            # The FAILED event is recorded under a *derived* key so the
            # original key stays free for a successful retry. Without
            # the suffix, a retry's COMMITTED append would collide with
            # the persisted FAILED row and (with ``outcome`` now part
            # of the ledger's identity check) raise
            # ``IdempotencyConflictError`` — and before that check
            # existed, the FAILED row masqueraded as committed and
            # still enqueued outbox intents.
            failed = NewSagaEvent(
                saga_id=ctx.saga_id,
                kind=EventKind.FORWARD,
                step=step,
                state_before=ctx.current_state,
                state_after=ctx.current_state,
                actor=ctx.actor,
                idempotency_key=f"{ctx.idempotency_key}:failed",
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

        State-machine enforcement: the saga's *persisted*
        ``current_state`` must be in ``COMPENSATOR_ALLOWED_STATES`` for
        ``step`` — compensating a historically-committed step from a
        much later state (e.g. ``compensate step=submit`` at SHIPPED)
        raises ``IllegalTransitionError`` instead of silently
        terminal-cancelling the saga with zero outbox intents. A benign
        replay (same idempotency key as an already-recorded compensator
        — the API uses the deterministic ``comp-{step}-{saga_id}`` key)
        short-circuits before the state check and returns the existing
        event.
        """
        existing = await self._replay_short_circuit(
            ctx=ctx, step=step, kind=EventKind.COMPENSATOR
        )
        if existing is not None:
            return existing

        saga = await self._ledger.get_saga(ctx.saga_id)
        current = LifecycleState(saga.current_state)
        allowed = COMPENSATOR_ALLOWED_STATES.get(step)
        if allowed is None or current not in allowed:
            raise IllegalTransitionError(
                step=step,
                current_state=current,
                kind="compensator",
                allowed=allowed,
            )

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
        # Enqueue outbox intents atomically with the compensator event.
        # Replay-safe; see run_forward for the rationale.
        await self._enqueue_outbox(ctx.saga_id, result)
        log.info(
            "saga.compensator.committed",
            saga_id=str(ctx.saga_id),
            step=step.value,
            state_after=result.state_after.value,
            outbox_intents=len(result.outbox),
        )
        return persisted

    async def _enqueue_outbox(self, saga_id: UUID, result: StepResult) -> None:
        """Write each ``OutboxIntent`` from ``result`` as an outbox row.

        Runs inside the caller's session/transaction so the rows commit
        atomically with the ledger event that produced them. The outbox
        worker (``saga/outbox.py``) drains them onto the wire later.

        Each enqueue runs inside a savepoint and a UNIQUE-constraint
        collision on ``idempotency_key`` is swallowed as a benign
        replay (the row was written on the original pass; re-running
        the same step with the same key must not double-enqueue and
        must not roll back the outer transaction). See ADR-0011.
        """
        for intent in result.outbox:
            try:
                async with self._session.begin_nested():
                    await outbox_enqueue(
                        self._session,
                        saga_id=saga_id,
                        target=intent.target,
                        idempotency_key=intent.idempotency_key,
                        payload=intent.payload,
                    )
            except IntegrityError:
                log.info(
                    "saga.outbox.replay_skipped",
                    saga_id=str(saga_id),
                    target=intent.target,
                    idempotency_key=intent.idempotency_key,
                )

    async def record_observation(
        self,
        *,
        saga_id: UUID,
        step: StepName,
        actor: str,
        payload: dict[str, Any],
        rationale: str | None = None,
        idempotency_key: str | None = None,
    ) -> SagaEvent:
        """Append a non-state-changing observation (e.g. agent rationale).

        If ``idempotency_key`` is omitted, a fresh ULID is generated and
        the observation is always appended. Pass a deterministic key
        (e.g. the overdue scanner uses ``f"overdue-{saga_id}"``) to make
        repeat appends idempotent — the second insert hits the saga
        ledger's UNIQUE constraint and ``ledger.append`` returns the
        existing row instead of writing a duplicate. Callers that need
        to distinguish "newly written" from "already there" can compare
        the returned event's ``idempotency_key`` (always the one passed
        in) against a payload field they control (e.g. ``observed_at``
        in the overdue scanner).
        """
        saga = await self._ledger.get_saga(saga_id)
        current = LifecycleState(saga.current_state)
        return await self._ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=step,
                state_before=current,
                state_after=current,
                actor=actor,
                idempotency_key=idempotency_key
                or new_idempotency_key(prefix=f"obs-{step.value}"),
                payload=payload,
                outcome=StepOutcome.COMMITTED,
                rationale=rationale,
            )
        )

    async def _replay_short_circuit(
        self,
        *,
        ctx: SagaContext,
        step: StepName,
        kind: EventKind,
    ) -> SagaEvent | None:
        """Return the existing event for ``ctx.idempotency_key``, if any.

        Benign replay contract: re-invoking a forward/compensator with
        the key of an already-persisted event returns that event
        without re-running the step function, re-enqueuing outbox
        intents, or tripping the state-transition guard (the saga has
        already advanced). A key that points at a *different* event
        (other saga / step / kind, or a non-committed outcome) raises
        ``IdempotencyConflictError`` — that is key reuse, not replay.
        """
        existing = await self._ledger.find_by_idempotency(ctx.idempotency_key)
        if existing is None:
            return None
        if (
            existing.saga_id != ctx.saga_id
            or existing.step != step
            or existing.kind != kind
            or existing.outcome != StepOutcome.COMMITTED
        ):
            raise IdempotencyConflictError(
                f"idempotency key {ctx.idempotency_key!r} already used by a "
                f"different event: existing (saga={existing.saga_id}, "
                f"step={existing.step.value}, kind={existing.kind.value}, "
                f"outcome={existing.outcome.value}) vs requested "
                f"(saga={ctx.saga_id}, step={step.value}, kind={kind.value})"
            )
        log.info(
            "saga.replay_short_circuit",
            saga_id=str(ctx.saga_id),
            step=step.value,
            kind=kind.value,
            idempotency_key=ctx.idempotency_key,
            seq=existing.seq,
        )
        return existing

    async def _gate_is_committed(self, saga_id: UUID, step: StepName) -> bool:
        """Return True if the most-recent gate for ``step`` is committed
        AND not yet consumed.

        Gates are single-use: once any FORWARD event for the step exists
        with ``seq`` greater than the gate's, the gate is spent —
        re-running the forward requires a fresh staff-committed gate.
        (A FAILED forward also consumes its gate: retrying after a
        failure is a new decision staff must re-approve; default-deny
        per ADR-0005.)
        """
        events = await self._ledger.events_for(saga_id)
        latest_gate = None
        for ev in events:
            if ev.kind == EventKind.GATE and ev.step == step:
                latest_gate = ev
        if latest_gate is None or latest_gate.outcome != StepOutcome.COMMITTED:
            return False
        for ev in events:
            if (
                ev.kind == EventKind.FORWARD
                and ev.step == step
                and ev.seq > latest_gate.seq
            ):
                return False  # gate already consumed by this forward
        return True
