"""TrackingAgent — turns ReShare/NCIP observations into ledger events.

Two pieces:

- :class:`TrackingAgent` (the existing manual entry point) — callers
  push an :class:`Observation` and we record it on the ledger.

- :class:`OverdueScanner` — a periodic sweep that finds sagas in
  ``shipped`` whose ``due_at`` (stamped onto the SHIP forward payload)
  has passed and records a single deterministic OBSERVATION event per
  saga. Re-running the scan is idempotent: the observation
  idempotency key is ``f"overdue-{saga_id}"`` so the saga ledger's
  UNIQUE constraint absorbs duplicates.

In production the scanner runs as a cron / background task. The
prototype exposes :meth:`OverdueScanner.scan` as a single async call
the demo and tests can drive directly.
"""

from __future__ import annotations

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
    """Result entry from :meth:`OverdueScanner.scan`."""

    saga_id: UUID
    reshare_id: str | None
    due_at: datetime
    days_overdue: int
    newly_recorded: bool


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
    ):
        self._sm = sessionmaker
        self._actor = actor
        self._now = now_fn or (lambda: datetime.now(UTC))

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
                key = f"overdue-{saga.id}"
                payload = {
                    "kind": "overdue",
                    "reshare_id": ship_event.payload.get("reshare_id"),
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
                # double-notifying staff.
                newly = bool(
                    event is not None and event.idempotency_key == key
                    and event.payload.get("observed_at") == payload["observed_at"]
                )
                results.append(
                    OverdueRecord(
                        saga_id=saga.id,
                        reshare_id=ship_event.payload.get("reshare_id"),
                        due_at=due_at,
                        days_overdue=days_overdue,
                        newly_recorded=newly,
                    )
                )

        log.info(
            "saga.overdue_scan.complete",
            scanned=len(results),
            newly=sum(1 for r in results if r.newly_recorded),
        )
        return results


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on malformed input."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
