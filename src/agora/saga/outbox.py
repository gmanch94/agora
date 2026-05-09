"""Outbox worker — drains pending outbound dispatches.

The outbox table buffers outbound messages so a saga step can commit
its ledger event atomically and let a separate worker handle
delivery. This keeps the saga's append-only log clean even when the
remote target (ReShare, NCIP, …) is briefly unreachable.

**Payload convention.** A handler is a coroutine
``(payload: dict, idempotency_key: str) -> Any`` that raises on
failure. The handler's return value is forwarded to the optional
per-target ``on_success`` projection callback. By convention the
payload for ``target="reshare"`` is::

    {"action": "send_request" | "cancel_request" | "confirm_shipment"
              | "confirm_return" | "recall_request",
     "args": { ... method kwargs ... }}

``make_reshare_handler(client)`` returns a Handler that dispatches
on ``action``. Other targets get their own builders (e.g. NCIP).

**Projection callbacks (``on_success``).** Some wire calls return
data the saga ledger needs back — the canonical case is
``send_request`` returning ``reshare_id`` (ADR-0012). A per-target
``on_success`` callback receives ``(session, row_id, saga_id,
payload, idempotency_key, result)`` and is invoked **inside the
same session** that runs ``outbox_mark_delivered``. The two writes
commit atomically: there is no window where the wire said "yes"
but the projection is missing, or the projection landed but the row
stays pending. Targets that don't need projection simply omit the
callback.

**Transaction discipline.**
- One session per row. A handler failure or DB write for one row must
  never roll back ``delivered_at`` on its neighbours.
- The handler call itself is *not* inside a savepoint. After the
  handler returns successfully we open one session in which the
  optional ``on_success`` projection AND ``outbox_mark_delivered``
  both run, then commit once. On handler exception we open a fresh
  session to record the failure (the session that read the row may
  be in a bad state if the handler did something weird with the
  connection — fresh session is cheap insurance).

**Multi-worker.** Safe on Postgres via :func:`outbox_claim` which uses
``SELECT ... FOR UPDATE SKIP LOCKED`` to acquire disjoint row sets.
Each pass: claim N rows (flip ``pending → in_flight``, stamp
``claimed_at``, release row locks at commit), then dispatch each row
in its own session. Concurrent workers see different rows on claim and
ignore each other's ``in_flight`` entries on the next pass. The lease
(``claim_lease_secs``, default 600s) sweeps abandoned ``in_flight``
rows back to ``pending`` if a worker crashes mid-dispatch. SQLite
serializes writers naturally so the same code path works in tests
without the ``FOR UPDATE`` hint.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agora.clients.ncip import NcipClient
from agora.clients.reshare import ReShareClient, ReShareSendResult
from agora.logging import get_logger
from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.saga.idempotency import (
    outbox_claim,
    outbox_claim_still_ours,
    outbox_mark_delivered,
    outbox_mark_failed,
    outbox_release_claim,
)
from agora.saga.ledger import SagaLedger

log = get_logger(__name__)


# mod-rs returns a UUID for the patron-request id in production
# (sandbox probe 2026-05-06: ``id`` field on the JSON response). The
# ``MockReShareClient`` returns ``rs-000001``-style synthetic ids for
# tests/dev. The pattern accepts both shapes — alphanumerics + dash +
# underscore, bounded length — to reject anything that could smuggle a
# control character / template fragment / path-traversal segment into
# downstream URL components or staff-console templates. The check is a
# format guard, not a security boundary by itself — Jinja autoescape
# and httpx URL-encoding remain the load-bearing defenses.
_RESHARE_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9_-]{1,128}$"
)


Handler = Callable[[dict[str, Any], str], Awaitable[Any]]
"""Outbox dispatch callable.

Args are ``(payload, idempotency_key)``. May return any value (the
underlying client's response object); the worker forwards it to a
per-target ``on_success`` projection callback if one is registered.
Raise any exception to mark the row failed; the worker translates the
exception message into ``last_error`` and either re-schedules with
exponential backoff or marks ``dead_letter`` when ``attempts`` hits
``max_attempts``.
"""


OnSuccess = Callable[
    [AsyncSession, int, UUID, dict[str, Any], str, Any],
    Awaitable[None],
]
"""Per-target projection callback invoked after a successful handler.

Signature: ``(session, row_id, saga_id, payload, idempotency_key, result)``.

Runs **inside the same session** as ``outbox_mark_delivered`` so the
projection write and the delivered flag commit atomically. Implementations
should be careful to:

- be idempotent under replay (use a deterministic ledger
  ``idempotency_key``, e.g. ``f"approve-ack-{row_id}"``);
- raise on unrecoverable projection errors so ``mark_delivered`` does
  not commit and the row remains pending for retry;
- gate writes by inspecting ``payload['action']`` — a ReShare handler
  may dispatch many actions, only some of which need projection.
"""


@dataclass(slots=True)
class DrainStats:
    """Counts from a single drain pass — handy for tests and metrics."""

    delivered: int = 0
    failed: int = 0
    dead_letter: int = 0
    skipped_no_handler: int = 0
    claim_lost: int = 0

    @property
    def total(self) -> int:
        return (
            self.delivered
            + self.failed
            + self.dead_letter
            + self.skipped_no_handler
            + self.claim_lost
        )


class OutboxWorker:
    """Polls the outbox and dispatches pending rows via target handlers.

    ``handlers`` is a ``{target: Handler}`` map. A row whose ``target``
    is unknown is left ``pending`` and counted as ``skipped_no_handler``
    rather than failed — adding a handler later picks the row up.

    ``on_success`` is an optional ``{target: OnSuccess}`` map of
    projection callbacks. After a handler returns successfully, the
    matching callback (if any) is invoked **inside the same session**
    as ``outbox_mark_delivered`` so the projection and the delivered
    flag commit atomically. A target that does not appear in
    ``on_success`` simply skips the projection step.

    Backoff is exponential on attempts: ``base_backoff_secs * 2**attempts``.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[Any],
        handlers: dict[str, Handler],
        *,
        on_success: dict[str, OnSuccess] | None = None,
        max_attempts: int = 10,
        base_backoff_secs: int = 60,
        claim_lease_secs: int = 600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._handlers = handlers
        self._on_success = on_success or {}
        self._max_attempts = max_attempts
        self._base_backoff_secs = base_backoff_secs
        # Lease for ``in_flight`` rows; orphan recovery resets stale
        # claims back to ``pending`` after this many seconds. Must
        # exceed the longest plausible handler runtime — too short and
        # a slow handler races a peer-reclaimed row (handler-level
        # idempotency keeps it correct, but logs get noisy).
        self._claim_lease_secs = claim_lease_secs

    async def drain_once(self, *, limit: int = 50) -> DrainStats:
        """Drain up to ``limit`` ready rows. Returns per-row outcomes.

        Multi-worker safe: :func:`outbox_claim` flips claimed rows from
        ``pending`` to ``in_flight`` inside a single transaction (using
        ``FOR UPDATE SKIP LOCKED`` on Postgres). Concurrent workers see
        disjoint row sets and won't double-dispatch.
        """
        stats = DrainStats()

        # Step 1: claim ready rows in one short transaction, then close
        # the session before we start dispatching. Keeping the read
        # session open while making slow HTTP calls to ReShare would
        # hold a DB connection for the entire dispatch window. The
        # claim transaction is brief: a sweep + a SELECT FOR UPDATE +
        # a same-row UPDATE + commit.
        async with self._sessionmaker() as read_session, read_session.begin():
            rows = await outbox_claim(
                read_session,
                limit=limit,
                lease_secs=self._claim_lease_secs,
            )
            # Snapshot the fields we need so we don't depend on the
            # ORM session staying alive past this block. ``saga_id`` is
            # included so projection callbacks can locate the saga
            # without a second read. ``claimed_at`` is the lock-token
            # we hand to ``outbox_claim_still_ours`` after the handler
            # returns: if a peer's orphan-recovery sweep reclaimed the
            # row mid-handler, ``claimed_at`` will have advanced and we
            # know to drop the result rather than double-write the
            # projection (audit 2026-05-09 #12).
            snapshots = [
                (
                    r.id,
                    r.saga_id,
                    r.target,
                    r.idempotency_key,
                    dict(r.payload),
                    r.attempts,
                    r.claimed_at,
                )
                for r in rows
            ]

        for row_id, saga_id, target, idem_key, payload, attempts, claim_ts in snapshots:
            handler = self._handlers.get(target)
            if handler is None:
                log.warning(
                    "outbox.no_handler",
                    row_id=row_id,
                    target=target,
                    idempotency_key=idem_key,
                )
                # Release the claim so the row stays ``pending`` and a
                # later drain (after a handler is registered) can pick
                # it up. Without this, the row would sit ``in_flight``
                # until the orphan-recovery lease expires.
                async with self._sessionmaker() as release_session:
                    await outbox_release_claim(release_session, row_id)
                    await release_session.commit()
                stats.skipped_no_handler += 1
                continue

            try:
                result = await handler(payload, idem_key)
            except Exception as exc:
                # Fresh session for the failure write so a poisoned
                # connection from the handler can't infect the
                # delivered/failed bookkeeping.
                async with self._sessionmaker() as fail_session:
                    new_attempts = attempts + 1
                    backoff = self._base_backoff_secs * (2**attempts)
                    await outbox_mark_failed(
                        fail_session,
                        row_id,
                        error=str(exc),
                        requeue_after_secs=backoff,
                        max_attempts=self._max_attempts,
                    )
                    await fail_session.commit()
                if new_attempts >= self._max_attempts:
                    stats.dead_letter += 1
                    log.error(
                        "outbox.dead_letter",
                        row_id=row_id,
                        target=target,
                        attempts=new_attempts,
                        error=str(exc),
                    )
                else:
                    stats.failed += 1
                    log.warning(
                        "outbox.retry_scheduled",
                        row_id=row_id,
                        target=target,
                        attempts=new_attempts,
                        backoff_secs=backoff,
                        error=str(exc),
                    )
                continue

            # Projection + mark_delivered in one session, one commit.
            # If the projection raises, ``mark_delivered`` does not
            # land, the row stays ``pending``, and the next drain pass
            # retries. Handler-level idempotency (the supplier's
            # ``Idempotency-Key`` honour, or our deterministic ledger
            # key for the projection) makes this safe.
            on_success = self._on_success.get(target)
            try:
                async with self._sessionmaker() as ok_session:
                    # Lease-race guard: between the handler-start and
                    # here, a peer's orphan-recovery sweep may have
                    # decided we're stale and re-claimed the row. The
                    # supplier call itself already went through (and is
                    # racing with the peer's call) — what we control is
                    # whether *we* write the projection. Skip mark/
                    # projection on lost ownership; let the peer win.
                    # ``claim_ts`` is None only on the synthetic edge
                    # case where outbox_claim returned a row whose
                    # claimed_at the in-memory ORM didn't populate
                    # (shouldn't happen in practice — flush stamps it).
                    # Treat None as "we never claimed, skip safely."
                    if claim_ts is None or not await outbox_claim_still_ours(
                        ok_session, row_id, claim_ts
                    ):
                        log.warning(
                            "outbox.claim_lost",
                            row_id=row_id,
                            target=target,
                            idempotency_key=idem_key,
                        )
                        stats.claim_lost += 1
                        continue
                    if on_success is not None:
                        await on_success(
                            ok_session,
                            row_id,
                            saga_id,
                            payload,
                            idem_key,
                            result,
                        )
                    await outbox_mark_delivered(ok_session, row_id)
                    await ok_session.commit()
            except Exception as exc:
                # Projection write failed. Treat exactly like a handler
                # failure: increment attempts, schedule a backoff, leave
                # the row pending. The supplier call already succeeded;
                # next drain will retry it (idempotent at the wire) and
                # re-attempt the projection.
                async with self._sessionmaker() as fail_session:
                    new_attempts = attempts + 1
                    backoff = self._base_backoff_secs * (2**attempts)
                    await outbox_mark_failed(
                        fail_session,
                        row_id,
                        error=f"projection failed: {exc!s}",
                        requeue_after_secs=backoff,
                        max_attempts=self._max_attempts,
                    )
                    await fail_session.commit()
                if new_attempts >= self._max_attempts:
                    stats.dead_letter += 1
                    log.error(
                        "outbox.projection.dead_letter",
                        row_id=row_id,
                        target=target,
                        attempts=new_attempts,
                        error=str(exc),
                    )
                else:
                    stats.failed += 1
                    log.warning(
                        "outbox.projection.retry_scheduled",
                        row_id=row_id,
                        target=target,
                        attempts=new_attempts,
                        backoff_secs=backoff,
                        error=str(exc),
                    )
                continue

            stats.delivered += 1
            log.info(
                "outbox.delivered",
                row_id=row_id,
                target=target,
                idempotency_key=idem_key,
                projected=on_success is not None,
            )

        return stats

    async def drain_until_empty(
        self, *, limit: int = 50, max_iterations: int = 100
    ) -> DrainStats:
        """Drain in a loop until no more ready rows. Bounded for safety.

        Useful for tests and one-shot CLI invocations. Aggregates stats
        across iterations.
        """
        agg = DrainStats()
        for _ in range(max_iterations):
            pass_stats = await self.drain_once(limit=limit)
            agg.delivered += pass_stats.delivered
            agg.failed += pass_stats.failed
            agg.dead_letter += pass_stats.dead_letter
            agg.skipped_no_handler += pass_stats.skipped_no_handler
            agg.claim_lost += pass_stats.claim_lost
            if pass_stats.total == 0:
                break
        return agg

    async def run_forever(self, *, poll_interval: float = 1.0) -> None:
        """Production loop: poll, drain, sleep, repeat. Cancellation-aware.

        Caller is expected to wrap this in ``asyncio.create_task`` and
        ``task.cancel()`` on shutdown. We catch ``CancelledError`` to
        log a clean exit message and re-raise.
        """
        log.info("outbox.worker.start", poll_interval=poll_interval)
        try:
            while True:
                try:
                    await self.drain_once()
                except Exception as exc:
                    # Don't let a worker-level bug kill the loop; log
                    # and back off briefly.
                    log.exception("outbox.worker.unexpected_error", error=str(exc))
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            log.info("outbox.worker.cancelled")
            raise


# ---------------------------------------------------------------------
# Target-specific handler builders
# ---------------------------------------------------------------------


# Allow-listed action names per target. Any payload['action'] outside
# this set is rejected hard, even though the row only lands here via a
# saga step we control — defense in depth against a compromised path
# that lets an attacker craft an outbox row, and against a future
# refactor that adds private helpers reachable via ``getattr`` (audit
# 2026-05-09 finding #4 / #28).
_RESHARE_ACTIONS: frozenset[str] = frozenset(
    {
        "send_request",
        "cancel_request",
        "confirm_shipment",
        "confirm_return",
        "recall_request",
        "renew_request",
    }
)
_NCIP_ACTIONS: frozenset[str] = frozenset({"check_out", "check_in"})


def make_reshare_handler(client: ReShareClient) -> Handler:
    """Build a Handler that dispatches ``payload['action']`` on ``client``.

    Expected payload shape::

        {"action": "send_request" | "cancel_request" | "confirm_shipment"
                  | "confirm_return" | "recall_request" | "renew_request",
         "args": { ...method kwargs (excluding idempotency_key)... }}

    The ``idempotency_key`` is sourced from the outbox row, not the
    payload — that's what makes the dispatch replay-safe even if the
    worker crashes after the remote call but before
    ``mark_delivered`` commits. The client method's return value
    (typically a :class:`ReShareSendResult`) is forwarded back to the
    worker, which passes it to the per-target ``on_success``
    projection callback.

    Only actions in :data:`_RESHARE_ACTIONS` are accepted. ``getattr``
    against an unrestricted action name would let a crafted payload
    reach private helpers (``_perform_action``, ``aclose``, …) — the
    allow-list closes that door even if the outbox-write path ever
    leaks through a less-trusted code path.
    """

    async def handler(payload: dict[str, Any], idempotency_key: str) -> Any:
        action = payload.get("action")
        args = payload.get("args", {})
        if not isinstance(action, str):
            raise ValueError(f"reshare outbox payload missing 'action': {payload!r}")
        if not isinstance(args, dict):
            raise ValueError(f"reshare outbox payload 'args' must be dict: {payload!r}")
        if action not in _RESHARE_ACTIONS:
            raise ValueError(
                f"reshare action {action!r} not in allow-list "
                f"{sorted(_RESHARE_ACTIONS)}"
            )

        method = getattr(client, action, None)
        if method is None or not callable(method):
            raise ValueError(f"reshare client has no action {action!r}")

        return await method(idempotency_key=idempotency_key, **args)

    return handler


def make_reshare_on_success() -> OnSuccess:
    """Build the projection callback for ``target='reshare'`` (ADR-0012).

    Today the only ReShare action whose result the saga ledger needs
    back is ``send_request`` — its :class:`ReShareSendResult` carries
    the supplier-assigned ``reshare_id`` that downstream SHIP/RETURN
    steps must reference.

    On a successful ``send_request`` dispatch this projection appends
    an OBSERVATION event for ``StepName.APPROVE`` carrying
    ``reshare_id``, ``supplier_symbol``, and the ISO 18626 ``state``.
    The event's ``state_after`` advances the saga from ``APPROVING``
    to ``APPROVED`` only when ``current_state == APPROVING`` —
    otherwise (saga compensated to ``CANCELLED`` while the worker
    was still mid-flight, or any other unexpected state) the
    observation is recorded **without** a state change so the audit
    trail keeps the supplier's response without trampling a
    deliberate operator action.

    Replay safety: the OBSERVATION is keyed
    ``f"approve-ack-{row_id}"``. A second invocation with the same
    row hits the saga-event UNIQUE constraint and
    :meth:`SagaLedger.append` returns the prior row instead of
    duplicating it.

    Other actions (``cancel_request``, ``confirm_shipment`` …) carry
    no data the ledger consumes, so this projection is a no-op for
    them. Future ADRs may extend the action-handling table.
    """

    async def on_success(
        session: AsyncSession,
        row_id: int,
        saga_id: UUID,
        payload: dict[str, Any],
        idempotency_key: str,
        result: Any,
    ) -> None:
        action = payload.get("action")
        if action != "send_request":
            # Other actions (cancel/confirm/recall) carry no data the
            # ledger needs back — nothing to project.
            return
        if not isinstance(result, ReShareSendResult):
            # Defensive: a Mock or future client could in principle
            # return something else. Skip projection rather than crash;
            # the wire call already succeeded.
            log.warning(
                "outbox.reshare.projection.unexpected_result_type",
                row_id=row_id,
                saga_id=str(saga_id),
                result_type=type(result).__name__,
            )
            return

        # Validate the supplier's reshare_id shape before persisting it
        # into the ledger and downstream URL components. Audit
        # 2026-05-09 #36: a compromised supplier could otherwise smuggle
        # a control character / template fragment into ``reshare_id``,
        # which flows into ``/rs/patronrequests/{reshare_id}/performAction``
        # and into staff-console rationale strings. Refuse projection on
        # malformed ids; the supplier call already succeeded and the
        # row stays pending for retry — staff can investigate via logs.
        if not (
            isinstance(result.reshare_id, str)
            and _RESHARE_ID_PATTERN.match(result.reshare_id)
        ):
            raise ValueError(
                f"supplier returned malformed reshare_id "
                f"{result.reshare_id!r}; refusing projection"
            )

        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        current = LifecycleState(saga.current_state)
        # Advance to APPROVED only from APPROVING. If the saga has
        # since been compensated (CANCELLED) or otherwise moved, keep
        # the audit row but don't trample current_state.
        state_after = (
            LifecycleState.APPROVED
            if current == LifecycleState.APPROVING
            else current
        )
        if current != LifecycleState.APPROVING:
            log.warning(
                "outbox.reshare.projection.state_not_approving",
                row_id=row_id,
                saga_id=str(saga_id),
                current_state=current.value,
            )

        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=StepName.APPROVE,
                state_before=current,
                state_after=state_after,
                actor="agent:outbox-worker",
                idempotency_key=f"approve-ack-{row_id}",
                iso_message_id=result.iso_message_id,
                payload={
                    "reshare_id": result.reshare_id,
                    "supplier_symbol": result.supplier_symbol,
                    "iso_state": result.state,
                    "source_outbox_row_id": row_id,
                    "source_outbox_idempotency_key": idempotency_key,
                },
                outcome=StepOutcome.COMMITTED,
                rationale=(
                    "Supplier acknowledged via ReShare; "
                    "saga advanced to Approved."
                    if state_after == LifecycleState.APPROVED
                    else (
                        "Supplier acknowledgement received after saga "
                        f"left APPROVING (now {current.value}); "
                        "recorded for audit without state change."
                    )
                ),
            )
        )

    return on_success


def make_ncip_handler(client: NcipClient) -> Handler:
    """Build a Handler that dispatches ``payload['action']`` on an NCIP client.

    Expected payload shape::

        {"action": "check_out" | "check_in",
         "args": { ...method kwargs (excluding idempotency_key)... }}

    NCIP traffic talks to the local ILS for circulation events tied to
    ILL — ``check_out`` when the borrower picks up the supplied item,
    ``check_in`` on return. The idempotency contract mirrors
    :func:`make_reshare_handler`: the key is sourced from the outbox
    row, not the payload, so a worker crash between the remote call
    and ``mark_delivered`` is safe to replay against any NCIP server
    that honours an idempotency token (the mock client dedups on the
    key directly).

    Today the NCIP client is mock-only (CLAUDE.md known-gap); this
    handler is wired into the lifespan ahead of the real HTTP/SOAP
    client landing so flows can start writing ``target="ncip"`` rows
    when the saga design needs them. No ``on_success`` projection is
    registered for ``target="ncip"`` today — NCIP responses don't
    currently carry data the saga ledger consumes — but the handler
    forwards its return value so a future projection can opt in
    without changing this contract.
    """

    async def handler(payload: dict[str, Any], idempotency_key: str) -> Any:
        action = payload.get("action")
        args = payload.get("args", {})
        if not isinstance(action, str):
            raise ValueError(f"ncip outbox payload missing 'action': {payload!r}")
        if not isinstance(args, dict):
            raise ValueError(f"ncip outbox payload 'args' must be dict: {payload!r}")
        if action not in _NCIP_ACTIONS:
            raise ValueError(
                f"ncip action {action!r} not in allow-list "
                f"{sorted(_NCIP_ACTIONS)}"
            )

        method = getattr(client, action, None)
        if method is None or not callable(method):
            raise ValueError(f"ncip client has no action {action!r}")

        return await method(idempotency_key=idempotency_key, **args)

    return handler
