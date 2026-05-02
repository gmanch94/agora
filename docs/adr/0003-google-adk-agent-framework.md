# ADR 0003 — Google ADK as agent framework

**Status:** Accepted
**Date:** 2026-05-02

## Context

Need an orchestration framework for the multi-agent layer. Candidates
considered: Google ADK (Agent Development Kit), LangGraph, raw Anthropic
SDK with custom orchestration, CrewAI, AutoGen.

Key requirements:
- Tool-using agents with structured output
- Local development without cloud lock-in
- Eval harness for testing agent recommendations
- Reasonable production deployment story when graduating from prototype
- Continuity with existing project (kroger-shopping-agent uses ADK)

## Decision

Use **Google ADK** for agent orchestration. Ship agents as ADK agents
with declared tools, run locally via the ADK CLI in development, deploy
to Agent Runtime / Cloud Run if the prototype graduates.

Important constraint: agents are **advisory only** in this prototype.
Their job is to produce a recommendation + rationale into the saga
ledger; the saga coordinator (plain Python) decides when to call them.
This means we are **not** using ADK's autonomous loops or sub-agent
hand-off — every agent is invoked once per saga step, with a tightly
scoped task.

## Consequences

**Positive**
- Familiar pattern (matches kroger-shopping-agent) — reuse code patterns,
  eval harness, deployment recipes from prior project.
- ADK provides: tool definitions, structured outputs, eval framework,
  Cloud Trace integration, Agent Runtime as a deploy target.
- Constraining agents to single-shot advisory mode keeps reasoning
  bounded and auditable.

**Negative**
- ADK is GCP-flavored; deploying off-GCP requires more manual work.
- ADK is younger than LangGraph; some patterns less established.
- Single-shot mode wastes some of what ADK offers (multi-agent loops);
  but that's by design for safety.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| LangGraph | Strong, but no continuity with existing project; eval story less mature |
| Raw Anthropic SDK | Most flexible but every primitive (tool routing, eval) hand-built |
| CrewAI | Opinionated patterns don't match our advisory + saga gate model |
| AutoGen | Conversational multi-agent style is wrong for our state-gated workflow |

## Implementation note

Each agent module in `src/agora/agents/` defines an ADK `LlmAgent` (or
plain function for the rule-only PolicyAgent), invoked by the saga
coordinator with a single call. Agent prompts are templated with the
saga context; outputs validated against pydantic schemas before being
written to the ledger.
