"""Unit tests for StepRegistry and register_step module helper.

No database required — all pure Python.
"""

from __future__ import annotations

from typing import Any

import pytest

from agora.models.lifecycle import StepName
from agora.saga.context import SagaContext
from agora.saga.steps import StepDefinition, StepRegistry, StepResult, register_step

# ---------------------------------------------------------------------------
# Minimal callable stubs
# ---------------------------------------------------------------------------

async def _fwd_a(ctx: SagaContext) -> StepResult:  # pragma: no cover
    raise NotImplementedError


async def _fwd_b(ctx: SagaContext) -> StepResult:  # pragma: no cover
    raise NotImplementedError


async def _comp_a(ctx: SagaContext, payload: dict[str, Any]) -> StepResult:  # pragma: no cover
    raise NotImplementedError


# ---------------------------------------------------------------------------
# StepRegistry — happy path
# ---------------------------------------------------------------------------


def test_registry_register_and_retrieve() -> None:
    reg = StepRegistry()
    defn = reg.register(name=StepName.SUBMIT, forward=_fwd_a, compensator=_comp_a)
    assert isinstance(defn, StepDefinition)
    assert defn.name == StepName.SUBMIT
    assert defn.forward is _fwd_a
    assert defn.compensator is _comp_a


def test_registry_register_without_compensator() -> None:
    reg = StepRegistry()
    defn = reg.register(name=StepName.SUBMIT, forward=_fwd_a)
    assert defn.compensator is None


# ---------------------------------------------------------------------------
# StepRegistry — idempotent re-registration (same callables)
# ---------------------------------------------------------------------------


def test_registry_idempotent_same_callables() -> None:
    """Registering the exact same callables twice returns the existing definition."""
    reg = StepRegistry()
    d1 = reg.register(name=StepName.SUBMIT, forward=_fwd_a, compensator=_comp_a)
    d2 = reg.register(name=StepName.SUBMIT, forward=_fwd_a, compensator=_comp_a)
    assert d1 is d2


# ---------------------------------------------------------------------------
# StepRegistry — conflict detection (different callables)
# ---------------------------------------------------------------------------


def test_registry_conflict_raises() -> None:
    """Re-registering with different callables raises ValueError."""
    reg = StepRegistry()
    reg.register(name=StepName.SUBMIT, forward=_fwd_a)
    with pytest.raises(ValueError, match="already registered with different callables"):
        reg.register(name=StepName.SUBMIT, forward=_fwd_b)


# ---------------------------------------------------------------------------
# StepRegistry.get — missing step
# ---------------------------------------------------------------------------


def test_registry_get_missing_raises() -> None:
    reg = StepRegistry()
    with pytest.raises(KeyError, match="submit"):
        reg.get(StepName.SUBMIT)


# ---------------------------------------------------------------------------
# StepRegistry.has
# ---------------------------------------------------------------------------


def test_registry_has_absent() -> None:
    reg = StepRegistry()
    assert not reg.has(StepName.SUBMIT)


def test_registry_has_present() -> None:
    reg = StepRegistry()
    reg.register(name=StepName.SUBMIT, forward=_fwd_a)
    assert reg.has(StepName.SUBMIT)


# ---------------------------------------------------------------------------
# StepRegistry.names
# ---------------------------------------------------------------------------


def test_registry_names_empty() -> None:
    reg = StepRegistry()
    assert reg.names() == []


def test_registry_names_populated() -> None:
    reg = StepRegistry()
    reg.register(name=StepName.SUBMIT, forward=_fwd_a)
    reg.register(name=StepName.ROUTE, forward=_fwd_b)
    names = reg.names()
    assert StepName.SUBMIT in names
    assert StepName.ROUTE in names
    assert len(names) == 2


# ---------------------------------------------------------------------------
# register_step module-level helper
# ---------------------------------------------------------------------------


def test_register_step_module_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """register_step delegates to the module-level global registry."""
    import agora.saga.steps as steps_mod

    fresh = StepRegistry()
    monkeypatch.setattr(steps_mod, "_global_registry", fresh)

    defn = register_step(
        name=StepName.SUBMIT,
        forward=_fwd_a,
        compensator=_comp_a,
        description="test step",
    )
    assert isinstance(defn, StepDefinition)
    assert fresh.has(StepName.SUBMIT)
