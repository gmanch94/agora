"""ADK-mediated ``LlmTiebreaker`` adapter (PR-2b of ADR-0014 track).

Wires the ``LlmTiebreaker`` Protocol shipped in PR-2a to a real
Gemini call via Google ADK. Decisions pinned in ADR-0003 + ADR-0014:

- ADK `LlmAgent` + `InMemoryRunner` (not raw ``google.genai``) so the
  whole platform speaks one orchestration framework.
- ``temperature=0`` for determinism — eval-floor numbers in
  ``baseline.json`` assume single-run scoring.
- ``output_schema=TiebreakDecisionSchema`` puts Gemini in JSON-mode
  with constrained decoding; the adapter parses the validated payload
  into the dataclass ``TiebreakDecision`` (Protocol-side type).
- Lazy ``google.adk`` import inside ``__init__`` so a base install
  (no ``[adk]`` extra) doesn't crash on ``import agora.agents.routing``.
- Per-call timeout enforced via ``asyncio.wait_for``. On timeout the
  adapter raises; ``RoutingAgent._call_tiebreaker`` catches and falls
  back to the rules pick + diagnostic.

Test surface: ``_invoke_model`` is the seam — tests stub it to return
canned ``TiebreakDecisionSchema`` instances (or raise) without touching
GCP. The schema-to-dataclass conversion in ``resolve`` is what tests
exercise around the stub.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from agora.agents.routing import TiebreakDecision
from agora.agents.routing_tiebreak_prompt import (
    TiebreakDecisionSchema,
    render_prompt,
    system_instruction,
)
from agora.config import get_settings
from agora.logging import get_logger

if TYPE_CHECKING:
    from agora.models.candidate import HolderCandidate
    from agora.models.request import ItemMetadata

log = get_logger(__name__)


# ADK error subclasses surface as plain ``Exception`` from outside the
# package; we catch broadly in ``resolve`` and re-raise so the seam
# fallback path applies. Listing this here as a comment so future
# maintainers can swap to a tighter set if/when ADK exposes one.


class AdkLlmTiebreaker:
    """``LlmTiebreaker`` backed by Google ADK + Gemini.

    Construction is cheap (an ``LlmAgent`` + ``InMemoryRunner`` are
    just objects until ``resolve`` is called). Each ``resolve`` call
    creates its own session — sessions are one-shot, so reusing one
    across calls would leak prompt context.

    Bound by ADR-0014 invariants:
      - never re-raise out to the caller (``RoutingAgent`` catches);
      - rationale ≤1 sentence (enforced by the prompt + the schema
        description; ``RoutingAgent`` adds the meta-prefix that keeps
        the composed rationale ≤3 sentences);
      - chosen_symbol must appear in the input list — the seam in
        PR-2a treats unknown symbols as abstain, so the adapter does
        not need to validate symbol membership itself.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_secs: float | None = None,
        location: str | None = None,
        app_name: str = "agora-routing-tiebreak",
    ) -> None:
        # Lazy ADK import — keep base install (no [adk] extra) clean.
        try:
            from google.adk.agents import LlmAgent
            from google.adk.runners import InMemoryRunner
            from google.genai import types as genai_types
        except ImportError as e:  # pragma: no cover - import-time only
            raise RuntimeError(
                "AdkLlmTiebreaker requires the 'adk' extra: "
                "`pip install 'agora[adk]'` (installs google-adk)."
            ) from e

        settings = get_settings()
        self._model = model or settings.routing_llm_model
        self._timeout = (
            timeout_secs if timeout_secs is not None else settings.routing_llm_timeout_secs
        )
        self._location = location or settings.routing_llm_location
        self._app_name = app_name

        self._agent = LlmAgent(
            name="routing_tiebreaker",
            model=self._model,
            instruction=system_instruction(),
            output_schema=TiebreakDecisionSchema,
            output_key="decision",
            generate_content_config=genai_types.GenerateContentConfig(
                temperature=0.0,
                # response_mime_type / response_schema are derived from
                # output_schema by ADK; no need to set them here.
            ),
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )
        self._runner = InMemoryRunner(agent=self._agent, app_name=self._app_name)
        # Stash the genai Content/Part constructors so ``_invoke_model``
        # doesn't have to re-import — keeps the lazy-import boundary
        # at ``__init__``.
        self._genai_types = genai_types

    async def resolve(
        self,
        candidates: list[HolderCandidate],
        *,
        item: ItemMetadata | None = None,
    ) -> TiebreakDecision:
        """Render prompt, invoke model under timeout, return decision.

        On any failure (timeout, ADK exception, malformed JSON,
        schema-validation error) this method re-raises. The seam in
        ``RoutingAgent._call_tiebreaker`` catches and applies the
        rules-fallback path.
        """
        prompt = render_prompt(candidates, item)
        log.debug(
            "routing.tiebreaker.adk.invoke",
            model=self._model,
            n_candidates=len(candidates),
            timeout_secs=self._timeout,
        )
        decision_schema = await asyncio.wait_for(
            self._invoke_model(prompt),
            timeout=self._timeout,
        )
        return TiebreakDecision(
            chosen_symbol=decision_schema.chosen_symbol,
            rationale=decision_schema.rationale,
        )

    async def _invoke_model(self, prompt: str) -> TiebreakDecisionSchema:
        """Send ``prompt`` through ADK; return validated schema instance.

        Test seam — unit tests stub this method to return a fixed
        ``TiebreakDecisionSchema`` (or raise) without touching GCP.
        Production path drives the InMemoryRunner: create session,
        send a single user message, drain the event stream looking
        for the final response, parse the JSON payload back into the
        schema.
        """
        session = await self._runner.session_service.create_session(
            app_name=self._app_name,
            user_id="agora",
        )
        new_message = self._genai_types.Content(
            role="user",
            parts=[self._genai_types.Part.from_text(text=prompt)],
        )
        final_text = ""
        async for event in self._runner.run_async(
            user_id="agora",
            session_id=session.id,
            new_message=new_message,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                # Concatenate any text parts in the final response
                # (ADK generally emits one, but this is robust).
                final_text = "".join(p.text or "" for p in event.content.parts)
                break

        if not final_text:
            raise RuntimeError("ADK runner returned no final response text")
        payload: dict[str, Any] = json.loads(final_text)
        return TiebreakDecisionSchema.model_validate(payload)
