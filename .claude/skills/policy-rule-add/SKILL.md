---
name: policy-rule-add
description: Add a new rule to `PolicyAgent` (CONTU-style copyright, patron eligibility, budget, ISO 18626 cost cap, etc.) with consistent code, tests, and rationale-string format. Use when extending `src/agora/agents/policy.py`.
---

# policy-rule-add

`PolicyAgent` is the single chokepoint for "is this request legally /
financially / contractually OK to fulfil". Adding rules ad-hoc leads
to inconsistent flag codes, missing tests, and rationale strings that
don't render in the staff console. This skill keeps the shape uniform.

## When to invoke

- User asks to add a policy rule (e.g. "block requests from libraries
  over their monthly cap", "warn when ISBN is on a denylist", "flag
  if patron has overdue items")
- Reviewer flags a missing pre-flight check
- Adding a new `CopyrightLedgerEntry`-style data source

## Required information

1. **Rule name** — short snake_case, e.g. `denylist_isbn`,
   `overdue_threshold`, `subscription_tier`.
2. **Flag code** — what `PolicyFlag.code` will be (kebab-case is
   canonical for codes used in the staff console — match existing:
   `contu_violation`, `patron_suspended`, `budget_exceeded`).
3. **Hard or soft?** — hard flags block the forward step even on
   staff approve (require explicit override + rationale in ledger).
   Soft flags warn and let staff click through.
4. **Inputs** — what does the rule read? Patron registry? A new ledger
   table? The request itself?
5. **Trigger condition** — when does this rule even apply? (e.g.
   CONTU only applies to `RequestType.COPY` with an ISSN.)
6. **Test cases** — at minimum a positive (rule fires) and a negative
   (rule does not fire on adjacent input).

## Files to touch

### 1. `src/agora/agents/policy.py`

- If the rule needs new state, add a `@dataclass(slots=True)` for it
  next to `CopyrightLedgerEntry`.
- Add a constructor parameter to `PolicyAgent.__init__` with a
  sensible default (`None` → empty list/set, then assign in body).
- Add a private predicate method `_<rule_name>(self, request) -> bool`
  next to `_violates_contu` and `_is_suspended`.
- Add the call site in `PolicyAgent.run`, gated on the trigger
  condition. Append a `PolicyFlag` with the agreed code, message, and
  `is_hard` setting.
- Update `_make_rationale` only if the rule needs a custom phrasing
  (usually not — the default `code1, code2` join is fine).

### 2. `tests/test_agents.py`

- Add `test_policy_<rule_name>_fires_when_<condition>`.
- Add `test_policy_<rule_name>_does_not_fire_when_<negation>`.
- Reuse the `_request(...)` helper at the top of the file.
- For state-bearing rules, construct the seed data inline (don't
  build a fixture unless three+ tests share it).

### 3. `docs/prd/02-agents.md`

- Add a row to the PolicyAgent rules table with: rule name, code,
  hard/soft, trigger, source data.

### 4. (Conditional) `alembic/versions/<new>.py`

- Only if the rule reads a new persistent table.
- If the rule reads an in-memory structure for now (prototype-style),
  skip and note "in-memory; promote to Postgres when productionising".

## Conventions to enforce

- Datetime: always `datetime.now(UTC)`. Never bare `datetime.now()`.
- Flag codes are stable identifiers — once shipped, don't rename. Add
  a new code if the meaning changes.
- Hard flags MUST have an `is_hard=True` argument and a clear
  `message` explaining the legal/policy rationale (this string ends
  up in the saga ledger and may be read in audit).
- Predicate methods return `bool` and have no side effects — pure
  reads only. Side-effecting flags belong in agents downstream of
  policy (e.g. PolicyAgent flags overdue, TrackingAgent acts on it).
- New rules default to **soft** unless there's a citable
  legal/contract reason for hard. Default-soft preserves the
  human-in-loop spirit (ADR-0005).

## Output

1. Summarise the rule in plain English (1-2 sentences).
2. Show the proposed code diff.
3. Show the proposed test diff.
4. Show the PRD diff.
5. Ask go/no-go.
6. On approval: apply, run `pytest tests/test_agents.py -q` and
   `ruff check src tests`, report results.

## Don'ts

- Don't add a rule without tests.
- Don't put PolicyAgent on the network (HTTP / DB connection per
  call). It runs in the request path; rule data should be in-memory
  or eagerly loaded.
- Don't conflate policy with routing. Policy answers "should we?".
  Routing answers "from whom?". A rule like "prefer consortium
  suppliers" goes to RoutingAgent, not here.
