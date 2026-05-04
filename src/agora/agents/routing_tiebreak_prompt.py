"""Prompt template + structured-output schema for the LLM tie-breaker.

Kept in its own module so prompt-wording iteration shows up as
prompt-only diffs (advisor recommendation — review hygiene). The
adapter (``routing_llm_adk.py``) renders against this template and
parses the model's JSON response back into a ``TiebreakDecision``.

Constraints (pinned by ADR-0014):

- The LLM rationale MUST be one short sentence. ``RoutingAgent`` wraps
  it in a meta-prefix ("Rules tied X/Y at gap Z; LLM picked Y: ...")
  so the composed rationale stays ≤3 sentences total (PRD-02).
- The LLM MAY abstain by returning ``chosen_symbol: null``. The agent
  then keeps the rules pick. Abstain is preferred over guessing on
  signals the prompt didn't surface.
- The LLM MUST pick a symbol from the candidate list it was given —
  picking an external ISIL is treated as malformed and falls back.

The schema is a pydantic model so ADK's ``LlmAgent.output_schema``
can use it for structured output (Gemini JSON-mode constrained
decoding). Validation lives at the schema layer; the adapter only
has to read fields back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agora.models.candidate import HolderCandidate
    from agora.models.request import ItemMetadata


class TiebreakDecisionSchema(BaseModel):
    """Pydantic mirror of ``TiebreakDecision`` for ADK structured output.

    Kept distinct from the ``@dataclass`` ``TiebreakDecision`` so the
    Protocol contract in ``routing.py`` doesn't pull pydantic into
    every importer of ``RoutingAgent``. The adapter converts.
    """

    chosen_symbol: str | None = Field(
        default=None,
        description=(
            "Symbol of the chosen candidate, or null to abstain. "
            "MUST exactly match one of the symbols listed in the prompt; "
            "an unknown symbol is treated as abstain."
        ),
    )
    rationale: str = Field(
        ...,
        description=(
            "One short sentence justifying the pick (or the abstention). "
            "Will be embedded in the staff-facing rationale; keep it ≤25 words."
        ),
    )


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


_SYSTEM_INSTRUCTION = """\
You are a routing tie-breaker for an inter-library loan (ILL) consortium.
The deterministic rules engine has already narrowed the field; the top
candidates are within an epsilon of each other on the rules score, so
your job is to pick which one staff would have picked given the
metadata that the rules engine cannot read.

Decision signals to weigh (in rough priority order):
  1. SLA tier ("raw.sla_tier"): "fast" or "A" beats "standard" or
     "B" beats "slow" or "C". Letter grades and verbal tiers both
     appear in scenarios; treat earlier letters / faster verbal
     tiers as better.
  2. Reciprocity balance ("raw.reciprocity_balance"): NEGATIVE
     numbers mean the consortium already owes this lender (we have
     borrowed more from them than we have lent back); positive
     numbers mean the lender owes us. Borrowing again from a
     lender we already owe deepens the imbalance — prefer the
     candidate with the MORE-POSITIVE (or less-negative) balance
     to keep consortium fairness healthy.
  3. Format / delivery affinity: if the request "item_kind" is
     "article" or "chapter" (i.e. copy-style not return-style),
     prefer a candidate whose "raw.holds_format" or "raw.delivery"
     indicates digital / electronic fulfillment.
  4. Historical reliability ("raw.on_time_rate"): higher is better.
  5. Distance: only as a final tie-break.

Rules-of-engagement:
  - Pick exactly one symbol from the listed candidates, or null to abstain.
  - Abstain when the metadata above does not actually distinguish the
    candidates — do NOT invent signals.
  - Rationale must be ONE short sentence (≤25 words).
  - Output strictly the JSON schema you were given. No prose around it.
"""


def _format_item(item: ItemMetadata | None) -> str:
    if item is None:
        return "(no item metadata supplied)"
    fields = []
    for attr in ("title", "author", "item_kind", "year", "doi", "isbn", "issn"):
        v = getattr(item, attr, None)
        if v:
            fields.append(f"{attr}={v}")
    return ", ".join(fields) if fields else "(empty item metadata)"


def _format_candidate(c: HolderCandidate) -> str:
    parts = [
        f"symbol={c.symbol}",
        f"consortium={c.is_consortium_member}",
        f"status={c.status}",
        f"preferred_score={c.preferred_score:.2f}",
    ]
    if c.distance_km is not None:
        parts.append(f"distance_km={c.distance_km:.0f}")
    raw = getattr(c, "raw", None) or {}
    for k in ("sla_tier", "reciprocity_balance", "on_time_rate", "holds_format", "delivery"):
        if k in raw:
            parts.append(f"raw.{k}={raw[k]!r}")
    return "  - " + ", ".join(parts)


def render_prompt(
    candidates: list[HolderCandidate],
    item: ItemMetadata | None = None,
) -> str:
    """Render the user-facing prompt body.

    The system instruction is sent separately via ADK's
    ``LlmAgent.instruction`` field; this function returns only the
    per-call payload (item + candidates).
    """
    candidate_block = "\n".join(_format_candidate(c) for c in candidates)
    return (
        f"Request item: {_format_item(item)}\n\n"
        f"Tied candidates (rules-baseline order):\n{candidate_block}\n\n"
        "Pick one symbol from the list above, or abstain (null), and "
        "give a one-sentence rationale."
    )


def system_instruction() -> str:
    """Return the system-instruction string for the ADK LlmAgent."""
    return _SYSTEM_INSTRUCTION
