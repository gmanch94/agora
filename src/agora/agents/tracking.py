"""TrackingAgent — turns ReShare/NCIP observations into ledger events.

Two pieces:

- :class:`TrackingAgent` (the existing manual entry point) — callers
  push an :class:`Observation` and we record it on the ledger.

- :class:`OverdueScanner` — a periodic sweep that finds sagas in
  ``shipped`` whose ``due_at`` (stamped onto the SHIP forward payload)
  has passed and records deterministic OBSERVATION events per saga.

  Two-tier emission, both advisory (no outbox, no state change, no
  auto-compensator dispatch — see ADR-0005):

    1. ``overdue-{saga_id}`` — written on the first scan past
       ``due_at``. Surfaces a "X day(s) overdue" badge to staff.

    2. ``recall-proposed-{saga_id}`` — written on the first scan past
       ``due_at + recall_after_days`` (default 14). Carries
       ``suggested_action: "compensate_ship"`` so the staff console can
       render a "recommend recall" CTA. Staff still clicks
       ``/sagas/{id}/compensate`` — the scanner never auto-recalls.

  Re-running the scan is idempotent: both keys collide on the saga
  ledger's UNIQUE constraint and the existing rows are returned. The
  recorded ``days_overdue`` snapshot is intentionally stale; the UI
  computes "currently N days" from ``due_at`` + render-time clock.

In production the scanner runs as a cron / background task. The
prototype exposes :meth:`OverdueScanner.scan` as a single async call
the demo and tests can drive directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agora.logging import get_logger
from agora.models.lifecycle import LifecycleState, StepName
from agora.saga.coordinator import Coordinator
from agora.saga.db import Saga
from agora.saga.ledger import SagaLedger

log = get_logger(__name__)


@dataclass(slots=True)
class Observation:
    saga_id: UUID
    step: StepName
    payload: dict[str, Any]
    rationale: str | None = None
    actor: str = "agent:tracking"


class TrackingAgent:
    """Append observations to the saga ledger.

    Observations don't change lifecycle state — they record information
    about an in-flight saga (e.g. ``due_date_set``, ``overdue_warning``,
    ``ils_check_in``). The coordinator decides whether to escalate.
    """

    def __init__(self, coordinator: Coordinator):
        self._coord = coordinator

    async def observe(self, obs: Observation) -> None:
        await self._coord.record_observation(
            saga_id=obs.saga_id,
            step=obs.step,
            actor=obs.actor,
            payload=obs.payload,
            rationale=obs.rationale,
        )


@dataclass(slots=True, frozen=True)
class OverdueRecord:
    """Result entry from :meth:`OverdueScanner.scan`.

    ``newly_recorded`` reflects the tier-1 ``overdue-{saga_id}``
    observation; ``recall_proposed_newly`` reflects the tier-2
    ``recall-proposed-{saga_id}`` observation. Both are False on
    replay (UNIQUE collision returned the existing row); callers can
    use either flag to gate one-shot side-effects (e.g. staff email).
    """

    saga_id: UUID
    reshare_id: str | None
    due_at: datetime
    days_overdue: int
    newly_recorded: bool
    recall_proposed: bool = False
    recall_proposed_newly: bool = False


class OverdueScanner:
    """Find shipped sagas past their due date and observe overdue.

    Each scan:
      1. Loads sagas where ``current_state == 'shipped'``.
      2. For each, fetches the most recent committed ``ship`` forward
         and reads ``due_at`` from its payload.
      3. If ``due_at < now`` an OBSERVATION event is appended with a
         deterministic idempotency key. The first scan past the due
         date records the event; subsequent scans hit the UNIQUE
         constraint on ``saga_event.idempotency_key`` and the ledger
         returns the existing row — no duplicate observation.

    The scanner does not change lifecycle state; that decision belongs
    to staff. The observation surfaces in the staff console as a
    badge / due-date warning. See ADR-0005.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        actor: str = "agent:tracking",
        now_fn: Callable[[], datetime] | None = None,
        recall_after_days: int | None = None,
    ):
        self._sm = sessionmaker
        self._actor = actor
        self._now = now_fn or (lambda: datetime.now(UTC))
        # Resolve threshold lazily-by-construction so callers can pin it
        # in tests without monkeypatching settings. Production wiring
        # passes ``settings.tracking_recall_after_days`` from the API
        # lifespan.
        if recall_after_days is None:
            from agora.config import get_settings

            recall_after_days = get_settings().tracking_recall_after_days
        self._recall_after_days = recall_after_days

    async def scan(self) -> list[OverdueRecord]:
        """Run one pass over shipped sagas; return overdue records found."""
        now = self._now()
        results: list[OverdueRecord] = []

        async with self._sm() as session, session.begin():
            stmt = select(Saga).where(
                Saga.current_state == LifecycleState.SHIPPED.value
            )
            sagas = (await session.execute(stmt)).scalars().all()

            ledger = SagaLedger(session)
            coord = Coordinator(session=session)
            for saga in sagas:
                ship_event = await ledger.find_committed_forward(
                    saga.id, StepName.SHIP.value
                )
                if ship_event is None:
                    continue
                due_iso = ship_event.payload.get("due_at")
                if not due_iso:
                    continue
                due_at = _parse_iso(due_iso)
                if due_at is None or due_at >= now:
                    continue

                days_overdue = max((now - due_at).days, 0)
                reshare_id = ship_event.payload.get("reshare_id")
                key = f"overdue-{saga.id}"
                payload = {
                    "kind": "overdue",
                    "reshare_id": reshare_id,
                    "due_at": due_iso,
                    "observed_at": now.isoformat(),
                    "days_overdue": days_overdue,
                }
                rationale = (
                    f"Item {days_overdue} day(s) past due "
                    f"({due_at.date().isoformat()})."
                )
                event = await coord.record_observation(
                    saga_id=saga.id,
                    step=StepName.SHIP,
                    actor=self._actor,
                    payload=payload,
                    rationale=rationale,
                    idempotency_key=key,
                )
                # Replay: ledger.append returned the existing row
                # because the deterministic key collided. We still
                # return a record for the caller's bookkeeping but
                # mark it not-newly-recorded so callers can avoid
                # double-notifying staff. The returned event always
                # carries the key we passed in; the differentiator is
                # whether ``observed_at`` matches what *we* wrote (yes
                # = we won the race / first scan; no = a prior scan
                # already recorded it).
                newly = event.payload.get("observed_at") == payload["observed_at"]

                # Tier-2: recall_proposed once we cross the threshold.
                # Advisory only — no outbox row, no state change. Staff
                # console renders ``suggested_action`` as a CTA pointing
                # at ``POST /sagas/{id}/compensate`` for SHIP. Replay is
                # absorbed by the saga-event UNIQUE constraint exactly
                # like tier-1.
                recall_proposed = days_overdue >= self._recall_after_days
                recall_newly = False
                if recall_proposed:
                    recall_key = f"recall-proposed-{saga.id}"
                    recall_payload = {
                        "kind": "recall_proposed",
                        "suggested_action": "compensate_ship",
                        "reshare_id": reshare_id,
                        "due_at": due_iso,
                        "observed_at": now.isoformat(),
                        "days_overdue": days_overdue,
                        "threshold_days": self._recall_after_days,
                    }
                    recall_rationale = (
                        f"Item {days_overdue} day(s) overdue "
                        f"(>= {self._recall_after_days}); recommend "
                        "issuing a recall via /compensate."
                    )
                    recall_event = await coord.record_observation(
                        saga_id=saga.id,
                        step=StepName.SHIP,
                        actor=self._actor,
                        payload=recall_payload,
                        rationale=recall_rationale,
                        idempotency_key=recall_key,
                    )
                    recall_newly = (
                        recall_event.payload.get("observed_at")
                        == recall_payload["observed_at"]
                    )
                results.append(
                    OverdueRecord(
                        saga_id=saga.id,
                        reshare_id=reshare_id,
                        due_at=due_at,
                        days_overdue=days_overdue,
                        newly_recorded=newly,
                        recall_proposed=recall_proposed,
                        recall_proposed_newly=recall_newly,
                    )
                )

        log.info(
            "saga.overdue_scan.complete",
            scanned=len(results),
            newly=sum(1 for r in results if r.newly_recorded),
            recall_proposed=sum(1 for r in results if r.recall_proposed),
            recall_proposed_newly=sum(
                1 for r in results if r.recall_proposed_newly
            ),
        )
        return results

    async def run_forever(self, *, poll_interval: float = 300.0) -> None:
        """Production loop: scan, sleep, repeat. Cancellation-aware.

        Mirror of :meth:`OutboxWorker.run_forever`. Caller is expected to
        wrap this in :func:`asyncio.create_task` and ``task.cancel()`` on
        shutdown. We catch :class:`asyncio.CancelledError` to log a clean
        exit message and re-raise.

        Per-pass exceptions are logged + swallowed so a transient DB
        glitch doesn't kill the loop. The scanner is naturally
        idempotent (deterministic ``overdue-{saga_id}`` key absorbed by
        the ledger UNIQUE constraint), so re-scanning after a partial
        pass is safe.

        ``poll_interval`` defaults to 300s (5 min) — overdue detection
        is not time-critical, and a long interval keeps log volume low.
        Set via ``AGORA_TRACKING_SCAN_INTERVAL_SECS`` in production.
        """
        log.info("tracking.scanner.start", poll_interval=poll_interval)
        try:
            while True:
                try:
                    await self.scan()
                except Exception as exc:
                    # Don't let a scanner-level bug kill the loop;
                    # log and back off for the normal poll interval.
                    log.exception(
                        "tracking.scanner.unexpected_error", error=str(exc)
                    )
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            log.info("tracking.scanner.cancelled")
            raise


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on malformed input."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
