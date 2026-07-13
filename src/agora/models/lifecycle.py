"""Lifecycle and ISO 18626 state enumerations.

User-facing lifecycle (``LifecycleState``) is a coarse projection of
the supplier-side ISO 18626 state machine (``Iso18626State``). See
``docs/prd/01-lifecycle-and-states.md`` for the mapping table.
"""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    """User-facing lifecycle visible in the staff console.

    ``APPROVING`` is an in-flight intermediate between ``ROUTED`` and
    ``APPROVED``: the saga has committed the intent to ask a supplier
    (the APPROVE forward enqueued an outbox row) but the worker has
    not yet observed the supplier ack. See ADR-0012. The current
    APPROVE flow still transitions directly to ``APPROVED``; the new
    state is wired into the enum first so downstream PRs can adopt it
    without an enum-domain migration.
    """

    SUBMITTED = "submitted"
    ROUTED = "routed"
    APPROVING = "approving"
    APPROVED = "approved"
    SHIPPED = "shipped"
    RECEIVED = "received"
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
    RECEIVE = "receive"
    RETURN_ITEM = "return"
    RENEW = "renew"
    # Compensators / branches:
    CANCEL = "cancel"
    REROUTE = "reroute"
    REVOKE = "revoke"
    RECALL = "recall"
    DISPUTE = "dispute"
    # Staff override — not a forward or compensator; no flows.py registration.
    # Written by POST /sagas/{id}/override as an OBSERVATION event to resolve
    # a DISPUTED saga directly to CANCELLED or UNFILLED.
    RESOLVE = "resolve"


# ---------------------------------------------------------------------
# Legal-transition tables (state-machine enforcement).
#
# ``Coordinator.run_forward`` / ``run_compensator`` consult these maps
# and raise ``IllegalTransitionError`` when the saga's *persisted*
# ``current_state`` is not in the step's allowed set. Steps absent from
# a map are fail-closed: they have no legal transition and the
# coordinator refuses to run them (default-deny per ADR-0005).
#
# Forward lifecycle:
#   SUBMITTED -> ROUTED -> APPROVING -> APPROVED -> SHIPPED
#             -> RECEIVED -> RETURNED
# (APPROVE forward lands in APPROVING; the outbox worker's projection
# advances APPROVING -> APPROVED via a COMMITTED OBSERVATION, ADR-0012.)
# ---------------------------------------------------------------------

FORWARD_STEP_ALLOWED_STATES: dict[StepName, frozenset[LifecycleState]] = {
    # SUBMIT is the initial step: the saga row is created at SUBMITTED
    # and the SUBMIT forward re-affirms it (SUBMITTED -> SUBMITTED).
    StepName.SUBMIT: frozenset({LifecycleState.SUBMITTED}),
    StepName.ROUTE: frozenset({LifecycleState.SUBMITTED}),
    StepName.APPROVE: frozenset({LifecycleState.ROUTED}),
    StepName.SHIP: frozenset({LifecycleState.APPROVED}),
    StepName.RECEIVE: frozenset({LifecycleState.SHIPPED}),
    StepName.RETURN_ITEM: frozenset({LifecycleState.RECEIVED}),
    # RENEW keeps the saga at RECEIVED; multiple renewals compose.
    StepName.RENEW: frozenset({LifecycleState.RECEIVED}),
}
"""Per-forward-step allowed *current* states (pre-transition)."""

COMPENSATOR_ALLOWED_STATES: dict[StepName, frozenset[LifecycleState]] = {
    StepName.SUBMIT: frozenset({LifecycleState.SUBMITTED}),
    StepName.ROUTE: frozenset({LifecycleState.ROUTED}),
    # APPROVE comp is reachable from APPROVING (supplier ack pending —
    # the flow itself rejects with a staff-actionable ValueError when
    # no ``reshare_id`` exists yet, surfacing as a 400) and APPROVED.
    StepName.APPROVE: frozenset(
        {LifecycleState.APPROVING, LifecycleState.APPROVED}
    ),
    # SHIP comp runs from SHIPPED or post-RECEIVE — both branches emit
    # the same ReShare recall (see flows.py SHIP comment block).
    StepName.SHIP: frozenset({LifecycleState.SHIPPED, LifecycleState.RECEIVED}),
    StepName.RECEIVE: frozenset({LifecycleState.RECEIVED}),
    # RETURN comp is state-legal only at RETURNED; the ledger's
    # terminal-state guard still refuses the resulting DISPUTED write
    # (RETURNED is terminal), preserving the documented behaviour.
    StepName.RETURN_ITEM: frozenset({LifecycleState.RETURNED}),
    StepName.RENEW: frozenset({LifecycleState.RECEIVED}),
}
"""Per-compensator allowed *current* states."""


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
