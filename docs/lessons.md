# Lessons Learned

Append-only log of patterns, gotchas, and surprises encountered while
building Agora. Cheaper than an ADR (no decision being made), more
durable than a chat summary. Cite the PR/commit each lesson came from
so future readers can read the diff.

Format: dated entries, newest at the top within a section, grouped by
area. One paragraph per lesson — if it grows past that, it's probably
an ADR.

---

## Saga / ledger

### 2026-05-02 — `append()` MUST run inside a savepoint
A duplicate `idempotency_key` insert raises `IntegrityError` and
SQLAlchemy marks the **whole** transaction as failed unless the insert
ran inside a `begin_nested()` savepoint. Without the savepoint, a
benign replay (retry of an already-committed step) would roll back the
caller's outer transaction along with whatever else they were doing.
`SagaLedger.append` and `outbox_enqueue` both wrap their inserts in
`begin_nested()`; on `IntegrityError` they swallow and return the
existing row. This is the dominant correctness mechanism for
exactly-once observable effects on top of at-least-once delivery.
*(Bootstrap; see `saga/ledger.py`, `saga/coordinator.py::_enqueue_outbox`.)*

### 2026-05-02 — `saga.current_state` is a projection, not the truth
The denormalised `saga.current_state` column exists for cheap list
queries. The event stream is the source of truth. When
reconstructing context (`api._derive_extras`), walk events in `seq`
order and let later commits overwrite earlier ones — never read
`saga.current_state` and infer history from it. This came up again in
ADR-0012 when APPROVE forward stopped writing `reshare_id` on its
forward event; `_derive_extras` had to learn to read the projected
OBSERVATION as well as FORWARD events.
*(See `api/app.py::_derive_extras`, ADR-0012.)*

### 2026-05-02 — Compensators run only against committed forwards
`SagaLedger.find_committed_forward(step)` is the gate. A pending or
failed forward step has nothing to undo; trying to run a compensator
against it raises `CoordinatorError`. The API surfaces this as a 409.
Tests assert this for every step in `test_coordinator` and
`test_property_saga::_SPECS`.
*(Bootstrap; reaffirmed in PR #17 with the APPROVING-state guard that
returns 400 when `reshare_id` is unavailable.)*

---

## Outbox + idempotency

### 2026-04-30 — `outbox.idempotency_key` UNIQUE is single-column, not composite
The constraint is `UNIQUE(idempotency_key)` across the whole table —
no composite with `target`. The first time we enqueued a second
intent (NCIP fan-out on SHIP) with the same base key the insert
collided. Resolution: per-target suffix on the key
(`f"{ctx.idempotency_key}:ncip"`). If we ever add more targets per
step, every additional target needs its own deterministic suffix.
Don't be tempted to widen the constraint to `(idempotency_key, target)`
without thinking — a single key is the easier mental model for replay
("this intent, exactly once") and matches how the saga ledger keys
events.
*(PR #18 — NCIP fan-out; see `saga/flows.py::ship_forward`.)*

### 2026-04-29 — Outbox commit-then-enqueue means projection callbacks run inside the worker's session
ADR-0011 mandates the outbox row commits atomically with the ledger
event that produced it. ADR-0012 extended that contract: the
`on_success` projection callback runs **inside the same session** as
`outbox_mark_delivered`, so the OBSERVATION write and the
`delivered_at` flag commit atomically. A failed projection re-queues
the row for retry without leaving the saga half-advanced. Without
this, a projection that wrote the OBSERVATION but then crashed before
marking the row delivered would replay forever; one that marked
delivered before writing the OBSERVATION would lose the projection on
crash.
*(PR #17 — ADR-0012 implementation; see `saga/outbox.py`,
`saga/flows.py::approve_forward`.)*

### 2026-04-30 — Fire-and-forget vs gated outbox is a per-step design call
NCIP fan-out chose fire-and-forget: borrower-side ILS bookkeeping is
local, the call's result doesn't feed downstream saga state, and
adding a new lifecycle state for "awaiting NCIP ack" would buy
nothing. ReShare's `send_request` chose gated (APPROVING → APPROVED):
the supplier's `reshare_id` is required by SHIP/RETURN, so the saga
must wait. Test for "this side-effect blocks the saga" before
defaulting to gated — a stuck outbox row that staff can investigate
is often the right answer.
*(PR #18 NCIP advisor reasoning; ADR-0012.)*

---

## Tracking / advisory agents

### 2026-05-02 — Stay 2 tiers, not 3, on advisory escalation
TrackingAgent recall escalation almost grew a 3-tier severity ladder
(`warning` / `escalated` / `recall_proposed`). The middle tier added
nothing the staff console can act on differently from tier 1 — same
badge color, same lack of CTA. Collapsed to two: tier 1 = "overdue
badge", tier 2 = "recall_proposed CTA". Less code, less config, less
test surface. When sketching escalation tiers, force yourself to name
the *distinct staff action* each tier enables; if you can't, collapse
the tier.
*(PR #19; advisor flagged this before substantive work.)*

### 2026-05-02 — Recorded `days_overdue` is a snapshot, never a live counter
The first scan past `due_at` writes one OBSERVATION with a frozen
`days_overdue` value. UNIQUE on idempotency_key means the second
scan returns the existing row — you literally cannot update the
recorded value. The UI computes "currently N days" from `due_at` +
render-time clock. This is the *correct* shape: ledger events are
immutable, computed views are not. Don't try to be clever and emit a
fresh observation per scan to keep the count current — that floods
the ledger with near-duplicates.
*(PR #19; see `saga/ledger.py` UNIQUE constraint behaviour.)*

### 2026-05-02 — Agents are advisory; gate the no-outbox invariant in tests
The "scanner writes observations, never enqueues outbox intents" rule
is the cleanest expression of ADR-0005 ("default-deny autonomy") for
TrackingAgent. Test it explicitly: after a tier-2 scan, query
`OutboxRow` and assert `[]`. Otherwise a future drive-by edit could
quietly add an outbox row "to make staff's life easier" and break the
human-in-loop invariant without any test failing.
*(PR #19 — `test_overdue_scanner_recall_writes_no_outbox`.)*

---

## Type / lint surface

### 2026-04-29 — `# type: ignore` markers go stale
mypy got smarter (or a Protocol got tightened, or an attribute became
public) and several `# type: ignore[arg-type]` markers on
`MockReShareClient` became unused. mypy `--strict` flags
`unused-ignore`. Don't treat the markers as load-bearing — when
something stops needing one, *delete* it; don't add `# type: ignore[unused-ignore]`
on top.
*(PR #14 — `Extend mypy --strict to tests/`.)*

### 2026-05-02 — `payload.get("kind")` returns `Any | None`; `sorted()` rejects it
`sorted(e.payload.get("kind") for e in events)` failed under
`--strict` with `Value of type variable "SupportsRichComparisonT"
cannot be "Any | None"`. Fix: cast at the source —
`sorted(str(e.payload.get("kind", "")) for e in events)`. General
rule: when reading from `dict[str, Any]` payloads in a type-strict
context, narrow the value at the read site, not by pacifying mypy at
the comparison site.
*(PR #19 — `tests/test_tracking.py`.)*

### 2026-04-29 — mypy `--strict` over `tests/` catches contract drift
Before extending strict mode to `tests/`, several test helpers had
silently lost their type annotations during merge-conflict resolution
(`async_sessionmaker[AsyncSession]` collapsed to `async_sessionmaker`,
fixture params untyped). The strict pass found 70 errors across 9
files. Worth keeping the test directory in `mypy.files` so future
merges can't repeat the trick.
*(PR #14, PR #16 — see `pyproject.toml`.)*

---

## Workflow / process

### 2026-05-02 — Advisor before substantive work pays for itself
Two PRs in this session called advisor before writing any code (NCIP
fan-out, recall escalation). Both times advisor changed the design:
NCIP got pushed to fire-and-forget (saved a lifecycle state + a
projection callback); recall escalation got compressed from 3 tiers
to 2. "Substantive" = anything that lands in a file. Orientation
(grep, read, glob) doesn't count. Cheap rule: if your next action is
`Write` or `Edit`, advisor first.
*(PR #18, PR #19; reaffirmed by per-PR advisor pattern.)*

### 2026-05-02 — Verify CLAUDE.md known-gap claims before propagating them
The known-gap entry referenced "agora.tracking.scanner" as if it were
a module path. It's actually an asyncio task name; the module is
`agora.agents.tracking`. Caught only because advisor flagged "verify
before coding." When updating CLAUDE.md, re-check every code-shaped
token in the bullet you're about to rewrite — don't trust the
existing prose.
*(PR #19 — CLAUDE.md tracking section.)*

### 2026-04-30 — Propagating handler return values is cheap symmetry insurance
`make_ncip_handler` originally discarded its call result because no
projection consumes it. PR #12 unified it with `make_reshare_handler`
to return the result anyway. Cost: one `return`. Benefit: a future
`make_ncip_on_success` opts in by registering a callback, no surgery
on the handler. When in doubt, plumb the value through and let the
caller decide.
*(Commit `2a3afd7`.)*

### 2026-04-29 — GPG-signed commits + pinentry timeouts hang the agent
Bootstrap commit timed out three times waiting for pinentry. Don't
bypass signing without explicit user permission — leave the work
staged and surface the blocker in the summary. CLAUDE.md now
documents the rule explicitly.
*(Bootstrap; see `MORNING_SUMMARY.md`.)*

---

## Schema / migrations

### 2026-04-29 — `Base.metadata.create_all()` for tests, Alembic for production
SQLite tests use `create_all()` directly; Postgres uses Alembic
migrations. The Alembic path has *never* been tested against a real
Postgres in CI. Every new column or table needs (a) a new revision
in `alembic/versions/` AND (b) the ORM in `saga/db.py` updated. Do
not rely on `create_all()` to "just work" — the column/index DDL it
emits is not what Alembic emits. Backlog item: stand up a
real-Postgres Alembic test.
*(Known-gap entry in CLAUDE.md.)*

### 2026-04-29 — Lifecycle column is `VARCHAR`, so adding a state needs no DDL
ADR-0012 prep added `LifecycleState.APPROVING`. The Alembic revision
is empty — no `ALTER TYPE` because the underlying column is varchar,
not a Postgres ENUM. We get the no-op revision anyway as a marker so
the schema-version column tracks the lifecycle change. If this ever
moves to a real ENUM, every state-add needs `ALTER TYPE ... ADD VALUE`.
*(PR #16; see `alembic/versions/20260503_approving_state_marker.py`.)*

---

## Security tooling

### 2026-05-04 — Audit pass: `# nosec` annotations age, must be re-justified each run
Smoke-testing the security-audit skill (backlog #6) found 4 nosec'd
lines: 2 legitimate (mypy narrowing in `clients/sru.py`, dev-default
`0.0.0.0` bind in `config.py`) and 2 carrying the rationale "ledger.append
never returns None in practice" at `saga/coordinator.py:175` and `:253`.
The PR #26 audit pass framed this as a live bug ("guard above is the
proof, replay path crashes"); the PR #27 fix surfaced that **the audit
itself was a misread**. `ledger.append` actually has *never* returned
None: the IntegrityError branch returns the existing event row (see
`tests/test_ledger.py::test_replay_returns_existing_event_not_none`).
The nosec rationale was true; the `if persisted is not None:` guard
9 lines above was dead code; the asserts never crashed. The real defect
was API-contract drift — three layers (signature `-> SagaEvent | None`,
docstring "returns None on replay", impl returning the existing row)
disagreed. Audit takeaway is unchanged and still load-bearing: **every
audit pass should grep `nosec` and re-justify each annotation against
current code, not trust the comment** — but the justification can also
land on "the surrounding code lies; tighten it" rather than "the nosec
lies; remove it." Fix in PR #27 tightened the signature to
`-> SagaEvent`, removed the dead guards + redundant asserts, and added
the replay-returns-existing test to pin the contract.
*(PR #26 audit; PR #27 fix — see `src/agora/saga/coordinator.py`,
`src/agora/saga/ledger.py`, `tests/test_ledger.py`.)*

### 2026-05-04 — Bundled `security_scan.py` runner needs `sys.executable -m`
Upstream `wdm0006/python-skills/security-audit/scripts/security_scan.py`
shells out to `["bandit", ...]`, `["pip-audit", ...]`, etc. — bare
PATH lookup. On a venv-only install (Windows `.venv\Scripts\`, or any
host where the scanners aren't on system PATH) every check returns
`FileNotFoundError: bandit not installed` and the skill silently fails
"clean." Patch: invoke via `[sys.executable, "-m", "bandit", ...]`,
`[sys.executable, "-m", "pip_audit", ...]`,
`[sys.executable, "-m", "detect_secrets", ...]`. Same trick worth
remembering for any other cherry-picked runner. While there: dropped
the `safety` branch (package unmaintained, pip-audit covers same DB)
and noted in SKILL.md "Bundled scripts" that the modifications mean
the upstream "unmodified" framing no longer applies.
*(PR #26 — see `.claude/skills/security-audit/scripts/security_scan.py`,
`.claude/skills/security-audit/SKILL.md`.)*

### 2026-05-02 — Bandit nosec needs the **two-hash** form
`# nosec B101 - reason` silently parses every word of the reason as a
test ID and floods stderr with `WARNING Test in comment: <word> is not
a test name or id, ignoring`. The actual suppression still works (the
B101 token is recognised) but the reason text is lost. Correct form
is `# nosec B101  # reason` — the second `#` re-opens a comment so
bandit ignores everything after it. General rule for nosec: one
space, the test ID(s), two spaces, second `#`, then prose. Anything
else and bandit treats your justification as more test IDs.
*(PR #21 — see `saga/coordinator.py`, `clients/sru.py`, `config.py`.)*

### 2026-05-02 — `detect-secrets` baseline is hash-based; commit it from day one
First-run flow is `detect-secrets scan > .secrets.baseline` followed
by `git add .secrets.baseline`. Without a baseline, every key-shaped
literal in the repo (dev defaults like `agora:agora@localhost`, AWS
example keys in docs) flags on every CI run. With a baseline,
`detect-secrets-hook` only fails on findings whose hash is not
already accepted — so credential rotation produces a NEW hash and is
correctly caught. The corollary: the baseline is not "noise the gate
ignores forever," it's a hash-pinned allowlist that breaks on any
real change.
*(PR #21 — see `.secrets.baseline`, `Makefile::audit`, `.github/workflows/audit.yml`.)*

### 2026-05-02 — Per-line `# nosec` beats global `[tool.bandit]` skips
Tempting to add `skips = ["B101"]` to `pyproject.toml` and move on —
4 findings, 1 line of config. Don't. The "why was this OK?" lives
at the call site (mypy narrowing in `sru.py`, post-condition
assertion in `coordinator.py`, dev-default bind in `config.py`); a
global skip drops it on the floor and makes future drive-by edits
unreviewable. Per-line `# nosec` with prose costs 4 lines and keeps
each justification glued to the code. Reserve global skips for
findings whose rationale is *the same everywhere they appear* (we
don't have any of those today).
*(PR #21 — `pyproject.toml::[tool.bandit]` keeps only `exclude_dirs`.)*

### 2026-05-02 — `git ls-files | xargs` is space-fragile; use `-z | xargs -0`
Both Makefile and CI workflow originally piped `git ls-files | xargs
detect-secrets-hook`. Today the agora tree has no filenames with
spaces so it works — but the standard hardening is one character:
`-z` on the producer, `-0` on xargs. Same applies to `$(shell git
ls-files)` in make recipes (whitespace-splits). Cheap insurance;
do it the first time.
*(PR #21 — see `Makefile::audit`, `.github/workflows/audit.yml`.)*

---

## Convention reminders (collected here so they don't drift out of CLAUDE.md)

- All datetimes are timezone-aware UTC (`datetime.now(UTC)`). When
  parsing inbound ISO strings, `_parse_iso` defaults `tzinfo=UTC` if
  the string was naive. Don't compare a naive `datetime` to an
  aware one — Python raises.
- Idempotency keys are ULIDs with semantic prefix
  (`route_01HXY...`). Use `new_idempotency_key(prefix=...)`. Don't
  hand-roll a UUID and call it an idempotency key — the prefix is the
  human breadcrumb in the ledger.
- BIGINT autoincrement PKs use the `_bigint_pk()` helper because
  SQLite needs `Integer` for rowid behaviour and Postgres needs
  `BigInteger` for production. Don't paste `BigInteger, primary_key=True`
  raw — SQLite tests will mis-behave.
- DB UUID columns use the `_PortableUUID` TypeDecorator so the same
  ORM works on both backends.

---

## How to add a lesson

When you finish a PR, ask: *did anything bite me that wasn't obvious
from the spec?* If yes, append a dated entry to the relevant section
above. One paragraph. Cite the PR/commit. Keep the tone "future me
will thank present me" — concrete, specific, and tied to a code
location. If the lesson is really a *decision*, write an ADR instead.
