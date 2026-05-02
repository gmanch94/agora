---
name: adr-new
description: Bootstrap a new Architecture Decision Record under `docs/adr/` with the project's standard template, the next sequential number, and consistent Status/Context/Decision/Consequences sections. Use when the user makes a non-trivial design decision worth locking in (anything that would be expensive to reverse later).
---

# adr-new

Agora's ADRs are the most-referenced thing in the repo — they answer
"why is it like this" months later when no one remembers. New ADRs
should look exactly like the existing 10 so they're scannable.

## When to invoke

- User says "let's write that up as an ADR" / "ADR for X" / "decide
  X"
- A code review uncovers an implicit decision that should be explicit
- Reversing a prior ADR (write a new ADR superseding it; mark the old
  one's Status as `Superseded by ADR-NNNN`)
- Before doing work that locks in a hard-to-reverse choice
  (storage, framework, protocol, security boundary)

## What to do

### Step 1 — pick the next number

List `docs/adr/`, find the highest `NNNN-*.md`, increment by 1,
zero-pad to 4 digits. Existing range as of this writing: 0001–0010.

### Step 2 — pick the slug

Slug is `kebab-case`, short, decision-shaped (the verb + the choice).
Examples from existing ADRs:

- `wrap-folio-reshare`
- `event-sourced-saga-ledger`
- `python-fastapi-stack`
- `fedramp-deferred`

Bad slugs: `database-stuff`, `decision-1`, `reshare`. Good slugs read
like a one-line answer to "what did we decide?".

### Step 3 — fill the template

```markdown
# ADR-NNNN: <Title (Decision in noun-phrase form)>

## Status

Accepted — YYYY-MM-DD

(Or: Proposed | Superseded by ADR-XXXX | Deprecated)

## Context

What forced the decision. 2–4 short paragraphs. Constraints, the
problem we're solving, what we tried or considered. Be honest about
what we don't know.

## Decision

The chosen path, stated as imperatives. "We will <verb>." Specific —
name the technology, library, pattern. If alternatives were rejected,
list them with one-line reasons. Keep this section short; it's the
load-bearing one.

## Consequences

### Positive
- ...

### Negative
- ...

### Neutral / follow-ups
- ...

## References

- Link to PRD section if applicable
- Link to upstream docs / RFCs / papers
- Link to prior ADR if superseding
```

### Step 4 — match house style

Read 2-3 existing ADRs first (e.g.
`docs/adr/0002-event-sourced-saga-ledger.md`,
`docs/adr/0005-human-approval-default.md`) and match:

- Tone: declarative, terse, written in the present/future tense
  ("We will use X", not "We considered using X")
- Length: 200-500 words total. ADRs are not essays.
- No "TBD" sections. If a section has nothing, write "None
  identified" — but think hard before doing so for Negative
  consequences (every decision has a downside).
- One ADR per decision. Split if you find yourself with two
  Decision sections.

### Step 5 — write + verify

Write the file with `Write` tool. Then:

```bash
.venv/Scripts/python.exe -m ruff check docs/adr/NNNN-*.md  # not a Python file but
                                                            # confirms no path issues
ls docs/adr/                                                # confirm placement
```

Surface the ADR's Decision section back to the user as a one-line
summary so they can sanity-check.

## Don'ts

- Don't write an ADR for trivial choices (variable naming, file
  layout). ADRs are for things expensive to reverse.
- Don't skip the Negative consequences section. If a decision has no
  downsides you're missing something.
- Don't edit prior ADRs to "fix" a decision. Mark them Superseded
  and write a new one. ADRs are a log, not a wiki.
- Don't number-collide. Always check the directory first.
- Don't use the `Status: Proposed` state to ship code. Either accept
  or don't.
