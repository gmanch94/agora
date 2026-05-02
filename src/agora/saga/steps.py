"""Saga step registry.

Each saga step is a pair of async callables: a forward function and a
compensator. Steps register themselves with ``@register_step`` so the
coordinator can look them up by name.

Forward functions return a ``StepResult`` carrying the new lifecycle
state and a payload that the ledger persists. Compensators take that
payload back so they have everything they need to undo / reverse the
forward effect.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agora.models.lifecycle import LifecycleState, StepName
from agora.saga.context import SagaContext


@dataclass(slots=True)
class OutboxIntent:
    """Side-effect a step wants enqueued atomically with its ledger event.

    The coordinator writes one ``OutboxRow`` per intent inside the same
    DB transaction that appends the ledger event, so the saga ledger
    and the "intent to call ReShare" can never disagree.

    Convention for ``target="reshare"`` (matches ``make_reshare_handler``
    in ``saga/outbox.py``)::

        OutboxIntent(
            target="reshare",
            idempotency_key=<ulid>,
            payload={"action": "confirm_shipment",
                     "args": {"reshare_id": "rs-..."}},
        )

    See ADR-0011 for the full rationale.
    """

    target: str
    idempotency_key: str
    payload: dict[str, Any]


@dataclass(slots=True)
class StepResult:
    """What a forward or compensator function returns to the coordinator."""

    state_after: LifecycleState
    payload: dict[str, Any]
    iso_message_id: str | None = None
    rationale: str | None = None
    outbox: list[OutboxIntent] = field(default_factory=list)


ForwardFn = Callable[[SagaContext], Awaitable[StepResult]]
CompensatorFn = Callable[[SagaContext, dict[str, Any]], Awaitable[StepResult]]


@dataclass(slots=True)
class StepDefinition:
    """A registered (forward, compensator) pair for a step name."""

    name: StepName
    forward: ForwardFn
    compensator: CompensatorFn | None
    description: str = ""


class StepRegistry:
    """Lookup table for step definitions.

    A single global instance is reasonable for the prototype; tests
    can construct private registries to isolate from globals.
    """

    def __init__(self) -> None:
        self._defs: dict[StepName, StepDefinition] = {}

    def register(
        self,
        *,
        name: StepName,
        forward: ForwardFn,
        compensator: CompensatorFn | None = None,
        description: str = "",
    ) -> StepDefinition:
        """Register a step. Idempotent for the same callables."""
        if name in self._defs:
            existing = self._defs[name]
            if existing.forward is forward and existing.compensator is compensator:
                return existing
            raise ValueError(f"step {name.value} already registered with different callables")
        defn = StepDefinition(
            name=name, forward=forward, compensator=compensator, description=description
        )
        self._defs[name] = defn
        return defn

    def get(self, name: StepName) -> StepDefinition:
        if name not in self._defs:
            raise KeyError(f"step {name.value} not registered")
        return self._defs[name]

    def has(self, name: StepName) -> bool:
        return name in self._defs

    def names(self) -> list[StepName]:
        return list(self._defs.keys())


_global_registry = StepRegistry()


def register_step(
    *,
    name: StepName,
    forward: ForwardFn,
    compensator: CompensatorFn | None = None,
    description: str = "",
) -> StepDefinition:
    """Module-level helper that registers into the global registry."""
    return _global_registry.register(
        name=name, forward=forward, compensator=compensator, description=description
    )


def get_global_registry() -> StepRegistry:
    """Accessor for the module-level singleton."""
    return _global_registry
