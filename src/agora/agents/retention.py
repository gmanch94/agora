"""Patron PII retention — scrub borrower-identifying fields once a saga is
terminal for ``retention_days`` (G-07 / ADR-0020).

The library-record statutes (ALA model policy, CA Govt Code 6267, IL 75
ILCS 70 and equivalents) require destruction of borrower records after
the transaction completes and any disputes are resolved. Agora's
saga ledger is append-only, so "destruction" is in-place anonymisation
of the borrower-identifying fields rather than physical deletion.

Two pieces, mirroring ``agents/tracking.py``:

- :class:`PatronScrubber` — given a saga, replace the borrower fields
  in ``saga.request_payload`` with anonymised placeholders, write a
  ``PATRON_SCRUBBED`` OBSERVATION event to the ledger, and mark the
  saga's ``updated_at`` so the next scan skips it. Single-saga entry
  point exposed for the DSAR ``forget`` endpoint.

- :class:`RetentionScanner` — periodic sweep over sagas in non-disputed
  terminal states whose ``updated_at`` is older than the retention
  window. Spawned from the FastAPI lifespan.

**Anonymisation contract.** Borrower fields are replaced with an
HMAC-SHA256 fingerprint of the cleartext value, salted by
``AGORA_PII_SCRUB_SALT`` (a 32-byte secret). The fingerprint is
deterministic so a future DSAR query against the same cleartext value
can locate the same scrubbed rows; the salt prevents offline
rainbow-table attacks on the (small) patron-id universe.

Fields scrubbed in ``saga.request_payload``:
- ``patron.patron_id`` → ``"scrubbed:<hmac-hex>"``
- ``item.item_barcode`` → ``None``
- ``patron.patron_email`` → ``None`` (when present; portal future)

Saga lifecycle, event timeline, and routing decisions are preserved.

**Idempotency.** A ``patron_scrubbed-{saga_id}`` idempotency key
attached to the OBSERVATION event makes re-runs of the scanner safe:
the UNIQUE constraint on ``saga_event.idempotency_key`` returns the
prior row, the scrubber detects already-scrubbed state via the
``"scrubbed:"`` prefix, and the second pass is a no-op.

**Fail-closed on empty salt.** If ``AGORA_PII_SCRUB_SALT`` is empty the
scrubber refuses to run — production deployments must rotate a real
secret rather than land an all-zero anonymisation accidentally. The
DSAR endpoints surface the precondition as a 503.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from agora.logging import get_logger
from agora.models.lifecycle import LifecycleState, StepName, StepOutcome
from agora.saga.coordinator import Coordinator
from agora.saga.db import OutboxRow, Saga, SagaEventRow

log = get_logger(__name__)


# Terminal states eligible for scrub. DISPUTED is excluded — a DISPUTED
# saga has an open issue that staff hasn't resolved; scrubbing its
# borrower data while the dispute is live destroys the evidence needed
# to resolve it. DISPUTED sagas must be resolved (via /override) to
# CANCELLED or UNFILLED before they enter the retention window.
SCRUB_ELIGIBLE_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RETURNED,
        LifecycleState.CANCELLED,
        LifecycleState.UNFILLED,
    }
)

# Prefix used on the scrubbed ``patron_id`` value. Lets the staff
# console + downstream readers detect "this saga has been scrubbed"
# without consulting the event timeline.
SCRUBBED_PREFIX = "scrubbed:"


class RetentionConfigError(RuntimeError):
    """Raised when scrubbing is invoked without a usable scrub salt.

    Production deployments MUST set ``AGORA_PII_SCRUB_SALT`` to a
    32-byte secret. Empty / whitespace-only values fail closed rather
    than silently anonymising with a zero salt.
    """


# Minimum salt length. Reviewer MEDIUM: a 1-byte salt is effectively
# zero-entropy — fingerprints become enumerable by precomputation
# against the small patron-id universe of one library. 32 hex chars
# ≈ 128 bits matches the `secrets.token_hex(32)` recommendation in
# `.env.example`. ``_fingerprint`` enforces this AND ``_require_scrub_salt``
# in app.py rejects at the API boundary so the boot path and the
# request path both fail closed.
MIN_SCRUB_SALT_LEN = 32


def _fingerprint(value: str, salt: str) -> str:
    """HMAC-SHA256 of the cleartext value, hex-encoded.

    Deterministic so a DSAR query against the same cleartext finds
    the same scrubbed rows. Salt is required, length-checked — see
    :class:`RetentionConfigError`.
    """
    if not salt.strip() or len(salt) < MIN_SCRUB_SALT_LEN:
        raise RetentionConfigError(
            "AGORA_PII_SCRUB_SALT must be at least "
            f"{MIN_SCRUB_SALT_LEN} chars of high-entropy secret "
            "(`python -c 'import secrets; print(secrets.token_hex(32))'`)"
        )
    return hmac.new(
        salt.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def fingerprint_patron(value: str, salt: str) -> str:
    """Public helper — returns the scrubbed-form sentinel for a patron id.

    Used by the DSAR ``GET /admin/patrons/{patron_id}/sagas`` endpoint
    to match a cleartext query against already-scrubbed rows.
    """
    return SCRUBBED_PREFIX + _fingerprint(value, salt)


def is_scrubbed(patron_id: str | None) -> bool:
    """``True`` when the patron-id payload has already been scrubbed."""
    return isinstance(patron_id, str) and patron_id.startswith(SCRUBBED_PREFIX)


# Reviewer HIGH: the saga ledger (``saga_event.payload``) and the
# outbox queue (``outbox.payload``) carry borrower data alongside the
# top-level ``saga.request_payload`` — RECEIVE / RETURN forwards write
# ``patron_id`` into NCIP intent payloads, and FORWARD events store
# the step inputs verbatim. A retention scrub that only touches
# ``saga.request_payload`` leaves cleartext breadcrumbs in those two
# tables. ``_deep_scrub_json`` walks any JSON tree replacing the
# patron-id key (when value matches cleartext) and nulling
# item_barcode / patron_email regardless of value.
def _deep_scrub_json(
    obj: Any,
    cleartext_patron_id: str,
    scrubbed_patron_id: str,
    cleartext_barcode: str | None = None,
) -> Any:
    """Recursively replace borrower fields inside a JSON-shaped tree.

    Returns the scrubbed tree (mutating dict / list in place AND
    returning is safe because callers reassign the column attribute
    to force SQLAlchemy mutation detection).

    Rules:
    - key ``patron_id``    → replace value when it equals
                              ``cleartext_patron_id``. Leaves
                              already-scrubbed values untouched.
    - key ``item_barcode`` → null any non-null string value.
    - key ``patron_email`` → null any non-null string value.
    - key ``item_id``      → null when the value equals
                              ``cleartext_barcode``. NCIP intents
                              (``saga/flows.py`` RECEIVE / RETURN)
                              store the ILS barcode under ``item_id``;
                              the equality guard preserves rows where
                              ``item_id`` fell back to ``reshare_id``
                              (a supplier-side UUID, not borrower PII).

    Other keys / shapes pass through unchanged. Non-dict / non-list
    leaves are returned as-is.
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "patron_id" and isinstance(v, str) and v == cleartext_patron_id:
                obj[k] = scrubbed_patron_id
            elif (k in {"item_barcode", "patron_email"} and v is not None) or (
                k == "item_id"
                and cleartext_barcode is not None
                and v == cleartext_barcode
            ):
                obj[k] = None
            else:
                obj[k] = _deep_scrub_json(
                    v, cleartext_patron_id, scrubbed_patron_id, cleartext_barcode
                )
        return obj
    if isinstance(obj, list):
        return [
            _deep_scrub_json(
                item, cleartext_patron_id, scrubbed_patron_id, cleartext_barcode
            )
            for item in obj
        ]
    return obj


@dataclass(slots=True, frozen=True)
class ScrubResult:
    """Outcome of a single saga scrub.

    ``scrubbed`` is True when this call mutated the saga; False when
    the saga was already scrubbed (replay-safe no-op).
    """

    saga_id: UUID
    scrubbed: bool
    fingerprint: str | None


class PatronScrubber:
    """Anonymise the borrower fields on a single saga.

    Takes the session per-call (not at init) so the OBSERVATION event
    lands on the same session as the ``request_payload`` mutation. A
    per-call :class:`Coordinator` is constructed against that session;
    pattern mirrors :class:`~agora.agents.tracking.OverdueScanner`.
    """

    def __init__(self, salt: str) -> None:
        self._salt = salt

    async def scrub(
        self, session: AsyncSession, saga: Saga
    ) -> ScrubResult:
        """Apply the scrub to ``saga`` in-place.

        Caller owns the surrounding transaction. The OBSERVATION event
        is written via a session-bound coordinator — caller should
        commit afterwards.
        """
        payload = dict(saga.request_payload or {})
        patron = dict(payload.get("patron") or {})
        existing_id = patron.get("patron_id")

        if not isinstance(existing_id, str) or is_scrubbed(existing_id):
            # Already scrubbed (or no patron_id at all) — replay-safe.
            return ScrubResult(saga_id=saga.id, scrubbed=False, fingerprint=None)

        fp = _fingerprint(existing_id, self._salt)
        scrubbed_id = SCRUBBED_PREFIX + fp
        patron["patron_id"] = scrubbed_id
        # ``patron_email`` is future-portal territory; scrub if present.
        if patron.get("patron_email"):
            patron["patron_email"] = None
        payload["patron"] = patron

        item = dict(payload.get("item") or {})
        # Capture the cleartext barcode BEFORE nulling — the deep scrub
        # below needs it to match NCIP ``item_id`` values (flows.py
        # stores ``item_barcode`` under that key in check_out/check_in
        # payloads; the reshare_id fallback must survive).
        raw_barcode = item.get("item_barcode")
        cleartext_barcode = raw_barcode if isinstance(raw_barcode, str) else None
        if item.get("item_barcode"):
            item["item_barcode"] = None
            payload["item"] = item

        saga.request_payload = payload
        flag_modified(saga, "request_payload")

        # Reviewer HIGH: walk every saga_event payload + every outbox
        # row for this saga and replace any cleartext patron_id /
        # null any item_barcode / patron_email. Without this the
        # FORWARD step payloads (RECEIVE / RETURN write patron_id into
        # NCIP intent.args) and the outbox queue leave cleartext PII
        # that defeats the retention policy.
        event_stmt = select(SagaEventRow).where(SagaEventRow.saga_id == saga.id)
        events = (await session.execute(event_stmt)).scalars().all()
        for ev in events:
            new_payload = _deep_scrub_json(
                dict(ev.payload or {}), existing_id, scrubbed_id, cleartext_barcode
            )
            ev.payload = new_payload
            # Plain JSON columns don't auto-track nested mutations;
            # ``flag_modified`` forces SQLAlchemy to mark the attribute
            # dirty so the UPDATE is emitted on flush.
            flag_modified(ev, "payload")

        outbox_stmt = select(OutboxRow).where(OutboxRow.saga_id == saga.id)
        rows = (await session.execute(outbox_stmt)).scalars().all()
        for row in rows:
            new_payload = _deep_scrub_json(
                dict(row.payload or {}), existing_id, scrubbed_id, cleartext_barcode
            )
            row.payload = new_payload
            flag_modified(row, "payload")

        coord = Coordinator(session=session)
        await coord.record_observation(
            saga_id=saga.id,
            step=StepName.RESOLVE,
            actor="agent:retention",
            payload={
                "kind": "patron_scrubbed",
                "fingerprint": fp,
                "retention_policy": "g07",
            },
            rationale="Patron PII scrubbed per retention policy (G-07).",
            idempotency_key=f"patron_scrubbed-{saga.id}",
        )
        return ScrubResult(saga_id=saga.id, scrubbed=True, fingerprint=fp)


class RetentionScanner:
    """Periodic sweep over terminal sagas past the retention window.

    Pattern mirrors :class:`agora.agents.tracking.OverdueScanner`:
    deterministic idempotency keys, UNIQUE-constraint absorbs replay,
    no outbox writes, no state changes.

    Single-scanner safe by construction. Multi-scanner deployments
    contend on the saga ledger UNIQUE constraint and the second
    invocation gets the existing row back — duplicate work but never
    duplicate scrub.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        scrubber: PatronScrubber,
        retention_days: int,
        interval_secs: float,
        jitter_secs: float = 30.0,
        batch_size: int = 500,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if retention_days < 0:
            raise ValueError("retention_days must be >= 0")
        if interval_secs <= 0:
            raise ValueError("interval_secs must be > 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        self._sm = sessionmaker
        self._scrubber = scrubber
        self._retention_days = retention_days
        self._interval = interval_secs
        self._jitter = max(jitter_secs, 0.0)
        self._batch_size = batch_size
        self._now = clock

    async def scan(self) -> list[ScrubResult]:
        """Run one sweep. Returns the per-saga results.

        Selection predicate (reviewer HIGH — starvation fix):

        - Already-scrubbed sagas are excluded IN SQL via the
          ``scrubbed:`` prefix on ``request_payload.patron.patron_id``
          (same JSON path as the ``ix_saga_patron_id`` expression
          index). Pre-fix, scrubbed rows re-entered the bounded window
          forever (``scrub`` early-returns without bumping
          ``updated_at``), so a backlog larger than the batch size
          could starve never-scrubbed sagas indefinitely.
        - Sagas with no string ``patron_id`` are excluded via
          ``IS NOT NULL`` — they carry nothing to scrub and would
          otherwise re-select forever for the same reason.
        - Eligibility keys off the ts of the earliest COMMITTED event
          whose ``state_after`` equals the saga's current terminal
          state — NOT ``updated_at``, which bumps on any row touch
          (late OBSERVATIONs, tracking refreshes) and would restart
          the retention clock past the statutory deadline. Sagas with
          no such event (fixtures, hand-seeded rows) fall back to
          ``updated_at`` via COALESCE.
        - ``ORDER BY updated_at ASC`` makes the bounded window
          deterministic, oldest first.
        """
        cutoff = self._now() - timedelta(days=self._retention_days)
        states = [s.value for s in SCRUB_ELIGIBLE_STATES]
        patron_id_expr = Saga.request_payload["patron"]["patron_id"].astext
        terminal_ts = (
            select(func.min(SagaEventRow.ts))
            .where(
                SagaEventRow.saga_id == Saga.id,
                SagaEventRow.state_after == Saga.current_state,
                SagaEventRow.outcome == StepOutcome.COMMITTED.value,
            )
            .correlate(Saga)
            .scalar_subquery()
        )
        async with self._sm() as session, session.begin():
            stmt = (
                select(Saga)
                .where(
                    Saga.current_state.in_(states),
                    patron_id_expr.is_not(None),
                    patron_id_expr.not_like(f"{SCRUBBED_PREFIX}%"),
                    func.coalesce(terminal_ts, Saga.updated_at) < cutoff,
                )
                .order_by(Saga.updated_at.asc())
                # bounded per-tick to keep transactions short.
                .limit(self._batch_size)
            )
            rows = (await session.execute(stmt)).scalars().all()
            results: list[ScrubResult] = []
            for saga in rows:
                res = await self._scrubber.scrub(session, saga)
                if res.scrubbed:
                    log.info(
                        "retention.saga_scrubbed",
                        saga_id=str(saga.id),
                        fingerprint=res.fingerprint,
                        retention_days=self._retention_days,
                    )
                results.append(res)
        return results

    async def run_forever(self) -> None:
        """Periodic driver — spawned from the FastAPI lifespan.

        Sleeps ``interval`` (plus optional jitter) between sweeps so
        multiple instances don't synchronise on the same scan tick.
        """
        log.info(
            "retention.scanner_started",
            interval_secs=self._interval,
            retention_days=self._retention_days,
        )
        while True:
            try:
                await self.scan()
            except (
                Exception
            ):
                log.exception("retention.scan_failed")
            # Jitter dampens the synchronised-storm risk of multi-replica
            # deployments (see ADR equivalent for OverdueScanner).
            sleep_for = self._interval + random.uniform(0.0, self._jitter)  # nosec B311
            await asyncio.sleep(sleep_for)


def saga_payload_patron_id(saga: Saga) -> str | None:
    """Read the patron_id off a saga, surviving missing nested keys."""
    payload: dict[str, Any] = saga.request_payload or {}
    patron = payload.get("patron") or {}
    pid = patron.get("patron_id")
    return pid if isinstance(pid, str) else None
