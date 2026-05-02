"""Outbox worker — drains pending outbound dispatches.

The outbox table buffers outbound messages so a saga step can commit
its ledger event atomically and let a separate worker handle
delivery. This keeps the saga's append-only log clean even when the
remote target (ReShare, NCIP, …) is briefly unreachable.

Today the table exists and ``idempotency.outbox_enqueue`` writes to
it, but the saga ``flows.py`` still calls clients inline. This module
provides the drain-and-dispatch half so the infrastructure is ready
when forward steps migrate to the "commit ledger then enqueue"
pattern (a separate ADR / change).

**Payload convention.** A handler is a coroutine
``(payload: dict, idempotency_key: str) -> None`` that raises on
failure. By convention the payload for ``target="reshare"`` is::

    {"action": "send_request" | "cancel_request" | "confirm_shipment"
              | "confirm_return" | "recall_request",
     "args": { ... method kwargs ... }}

``make_reshare_handler(client)`` returns a Handler that dispatches
on ``action``. Other targets get their own builders (NCIP next).

**Transaction discipline.**
- One session per row. A handler failure or DB write for one row must
  never roll back ``delivered_at`` on its neighbours.
- The handler call itself is *not* inside a savepoint. After the
  handler returns successfully we mark ``delivered`` in the same
  session; on exception we open a fresh session to record the
  failure (the session that read the row may be in a bad state if
  the handler did something weird with the connection — fresh
  session is cheap insurance).

**Multi-worker.** This worker assumes a single drainer. Running two
in parallel against the same table would double-deliver because
``outbox_pending`` has no row-level lock. Postgres can fix this with
``SELECT ... FOR UPDATE SKIP LOCKED``; SQLite cannot. Out of scope
for the prototype — flagged in CLAUDE.md known-gaps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from agora.clients.reshare import ReShareClient
from agora.logging import get_logger
from agora.saga.idempotency import (
    outbox_mark_delivered,
    outbox_mark_failed,
    outbox_pending,
)

log = get_logger(__name__)


Handler = Callable[[dict[str, Any], str], Awaitable[None]]
"""Outbox dispatch callable.

Args are ``(payload, idempotency_key)``. Returns nothing on success.
Raise any exception to mark the row failed; the worker translates the
exception message into ``last_error`` and either re-schedules with
exponential backoff or marks ``dead_letter`` when ``attempts`` hits
``max_attempts``.
"""


@dataclass(slots=True)
class DrainStats:
    """Counts from a single drain pass — handy for tests and metrics."""

    delivered: int = 0
    failed: int = 0
    dead_letter: int = 0
    skipped_no_handler: int = 0

    @property
    def total(self) -> int:
        return self.delivered + self.failed + self.dead_letter + self.skipped_no_handler


class OutboxWorker:
    """Polls the outbox and dispatches pending rows via target handlers.

    ``handlers`` is a ``{target: Handler}`` map. A row whose ``target``
    is unknown is left ``pending`` and counted as ``skipped_no_handler``
    rather than failed — adding a handler later picks the row up.

    Backoff is exponential on attempts: ``base_backoff_secs * 2**attempts``.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[Any],
        handlers: dict[str, Handler],
        *,
        max_attempts: int = 10,
        base_backoff_secs: int = 60,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._base_backoff_secs = base_backoff_secs

    async def drain_once(self, *, limit: int = 50) -> DrainStats:
        """Drain up to ``limit`` ready rows. Returns per-row outcomes."""
        stats = DrainStats()

        # Step 1: read pending rows in one session, then close it before
        # we start dispatching. Keeping the read session open while
        # making slow HTTP calls to ReShare would hold a DB connection
        # for the entire dispatch window.
        async with self._sessionmaker() as read_session:
            rows = await outbox_pending(read_session, limit=limit)
            # Snapshot the fields we need so we don't depend on the
            # ORM session staying alive past this block.
            snapshots = [
                (r.id, r.target, r.idempotency_key, dict(r.payload), r.attempts)
                for r in rows
            ]

        for row_id, target, idem_key, payload, attempts in snapshots:
            handler = self._handlers.get(target)
            if handler is None:
                log.warning(
                    "outbox.no_handler",
                    row_id=row_id,
                    target=target,
                    idempotency_key=idem_key,
                )
                stats.skipped_no_handler += 1
                continue

            try:
                await handler(payload, idem_key)
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

            async with self._sessionmaker() as ok_session:
                await outbox_mark_delivered(ok_session, row_id)
                await ok_session.commit()
            stats.delivered += 1
            log.info(
                "outbox.delivered",
                row_id=row_id,
                target=target,
                idempotency_key=idem_key,
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


def make_reshare_handler(client: ReShareClient) -> Handler:
    """Build a Handler that dispatches ``payload['action']`` on ``client``.

    Expected payload shape::

        {"action": "send_request" | "cancel_request" | "confirm_shipment"
                  | "confirm_return" | "recall_request",
         "args": { ...method kwargs (excluding idempotency_key)... }}

    The ``idempotency_key`` is sourced from the outbox row, not the
    payload — that's what makes the dispatch replay-safe even if the
    worker crashes after the remote call but before
    ``mark_delivered`` commits.
    """

    async def handler(payload: dict[str, Any], idempotency_key: str) -> None:
        action = payload.get("action")
        args = payload.get("args", {})
        if not isinstance(action, str):
            raise ValueError(f"reshare outbox payload missing 'action': {payload!r}")
        if not isinstance(args, dict):
            raise ValueError(f"reshare outbox payload 'args' must be dict: {payload!r}")

        method = getattr(client, action, None)
        if method is None or not callable(method):
            raise ValueError(f"reshare client has no action {action!r}")

        await method(idempotency_key=idempotency_key, **args)

    return handler
