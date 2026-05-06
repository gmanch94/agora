"""Unit tests for TransactionAgent — verifies delegation to ReShareClient.

No database required.  Uses MockReShareClient so the thin wrapper
methods (submit_to_supplier, cancel_at_supplier, …) are exercised
directly rather than only through the saga coordinator.
"""

from __future__ import annotations

import pytest

from agora.agents.transaction import TransactionAgent
from agora.clients.reshare import MockReShareClient


@pytest.fixture()
def agent() -> TransactionAgent:
    return TransactionAgent(MockReShareClient())


# ---------------------------------------------------------------------------
# submit_to_supplier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_to_supplier_returns_result(agent: TransactionAgent) -> None:
    result = await agent.submit_to_supplier(
        idempotency_key="key-001",
        request_payload={"title": "Brave New World"},
        supplier_symbol="LIB-B",
    )
    assert result.supplier_symbol == "LIB-B"
    assert result.reshare_id
    assert result.state == "Requested"


# ---------------------------------------------------------------------------
# cancel_at_supplier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_at_supplier_returns_result(agent: TransactionAgent) -> None:
    # First create a request so the mock has a reshare_id to cancel.
    created = await agent.submit_to_supplier(
        idempotency_key="key-submit",
        request_payload={"title": "Book"},
        supplier_symbol="LIB-C",
    )
    result = await agent.cancel_at_supplier(
        idempotency_key="key-cancel",
        reshare_id=created.reshare_id,
        reason="patron no longer needs",
    )
    assert result.reshare_id == created.reshare_id
    assert result.state == "Cancelled"


# ---------------------------------------------------------------------------
# mark_shipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_shipped_returns_result(agent: TransactionAgent) -> None:
    created = await agent.submit_to_supplier(
        idempotency_key="key-s",
        request_payload={"title": "Book"},
        supplier_symbol="LIB-D",
    )
    result = await agent.mark_shipped(
        idempotency_key="key-ship",
        reshare_id=created.reshare_id,
    )
    assert result.reshare_id == created.reshare_id
    assert result.state == "Loaned"


# ---------------------------------------------------------------------------
# mark_returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_returned_returns_result(agent: TransactionAgent) -> None:
    created = await agent.submit_to_supplier(
        idempotency_key="key-r",
        request_payload={"title": "Book"},
        supplier_symbol="LIB-E",
    )
    result = await agent.mark_returned(
        idempotency_key="key-ret",
        reshare_id=created.reshare_id,
    )
    assert result.reshare_id == created.reshare_id
    assert result.state == "LoanCompleted"


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_result(agent: TransactionAgent) -> None:
    created = await agent.submit_to_supplier(
        idempotency_key="key-rc",
        request_payload={"title": "Book"},
        supplier_symbol="LIB-F",
    )
    result = await agent.recall(
        idempotency_key="key-recall",
        reshare_id=created.reshare_id,
        reason="overdue",
    )
    assert result.reshare_id == created.reshare_id
    assert result.state == "Recalled"
