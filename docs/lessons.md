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

### 2026-05-03 — Adding an independent scanner tier breaks tests that share fixtures
PR #39 added tier-3 (`receipt-unconfirmed-{saga_id}`) to
`OverdueScanner` alongside the existing tier-1 (`overdue`) and
tier-2 (`recall-proposed`) emissions. The seed helper
`_seed_shipped_saga(due_at=...)` defaults `shipped_at` to
`due_at - 28 days`, which means almost every existing test has a
saga shipped well past tier-3's 7-day threshold. Two tests with
strict assertions (`records == []` and a sorted-list comparison on
emitted observation kinds) failed because tier-3 fired alongside
their tier-1/2 scenarios. Fix wasn't a bug — both tests were
correct, the new tier was correct; they just happened to share
fixtures whose default values now triggered an unrelated emission.
Generalised: **when you add an independent code path that gates on
a field already populated by shared test fixtures, audit every
existing test that uses those fixtures.** Either pin the new
threshold high (`unconfirmed_receipt_after_days=999`) to opt out of
tier-3, or extend the helper to take the new field as an explicit
arg (we did both). Caught locally before push by running the
existing test suite first; the alternative — pushing and watching
CI catch it — costs an extra round trip.
*(PR #39 — see `tests/test_tracking.py::_seed_shipped_saga`,
`test_overdue_scanner_skips_not_yet_due`, and
`test_overdue_scanner_emits_recall_proposed_past_threshold`.)*

### 2026-05-03 — Re-anchoring a side effect can obsolete prior state-aware logic
PR #37 wired a state-aware NCIP rollback into the SHIP compensator
(emit `check_in` from `SHIPPED`, skip from `RECEIVED`) so the ILS
record matched physical reality after a recall. One PR later, the
NCIP `check_out` anchor moved from SHIP forward to RECEIVE forward —
the cleaner circulation-timing model — and the SHIP comp's
state-aware branch became dead code. Walking both branches under
the new anchor: at SHIPPED no ILS loan was ever opened (RECEIVE
never ran, so no `check_out` dispatched), at RECEIVED the patron
holds the book (loan correctly reflects custody, return flow owns
`check_in`). Both branches converge on "just recall." The
`current_state` check survives only as state-aware rationale text.
Generalises: state-aware compensator branches often signal an
upstream design tension. When a future PR removes the tension, ask
whether the branches still earn their keep — don't preserve them
out of habit. PR #37's logic was correct given its anchor; this
PR's deletion is also correct given the new anchor. Both versions
shipped in the same week.
*(Backlog item: NCIP `check_out` re-anchor SHIP→RECEIVE — see
`saga/flows.py::receive_forward` + `ship_compensator`,
`tests/test_coordinator.py::test_ship_compensator_from_{shipped,received}_emits_recall_only`,
`tests/test_coordinator.py::test_receive_forward_advances_to_received`.)*

### 2026-05-03 — Compensator NCIP rollback is state-aware, not boolean
*(Superseded for SHIP comp specifically by the NCIP-checkout
SHIP→RECEIVE re-anchor — see entry above. The general lesson still
applies: design compensators by asking "what does the other system
believe right now?" not "what did the forward send?" The state-aware
SHIP comp branch was correct under the old anchor; this re-anchor PR
deletes it as dead code rather than amending it.)*

The first instinct when wiring SHIP-compensator NCIP rollback was
"emit `check_in` if the SHIP forward emitted `check_out`" — i.e. a
forward-mirror. That's wrong. The right question is "what does the
ILS record show *right now*, and is that record correct?" Today's
SHIP forward anchors `check_out` to supplier-shipped (not
borrower-receipt — known-gap), so at saga state `SHIPPED` the ILS
shows a loan that hasn't physically happened — clearing it on recall
is a true rollback. At `RECEIVED` the ILS shows a loan that *has*
happened — clearing it would lie about current custody, since the
patron still holds the book; the eventual return flow owns the
`check_in`. The compensator branches on `ctx.current_state` and emits
the rollback only from `SHIPPED`. Generalises: when designing a
compensator with side effects in another system, ask "what does that
system believe right now, and what should it believe after the
recovery?" — not "what was sent forward?"
*(Backlog #4 — see `saga/flows.py::ship_compensator`,
`tests/test_coordinator.py::test_ship_compensator_from_*`,
ADR-0011 + outbox-UNIQUE notes for the `:ncip-rollback` suffix.)*

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

### 2026-05-04 — `"".split(",")` is `[""]`, not `[]` — strip-and-filter every CSV env-var
`AGORA_CONSORTIUM_MEMBERS=""` naively split would mark the empty
string `""` as an in-consortium symbol — a phantom roster entry that
matches nothing but is technically present. Same for trailing-comma
forms (`"A,"` → `["A", ""]`) and pure-comma forms (`","` → `["", ""]`).
Idiom for any env-var-as-set property: `{tok.strip() for tok in raw.split(",") if tok.strip()}` —
strip per token, filter empties. Pinned by 5 unit cases in
`tests/test_factories.py::test_consortium_members_*`, including the
trailing-comma trap (`",", " , , "`) which is the easy mistake in
`.env` files. Generalises to any place we tokenize user-supplied
delimited input — don't trust split alone.
*(PR #56 — see `Settings.consortium_members` in `src/agora/config.py`.)*

### 2026-05-04 — Dynamic module load needs a typed alias to pacify mypy
`scripts/validate_iso18626.py` is loaded by `importlib` so the test
file isn't bound by package layout. `module.validate` typed as
`Any` infects every downstream call site. Wrapping with a typed
alias (`fn: _ValidateFn = module.validate; return fn`) restores
typing without making the test ugly. Generalises to any
"plugin-style" loader where the module isn't on the import path
ahead of time.
*(PR #52 — see `tests/test_iso18626_validation.py::_load_validate`.)*

### 2026-05-04 — `@pytest.mark.parametrize` with an empty list emits a collection warning even under `skipif`
The real-XSD test variants `parametrize` over a list whose contents
depend on whether the XSD was cached. When the cache is missing the
list is empty and pytest emits a collection-time warning *even when
`skipif` would have skipped the test.* Fix was to invert the shape:
loop inside the test body and call `pytest.skip()` per iteration so
collection sees a populated parametrize. Cleaner than a global skip
marker that mutes everything.
*(PR #52.)*

### 2026-05-03 — pydantic-settings constructor takes ALIASES, not field names
`Settings(reshare_base_url="x")` returns a Settings object with
`reshare_base_url == ""` — silently ignored. The field is declared
`Field(default="", alias="RESHARE_BASE_URL")` and our `model_config`
sets `extra="ignore"`, so unknown kwargs (the field name) are
discarded without error. Only the alias works:
`Settings(RESHARE_BASE_URL="x")`. Tests that construct `Settings`
directly must use the alias form. Caught while writing the
`OkapiAuth` integration tests — a quick repl check (`s = Settings(
reshare_base_url="x"); print(s.reshare_base_url)`) showed the empty
default coming through. Either add `populate_by_name=True` to
`model_config` (allows both forms) or always use the alias; for now
we use the alias because it matches how env vars look in CI logs.
*(PR #34 — `tests/test_okapi_auth.py::test_reshare_client_picks_okapi_when_url_set`.)*

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

### 2026-05-04 — README drifts harder than any other doc
The README is the first-impression doc but sits outside every
feature PR's natural blast radius — PRDs get touched when their area
changes, runbook gets touched when env vars / endpoints change, but
README only gets reviewed when somebody opens the repo cold and
notices the mismatch. The 2026-05-04 README refresh found 21 PRs of
accumulated drift: missing `Received` lifecycle state (shipped #36),
test count 76 → 210, ADR count 12 → 14, missed three-tier scanner /
NCIP fan-out / DiscoveryAgent endpoint / routing LLM tie-breaker /
ISO 18626 harness in the Status section, missing CrossRef row in the
standards table, missing `evals/` + `scripts/` in the layout block.
**Generalises:** schedule an explicit README review when *any* of
(a) test count changes by ≥5, (b) ADR count changes, (c) lifecycle
states change, (d) standards/external-system list changes, (e) Status
section claims tied to "latest shipped." Otherwise it's drift on
autopilot until the next first-impression visitor.
*(PR #62.)*

### 2026-05-04 — Symmetry tests need both directions AND both axes (keys + values)
PR #59 added forward + reverse key-symmetry between `.env.example`
and `Settings`; PR #60 did the same for the runbook env-var table.
Two PRs, four tests, full coverage on the *keys* axis. Then PR #65
added a *value*-symmetry test for `.env.example` and PR #66 did the
same for the runbook. Two more PRs, two more tests. Only with all six
in place does the symmetry actually hold against the historical
failure mode that triggered this work — the routing-LLM ε drift
through PRs #47-#51, where the runbook said `0.05` / "Placeholder
until PR-2b tunes against eval" while `Settings` was tightened to
`0.03` in #51 and `.env.example` had a stale 0.05 too. The keys all
matched (Settings.routing_tiebreak_epsilon was always there); only
the *values* lied. **Generalises:** a symmetry claim has at least two
axes — *which entries exist* and *what each entry says*. A test that
covers only one axis catches half the drift modes. When you reach for
a symmetry pytest, ask: do I need keys-only, values-only, or both?
For three-artifact contracts (Settings + dev-template + ops-doc) you
typically need both, on each pair, in each direction — six tests
total. lessons.md PR #58 captured the meta-lesson "operationalise
symmetry claims via pytest"; this entry refines it: don't stop at one
axis.
*(PRs #59, #60, #65, #66 — see `tests/test_config.py`; CLAUDE.md
behavioural rule "When adding a new ``Settings`` field, three
artifacts must agree" added in PR #67.)*

### 2026-05-04 — Operationalise symmetry lessons via pytest, not just lessons.md prose
PR #58 captured the lesson "symmetry claims between artifacts need a
CI check or they're aspirational." That paragraph by itself didn't
prevent the next drift — it only described the failure mode. PRs #59
and #60 turned the lesson into two pytest cases (each both directions:
.env.example ↔ Settings, runbook table ↔ Settings) so the next time
somebody adds a `Settings` field without touching the docs, CI fails
with the exact missing keys. **Generalises:** when a lesson describes
a *mechanical* invariant (key sets, naming conventions, file locations
that must agree), prefer test-as-enforcement over prose-as-warning.
The lesson stays useful as historical context, but the pytest is what
actually gates future PRs. lessons.md is for non-mechanical gotchas
(state-aware logic, prompt polarity, model-id confusion) where a test
would be expensive or impossible.
*(PRs #59, #60 — see `tests/test_config.py`; lesson cited PR #58.)*

### 2026-05-04 — `.gitignore` session-scratch artifacts belt-and-braces
`NEXT_SESSION.md`, `RECOMMENDATION.md`, `DOCS_STALE_PUNCHLIST.md`,
`DESIGN.md` are agent-written planning notes that never need to land
in git. Repo root makes them easy to grep but also easy to land via a
careless `git add .`. After 6 PRs in one working session, `git status`
showed 5 untracked files in the root — clearly enough exposure to
warrant a guard. Adding them to `.gitignore` even though they're
already untracked closes the only failure mode (an accidental wide-add
that hits the parent directory). Same trick for `.claude/settings.local.json`
which is per-dev local Claude scope.
*(PR #61 — see `.gitignore` § Session-scratch planning artifacts.)*

### 2026-05-04 — `.env.example` drifts silently unless the runbook claim is enforced
The runbook env-var table § Configuration says ".env.example in the
repo lists the same set." That invariant had been silently broken for
~13 PRs (OKAPI_URL #34, AGORA_SRU_ENABLED / AGORA_CROSSREF_ENABLED #46,
all the AGORA_TRACKING_* vars, every AGORA_ROUTING_LLM_* var,
AGORA_CONSORTIUM_MEMBERS #56) because nobody was checking. Adding env
vars to `Settings` is part of every feature PR; updating `.env.example`
is not muscle memory. Fix: PR #57 backfilled the missing 17 rows with
self-documenting comments and pointed the file's header at the runbook
table as canonical. **Generalises:** when a doc claims symmetry between
two artifacts, either wire a CI check (script that diffs `Settings`
field set vs `.env.example` keys) or expect drift. Symmetry claims
without enforcement are aspirational, not normative.
*(PR #57 — see `.env.example` header pointer.)*

### 2026-05-04 — Quality gates without committed numbers are just vibes
Initial draft of ADR-0014 (routing eval gate) had only a qualitative
hurdle: "PR-2 must beat the rules baseline." Advisor pushback: the
gate is empty until somebody runs the harness and commits numbers.
Resolution: run the harness against rules-only first, commit
`evals/routing/baseline.json`, quote the numbers in the ADR (top-1
0.8000 / mean Spearman 0.5556) so a regression shows up in the diff.
The scaffolding has to load before the LLM PR exists; otherwise the
LLM PR is rebuilding both the metric and the floor at the same time
and nobody can tell what shipped what.
*(PR #47 — see `evals/routing/baseline-rules.json`,
`docs/adr/0014-routing-llm-eval-floor.md`,
`.github/workflows/routing-eval-floor.yml`.)*

### 2026-05-04 — Eval harnesses don't live under `tests/`
Pytest auto-collects everything matching `test_*.py` under
`testpaths`. Wiring real LLM calls into that path makes CI slow,
costly, and flaky on the day someone touches infra. The eval harness
itself ships as code under `src/agora/evals/routing.py` (still
mypy-checked, bandit-scanned), data lives at top-level
`evals/routing/`, invocation is `make eval-routing`. A small
synthetic-fixture plumbing test (`tests/test_eval_harness.py`)
keeps the *harness* covered by pytest without dragging the full
eval set in. Generalises: **eval harnesses are on-demand quality
artefacts, not unit tests** — keep them next to docs, not next to
pytest.
*(PR #47.)*

### 2026-05-04 — When an "improvement" requires external state, split along that state boundary
Original PR-2 scope was "wire LLM tie-breaker prompt + ADK call + ε
threshold + fallback + eval rerun + CI gate" — one PR. Advisor:
the eval rerun requires a real Gemini call. Without one, the
adapter ships but the baseline doesn't move; the gate is unchanged.
Resolution: PR-2a (seam + Mock + tests + ADR/docs split) is a pure
*structural* change — provable green without any external service.
PR-2b (real adapter + prompt + eval rerun + new committed baseline
+ CI gate) is the *intelligence* change. Otherwise PR-N looks
identical to PR-N-1 in metrics and you can't attribute outcomes.
*(PRs #48 / #49.)*

### 2026-05-04 — Verify discriminator gaps fit ε before authoring scenarios
PR-2's eval set adds four scenarios designed to invert the rules
baseline. Advisor pre-flight: "compute the rules score gap for each
before writing — anything past ε is out of scope for the
tie-breaker mechanism." Result: routing-013/014/016 all had gap
0.0 (true ties — in scope), but routing-015's gap was 0.46 — no ε
can ever make the LLM fire. Right answer: keep the scenario AND
mark it explicitly out-of-scope in the ADR, raising PR-2b's top-1
ceiling to 19/20 (0.95). Generalises: when an eval scenario sits
upstream of a mechanism's discriminator, it's vapor coverage —
document the discriminator constraint before authoring.
*(PR #48 — see `docs/adr/0014-routing-llm-eval-floor.md` § Out of
scope, scenarios `routing-013..016` in `evals/routing/scenarios.yaml`.)*

### 2026-05-03 — Sanity-check new test coverage by deleting the registration
Adding `RECEIVE` step + state — the lifecycle-extend skill calls for
"happy-path test that drives the saga through the new step." It's
possible to write that test in a way that *passes whether or not
the new step is actually wired*: e.g. if the saga state asserts only
on the final `RETURNED`, and the loop happens to skip RECEIVE, the
test stays green for the wrong reason. Advisor's check from PR #34
generalised here: after writing the test, **temporarily delete the
new registration** (e.g. comment out `reg.register(name=StepName.RECEIVE,
...)`) and re-run. If the test still passes, it isn't exercising the
new step. Done in this PR by scripting the deletion + restoration —
3 of 3 RECEIVE tests failed without the registration, 3 of 3 passed
with. Same shape as the lock-removal sanity check; same generic rule:
prove the negative before trusting the positive.
*(Backlog #3 RECEIVE state PR — `tests/test_coordinator.py::test_receive_*`,
`src/agora/saga/flows.py::_wire`.)*

### 2026-05-03 — Verify your concurrency tests actually exercise the lock
PR #34 added `OkapiAuth` with an `asyncio.Lock` around token
acquisition + a test asserting "5 parallel requests = 1 login."
The test passed. But a single-threaded asyncio scheduler can pass
that assertion *without* the lock under some scheduling — if the
first task completes login + assigns `self._token` before any
sibling task even reaches the lock-acquire, the double-checked
cache hides the bug. Forced contention by adding `await
asyncio.sleep(0)` inside the login handler, then sanity-verified
the test by removing the lock from a copy of the auth flow and
re-running: 5 logins instead of 1. The test catches the
regression. **Rule:** for any concurrency test, prove the negative
(temporarily break the primitive) before declaring the positive
test sound.
*(PR #34 — `tests/test_okapi_auth.py::test_concurrent_requests_share_single_login`.)*

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

### 2026-05-03 — Vertex API enablement ≠ Gemini publisher-model access
PR-2b's `--llm` eval rerun was attempted with ADC bound,
`aiplatform.googleapis.com` enabled on the quota project, and
`GOOGLE_GENAI_USE_VERTEXAI=1` + project + location all set
correctly. Every call returned **Vertex 404 NOT_FOUND** on
`gemini-2.0-flash` AND `gemini-1.5-flash`:

```
'Publisher Model `projects/<project>/locations/us-central1/
publishers/google/models/gemini-2.0-flash` was not found or your
project does not have access to it.'
```

Three things have to all be true to talk to a Gemini publisher
model on Vertex: (1) ADC bound to a quota project, (2) Vertex API
enabled, (3) **Vertex AI Studio access** has been click-through
enabled on the project (a one-time consent prerequisite, separate
from API enablement). Unblocked by opening
https://console.cloud.google.com/vertex-ai/studio with the bound
project selected and clicking through. **Don't conflate "Vertex
API enabled" with "I can call Gemini" in session-bootstrap docs
or runbooks.** *(Resolved post-PR-2b in the same session; eval
rerun shipped against `gemini-2.5-flash`.)*

### 2026-05-03 — Studio model display labels are NOT API model IDs
Once Vertex AI Studio access was enabled, the user verified the
flow by chatting with the Studio model labeled
`gemini-3.1-flash-lite-preview`. Setting
`AGORA_ROUTING_LLM_MODEL=gemini-3.1-flash-lite-preview` for the
API call still returned 404 — Studio renders friendly display
labels (often preview / latest tags) that don't map 1:1 to public
API model IDs. The standard 1st-party API IDs are what you set in
the SDK / payload (`gemini-2.5-flash`, `gemini-2.5-pro`,
`gemini-2.5-flash-lite`, etc.). `gcloud ai models list
--region=us-central1` only lists fine-tuned / custom models — it
will NOT list 1st-party publisher models, so it's not a discovery
tool for picking an API id. Reference:
https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions
*(Cost: ~10 minutes of trying Studio labels against the API and
getting 404s before pivoting to standard IDs.)*

### 2026-05-03 — Prompt polarity bugs survive happy-path tests; only the eval set catches them
PR-2b shipped a tie-breaker prompt with **reciprocity polarity
backwards**: it told the LLM "positive number = consortium owes
this lender, prefer the more-negative balance" while the
`evals/routing/scenarios.json` convention labels NEGATIVE balances
as the consortium owing the lender (i.e. the lender we should
AVOID re-borrowing from). Every `test_routing_*` happy-path /
contract test passed because they mock the LLM at the
`_invoke_model` boundary — they never check that the prompt
actually says the right thing. The bug only surfaced in the LLM
eval rerun, where `routing-014` came back picking MEM-A (the
in-debt member, expected MEM-B). #7c flipped the polarity in
`src/agora/agents/routing_tiebreak_prompt.py`; eval lifted to
**0.9500 / 0.8889** (was 0.8500 / 0.6944). **Two takeaways:**
(1) prompt semantics must align 1:1 with scenario labelling
conventions — describe the convention in the prompt body so a
future scenario author can sanity-check both halves at once;
(2) for any tunable prompt, the eval set is the only test that
catches semantic drift. Mock-based unit tests verify wiring, not
content. *(Cost: shipped one cycle of regression in PR-2b that
#7c had to recover.)*

### 2026-05-03 — Tighten ε *only* after computing gaps for ALL scenarios
PR-2b's first-cut ε=0.05 silently fired the LLM on `routing-009`
(rules top-2 gap 0.0467) — rules picked correctly, LLM picked
worse. The instinct was to leave ε generous "in case the LLM
helps." The right move is the opposite: tighten ε to the smallest
value that still admits the scenarios the LLM is hired to
disambiguate. #7c dropped 0.05 → 0.03 after a one-liner that
computed gaps for all 20 scenarios; this excluded 007 (gap 0.04),
009 (0.0467), 011 (0.04) — all of which rules already get right —
and kept 013 / 014 / 016 (all true ties at gap 0.0). **Run this
gap-blast-radius check before any ε change.** The advisor
flagged this exact omission pre-flight; without that pass we
would have shipped an ε that tried to "be permissive" and instead
let the LLM dilute correct rules picks. *(See
`src/agora/config.py` — `routing_tiebreak_epsilon` default
documents the pin.)*

### 2026-05-03 — `gemini-2.5-flash` cold-start exceeds 5s default timeout
First call against `gemini-2.5-flash` from the smoke test hit
`AGORA_ROUTING_LLM_TIMEOUT_SECS=5` (the documented default) and
raised `TimeoutError`. Bumped to 30s — call returned in
~5–10s. The seam catches the timeout and falls back to rules, so
the failure mode is correct, but a saga whose top-2 candidates
land within ε would silently never benefit from the LLM if every
call timed out. **For the eval rerun set
`AGORA_ROUTING_LLM_TIMEOUT_SECS=30` explicitly** — committed
default of 5 is the production-warm target, not the cold-start
target. Subsequent calls in the same process were fast (under
2s); the cold-start bottleneck is per-process, not per-call.
Future ADR may bump the default once we have production warm-pool
data.

### 2026-05-03 — Defensive standards harness ships with self-test fixtures, not the real schema
PR #52 (#10 ISO 18626 XSD validation in CI) wanted to add CI-level
schema validation. Two complications: (a) the project doesn't emit
ISO 18626 XML today (mod-rs handles wire); (b) the canonical XSD
on illtransactions.org was unreachable from the dev environment
(TLS cert mismatch on the public host). Shipping with a real-XSD
hard requirement would have made every CI run depend on a third-party
fetch that may or may not work, and would have forced a license/
redistribution call we hadn't made. The clean answer was to **split
the harness from the schema**: ship the validator
(`scripts/validate_iso18626.py`), ship hand-rolled minimal fixtures
under `tests/fixtures/iso18626/` (`minimal.xsd` +
`minimal-valid.xml` + `minimal-invalid.xml`) using a private
namespace `http://example.test/agora/minimal`, and gate the
real-XSD test on `docs/standards/iso18626/iso18626-v1_3.xsd` being
cached locally (skips with a clear pointer to the cache step
otherwise). Net: **8 always-on tests prove the lxml plumbing works,
2 tests skip cleanly until staff caches the XSD.** When the cache
lands, the same harness picks up real-schema fixtures
(`iso18626-*.xml`) automatically. **Generalises to any defensive
standards work where the spec lives behind a third-party fetch:**
make the framework ship-ready, make the data opt-in, document the
opt-in step in a README colocated with the cache directory.

### 2026-05-04 — Factory-toggle conventions don't always mirror — check defaults first
ReShare's `get_client()` works by URL-presence: `RESHARE_BASE_URL=""`
defaults to mock; non-empty switches to http. Mirroring that for
CrossRef and SRU was the obvious move and **wrong**: both ship
with non-empty production URL defaults (`api.crossref.org`,
`lx2.loc.gov`), so a presence check would force http and break
offline dev. Resolution: explicit `AGORA_*_ENABLED` boolean
Settings (additive, no behaviour change for existing callers).
The advisor surfaced three options pre-flight — flip URL defaults
to empty (breaks `HttpCrossrefClient()`'s default-URL constructor),
explicit booleans (chosen), or a coarse umbrella flag (rejected
— two clients may want independent toggling). Lesson:
**convention-mirroring needs a defaults check** — two factories
that look isomorphic on the surface can have incompatible toggle
semantics under their defaults.
*(PR #46 — see `agora.clients.crossref.get_crossref_client`,
`agora.clients.sru.get_sru_client`, `Settings.crossref_enabled` /
`sru_enabled`.)*

### 2026-05-04 — App-state wiring without a consumer is dead state
Initial scope for PR-8b factory wiring was: factories + lifespan
registration + `app.state.discovery` + `aclose()` on shutdown.
Advisor pushback: there's no endpoint that consumes DiscoveryAgent
today, so we'd be constructing an httpx connection pool nothing
dispatches against. Three honest scopes surfaced — (1) factories
only, (2) factories + dead state, (3) factories + endpoint. Picked
(1). The endpoint shipped one PR later (8c) once the handler shape
was decided. Generalises: **prefer the smaller PR that ships a
real seam** over a medium PR with a dangling pool. Dead state
also confuses future readers — it implies a consumer exists.
*(PRs #46 / #53.)*

### 2026-05-04 — Test that constructs an optional-extra module forces that extra into CI
`[adk]` was an opt-in extra for production consumers (kroger
precedent). The moment `tests/test_routing_llm_adk.py` constructed
`AdkLlmTiebreaker` (which lazy-imports `google.adk` and raises if
absent), the extra became mandatory in CI's install line. Local
Windows venv had `[adk]` from earlier session probing; CI's fresh
`pip install -e ".[dev]"` missed it → 8 lazy-import RuntimeErrors.
Fix: switch CI install to `pip install -e ".[dev,adk]"` (commit
`05d876b`). Generalises: **tests that import an optional-extra
module path bind that extra to CI.** The opt-in distinction lives
for prod consumers only — once tests exist, CI must install the
extra unconditionally.
*(PR #49 — see `.github/workflows/*.yml` install lines.)*

### 2026-05-04 — Discovery endpoint = single OBSERVATION re-runnable per call
`POST /sagas/{id}/discover` writes a single OBSERVATION event with
a fresh ULID idempotency key (`discovery-{ULID}`) — *not* a
deterministic key like the OverdueScanner uses. Discovery is
intentionally re-runnable: a citation edit or SRU index refresh
should produce a new event with the latest candidate list, not be
absorbed by the UNIQUE constraint as a "replay." Anchored on
`StepName.ROUTE` (the step the candidates feed) following the
TrackingAgent precedent of "anchor the observation on the step
it's *about*, not the saga's current step." Saga state is
unchanged — discovery is advisory; staff still commits a ROUTE
gate via `/approve` to lock in the supplier.
*(PR #53 — see `agora.api.app.discover`,
`tests/test_api.py::test_discover_is_rerunnable_each_call_new_event`.)*

---

## Schema / migrations

### 2026-04-29 — `Base.metadata.create_all()` for tests, Alembic for production
SQLite tests use `create_all()` directly; Postgres uses Alembic
migrations. Every new column or table needs (a) a new revision
in `alembic/versions/` AND (b) the ORM in `saga/db.py` updated. Do
not rely on `create_all()` to "just work" — the column/index DDL it
emits is not what Alembic emits. **Closed by PR #24** (2026-05-02):
`postgres-tests.yml` now runs three tests against `postgres:15-alpine`
in CI — `upgrade head`, `upgrade head → downgrade base → upgrade head`
round-trip, and `compare_metadata` ORM-vs-migrated-schema parity.
SQLite still uses `create_all()` for boot speed in unit tests.
*(Original known-gap in CLAUDE.md; closed in PR #24, see
`tests/test_alembic_postgres.py`.)*

### 2026-04-29 — Lifecycle column is `VARCHAR`, so adding a state needs no DDL
ADR-0012 prep added `LifecycleState.APPROVING`. The Alembic revision
is empty — no `ALTER TYPE` because the underlying column is varchar,
not a Postgres ENUM. We get the no-op revision anyway as a marker so
the schema-version column tracks the lifecycle change. If this ever
moves to a real ENUM, every state-add needs `ALTER TYPE ... ADD VALUE`.
*(PR #16; see `alembic/versions/20260503_approving_state_marker.py`.)*

---

## Security tooling

### 2026-05-04 — `.secrets.baseline` filenames are platform-shaped; commit them in forward-slash
The detect-secrets baseline persists filenames in whichever separator
the generating OS uses. Generated on Windows: `docs\runbook.md`.
Linux CI's `git ls-files | xargs detect-secrets-hook --baseline` then
compares those Windows paths against the actual `docs/runbook.md`,
fails to reconcile, treats every Windows-pathed file as a brand-new
scan target, finds the same secrets again, and rewrites the baseline
with forward-slash entries — exiting non-zero with "The baseline file
was updated. Please `git add .secrets.baseline`." The audit then fails
on every CI run until somebody catches it. Fix: normalize the baseline
to forward-slash before commit (one-shot json transform). Same
mechanism caused PR #55's audit failure on the docs stale-fix sweep
even though the actual line shift was intended (`docs/runbook.md` env
table grew). The two effects compound — the path mismatch makes line
numbers flap on every run regardless of whether content shifted.
**Generalises:** any tool whose database keys on filenames must be
normalized to a single separator if the repo is touched on both
platforms. Don't rely on git's `core.autocrlf` to mask this — content
endings normalize, but filename storage in third-party JSON does not.
*(PRs #55, #57 — see `.secrets.baseline` results map; the normalize
script lived briefly at `scripts/_normalize_baseline.py` before
deletion.)*

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
