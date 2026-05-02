"""NCIP client stub.

NCIP traffic talks to the local ILS for circulation events tied to ILL:
borrower check-out, hold pickup, fines accrual, check-in.

This module exposes a Protocol + a mock; the real HTTP/SOAP wrapper is
deferred (FOLIO's `mod-ncip` exposes its own HTTP API which we will
target). For prototype demos and tests, the mock is sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agora.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class NcipResult:
    item_id: str
    patron_id: str
    state: str  # 'checked_out' | 'checked_in' | 'on_hold'


class NcipClient(Protocol):
    async def check_out(
        self, *, idempotency_key: str, item_id: str, patron_id: str
    ) -> NcipResult: ...

    async def check_in(
        self, *, idempotency_key: str, item_id: str
    ) -> NcipResult: ...

    async def health(self) -> bool: ...


class MockNcipClient:
    """In-memory NCIP double for prototype/tests."""

    def __init__(self) -> None:
        self._state: dict[str, NcipResult] = {}
        self._idem: dict[str, NcipResult] = {}

    async def check_out(
        self, *, idempotency_key: str, item_id: str, patron_id: str
    ) -> NcipResult:
        if (prior := self._idem.get(idempotency_key)) is not None:
            return prior
        result = NcipResult(item_id=item_id, patron_id=patron_id, state="checked_out")
        self._state[item_id] = result
        self._idem[idempotency_key] = result
        return result

    async def check_in(self, *, idempotency_key: str, item_id: str) -> NcipResult:
        if (prior := self._idem.get(idempotency_key)) is not None:
            return prior
        prior_state = self._state.get(item_id)
        patron = prior_state.patron_id if prior_state else "unknown"
        result = NcipResult(item_id=item_id, patron_id=patron, state="checked_in")
        self._state[item_id] = result
        self._idem[idempotency_key] = result
        return result

    async def health(self) -> bool:
        return True


def get_client() -> NcipClient:
    """Factory: returns mock for prototype; real client TBD."""
    log.info("ncip.client.using_mock")
    return MockNcipClient()  # type: ignore[return-value]
