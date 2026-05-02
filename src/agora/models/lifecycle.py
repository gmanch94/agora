"""Lifecycle and ISO 18626 state enumerations.

User-facing lifecycle (``LifecycleState``) is a coarse projection of
the supplier-side ISO 18626 state machine (``Iso18626State``). See
``docs/prd/01-lifecycle-and-states.md`` for the mapping table.
"""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    """User-facing lifecycle visible in the staff console."""

    SUBMITTED = "submitted"
    ROUTED = "routed"
    APPROVED = "approved"
    SHIPPED = "shipped"
    RETURNED = "returned"
    CANCELLED = "cancelled"
    UNFILLED = "unfilled"
    DISPUTED = "disputed"


TERMINAL_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RETURNED,
        LifecycleState.CANCELLED,
        LifecycleState.UNFILLED,
        LifecycleState.DISPUTED,
    }
)


class Iso18626State(str, Enum):
    """Supplier-side ISO 18626:2021 status values used by ReShare."""

    REQUESTED = "Requested"
    EXPECT_TO_SUPPLY = "ExpectToSupply"
    WILL_SUPPLY = "WillSupply"
    LOANED = "Loaned"
    OVERDUE = "Overdue"
    RECALLED = "Recalled"
    RETRY_POSSIBLE = "RetryPossible"
    UNFILLED = "Unfilled"
    COPY_COMPLETED = "CopyCompleted"
    LOAN_COMPLETED = "LoanCompleted"
    CANCELLED = "Cancelled"


class StepName(str, Enum):
    """Saga step identifiers; one per forward operation in the lifecycle."""

    SUBMIT = "submit"
    ROUTE = "route"
    APPROVE = "approve"
    SHIP = "ship"
    RETURN_ITEM = "return"
    # Compensators / branches:
    CANCEL = "cancel"
    REROUTE = "reroute"
    REVOKE = "revoke"
    RECALL = "recall"
    DISPUTE = "dispute"


class StepKind(str, Enum):
    """How a step relates to the saga's logical flow."""

    FORWARD = "forward"
    COMPENSATOR = "compensator"


class StepOutcome(str, Enum):
    """Lifecycle of an individual step's execution."""

    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventKind(str, Enum):
    """High-level taxonomy of saga ledger events."""

    FORWARD = "forward"
    COMPENSATOR = "compensator"
    GATE = "gate"
    OBSERVATION = "observation"
