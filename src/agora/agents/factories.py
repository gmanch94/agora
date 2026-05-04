"""Agent-level factories. Mirror the client factories in ``agora.clients``.

Today this module provides only ``get_llm_tiebreaker`` for the
RoutingAgent. Future agent intelligence wiring (DiscoveryAgent
re-ranker, PolicyAgent narrative-summary) lives here too.

Like ``agora.clients.crossref.get_crossref_client`` (PR #46), the
toggle is an explicit boolean (``AGORA_ROUTING_LLM_ENABLED``) rather
than a URL-presence check — reading ADC config or model strings has
no obvious "empty default" sentinel, and a real LLM call costs
money, so a mock-by-default explicit-opt-in pattern is the safer
default for offline dev + tests + CI.
"""

from __future__ import annotations

from agora.agents.routing import LlmTiebreaker
from agora.config import get_settings
from agora.logging import get_logger

log = get_logger(__name__)


def get_llm_tiebreaker() -> LlmTiebreaker | None:
    """Factory: real ``AdkLlmTiebreaker`` when enabled, else ``None``.

    Returning ``None`` is the explicit "no tie-breaker wired" signal —
    ``RoutingAgent(llm_tiebreaker=None)`` is byte-identical to
    ``RoutingAgent()`` with no kwargs (the rules-only baseline path
    pinned by PR-2a's invariant).

    Disabled by default: callers that want LLM augmentation must set
    ``AGORA_ROUTING_LLM_ENABLED=1`` AND have GCP application-default
    credentials bound to a project with the Vertex AI API enabled.
    Without ADC the ``AdkLlmTiebreaker`` will still construct
    successfully — the failure surfaces on the first ``resolve`` call,
    which raises and gets caught by the seam's exception-fallback
    path. The diagnostic in the rationale is the operator's signal
    that the credential isn't actually working.
    """
    s = get_settings()
    if not s.routing_llm_enabled:
        return None
    # Lazy import — keep ``factories`` cheap when LLM is disabled (the
    # default), and don't pay the ADK import cost in test runs that
    # never opt into routing-LLM.
    from agora.agents.routing_llm_adk import AdkLlmTiebreaker

    log.info(
        "routing.tiebreaker.factory.using_adk",
        model=s.routing_llm_model,
        timeout_secs=s.routing_llm_timeout_secs,
        location=s.routing_llm_location,
    )
    return AdkLlmTiebreaker()
