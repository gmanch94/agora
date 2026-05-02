# ADR 0004 — Python 3.11+ / FastAPI / Postgres stack

**Status:** Accepted
**Date:** 2026-05-02

## Context

Need to choose runtime, web framework, and DB for the Agora layer
(distinct from ReShare which runs on its own JVM stack).

## Decision

- **Language:** Python 3.11+ (matches ADK; matches kroger project).
- **Web framework:** FastAPI for the staff console API.
- **Async runtime:** stdlib asyncio; httpx for clients.
- **DB:** Postgres 15+. SQLAlchemy 2.x async + asyncpg driver, plus
  Alembic for migrations.
- **Validation:** pydantic v2.
- **Test:** pytest + pytest-asyncio + Hypothesis (property-based tests
  for saga primitives).
- **Lint/type:** ruff, mypy strict on `src/`, ruff format.
- **Packaging:** pyproject.toml (PEP 621), `uv` or `pip` to install.

## Consequences

**Positive**
- Aligned with Google ADK (Python).
- FastAPI: async-native, pydantic-integrated, OpenAPI for free.
- Postgres: matches ReShare's own stack — easier dev parity.
- pydantic v2: fast validation, schema generation for ISO 18626 subset.

**Negative**
- Mixed-language stack with Java-based ReShare (acceptable; well-defined
  REST seam).
- Async correctness requires care — easy to introduce subtle bugs in
  saga code. Mitigation: small pure-sync core for ledger ops, async only
  at I/O boundaries.

## Alternatives considered

| Alternative | Reason rejected |
|-------------|-------------------|
| Go | Strong concurrency but no ADK SDK, less LLM ecosystem |
| Node/TypeScript | OK choice but less aligned with ADK + prior project |
| Django | Heavier than needed; FastAPI's async-first model is a better fit |
| SQLite | Fine for prototype dev but ReShare uses Postgres; better to match |
