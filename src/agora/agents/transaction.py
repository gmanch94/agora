"""TransactionAgent — drives ReShare via its REST API.

The TransactionAgent is the only agent that actually calls a
state-changing external API. Every method takes an idempotency key
threaded down from the saga coordinator.
"""

from __future__ import annotations

from typing import Any

from agora.clients.reshare import ReShareClient, ReShareSendResult


class TransactionAgent:
    """Thin wrapper that hides ReShare client construction from steps."""

    def __init__(self, reshare: ReShareClient):
        self._reshare = reshare

    async def submit_to_supplier(
        self,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        supplier_symbol: str,
    ) -> ReShareSendResult:
        return await self._reshare.send_request(
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            supplier_symbol=supplier_symbol,
        )

    async def cancel_at_supplier(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        return await self._reshare.cancel_request(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            reason=reason,
        )

    async def mark_shipped(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        return await self._reshare.confirm_shipment(
            idempotency_key=idempotency_key, reshare_id=reshare_id
        )

    async def mark_returned(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        return await self._reshare.confirm_return(
            idempotency_key=idempotency_key, reshare_id=reshare_id
        )

    async def recall(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        return await self._reshare.recall_request(
            idempotency_key=idempotency_key, reshare_id=reshare_id, reason=reason
        )
