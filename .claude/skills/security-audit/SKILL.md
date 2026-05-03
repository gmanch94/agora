---
name: security-audit
description: Run a security audit pass over agora — Bandit on src/agora/, pip-audit on locked deps, detect-secrets sweep, plus agora-specific concerns (ReShare Basic auth, NCIP creds, OpenURL targets, saga-event payload sanitization). Use when reviewing security before a milestone, after touching auth/credentials, before opening agora to a real ReShare tenant, or when CLAUDE.md known-gaps lists an unverified surface that touches credentials.
---

# security-audit

Agora ships secrets through three surfaces: ReShare Basic auth (dev
path, `AGORA_RESHARE_*`), Postgres connection URL, and NCIP creds
(future). Saga-event payloads are written to the ledger verbatim, so
a forward step that stuffs a credential into `payload` leaks it into
the audit log. This skill is the audit pass — broader than the inline
PreToolUse `scan_secrets.py` hook, which only catches secrets at edit
time.

## When to invoke

- Before going live against a real ReShare tenant (today: dev only)
- After touching `clients/reshare.py`, `clients/ncip.py`, `config.py`,
  or anything under `src/agora/api/` that handles auth
- Before a milestone PR (e.g. wrapping up a phase from
  `prompts/build-agora.md`)
- When CLAUDE.md known-gaps mentions an unverified credential surface
- Periodic: every ~10 PRs, regardless

## Why a separate audit (vs the inline hook)

`.claude/hooks/scan_secrets.py` catches obvious patterns at write
time (PreToolUse on `Write|Edit`). It does **not** catch:
- Secrets that leak via saga-event `payload` (audit log) or outbox
  rows
- Vulnerable transitive deps (only pip-audit sees these)
- SQL-injection-shaped string concatenation
- `shell=True` subprocess calls, unsafe `pickle.loads`,
  weak crypto (MD5/SHA1 for anything other than a checksum)
- Path traversal on any file the API ever opens by patron-supplied
  name (none today; flag for future patron-upload feature)

The audit is the once-per-milestone broad sweep.

## Quick start

```bash
# Static analysis (high severity only — noise filter)
.venv/Scripts/python.exe -m bandit -r src/agora -ll

# Dependency vulnerabilities (locked deps in pyproject.toml)
.venv/Scripts/python.exe -m pip_audit

# Secrets sweep — generates baseline first run, diff after
.venv/Scripts/python.exe -m detect_secrets scan --baseline .secrets.baseline

# All three (bandit + pip-audit + detect-secrets) via the bundled
# script. Invokes scanners via `sys.executable -m <module>`, so the
# venv interpreter is enough — no PATH munging needed:
.venv/Scripts/python.exe .claude/skills/security-audit/scripts/security_scan.py .
```

Tools are NOT in `[dev]` extra today — install ad-hoc per audit
(adding to `[dev]` is a separate decision; pip-audit pulls a lot).

```bash
.venv/Scripts/python.exe -m pip install bandit pip-audit detect-secrets
```

## Agora-specific concerns

### Secrets in saga-event payloads

`saga_event.payload` is JSONB, append-only, and visible to anyone
with DB read. Forward steps must NOT stuff credentials into payload.
Audit:

```bash
# Look for forward steps that close over auth-bearing locals
.venv/Scripts/python.exe -m grep -rn "password\|api_key\|token\|basic_auth" src/agora/saga/
```

Today `saga/flows.py` only writes IDs (`reshare_id`,
`chosen_supplier`, etc.) — keep it that way.

### Outbox payload leakage

`outbox.payload` is also JSONB. The `make_reshare_handler` builder
calls `getattr(client, action)(**args)` — `args` comes from the row.
**Don't pass auth in `args`** — auth lives on the client built once
in lifespan startup.

### ReShare Basic auth (dev path)

`AGORA_RESHARE_BASIC_AUTH` env var. Production needs Okapi token
flow (CLAUDE.md known-gap). When migrating, write an ADR via
`adr-new` and rotate any leaked dev creds.

### NCIP creds (future)

When the NCIP client lands (CLAUDE.md known-gap: mock-only today),
audit its credential handling against this checklist before merging.

### `httpx` and SSRF

`HttpReShareClient` and `OpenURLClient` take a base URL from config.
If a base URL ever derives from patron input, that's SSRF. Today
neither does — flag if a future change makes it possible.

## Common Bandit findings to expect (and triage)

| Bandit ID | Pattern | Likely fix in agora |
|-----------|---------|---------------------|
| B101 | `assert` in non-test code | OK in `src/agora/saga/` for invariants; skip via `.bandit` |
| B105/B106 | Hardcoded password string literal | Move to `config.py` `Field(..., alias="AGORA_*")` |
| B301 | `pickle.loads` | Should not appear; we use JSON for payloads |
| B303 | MD5/SHA1 | Confirm not used for security; checksums OK |
| B602 | `subprocess` with `shell=True` | Replace with list args |
| B608 | SQL string interpolation | All queries go through SQLAlchemy ORM today; fail loud if raw SQL appears |

## `.bandit` config (recommended)

Drop this at repo root if running bandit regularly:

```yaml
exclude_dirs:
  - tests
  - docs
  - .venv
  - alembic/versions
skips:
  - B101  # assert_used — we use asserts for saga invariants
```

`alembic/versions/` is excluded because Alembic generates raw SQL
that's reviewed at migration-write time, not audit time.

## CI integration (deferred)

Not wired today. When wired, suggested workflow lives at
`.github/workflows/security.yml`:

```yaml
- run: .venv/Scripts/python.exe -m bandit -r src/agora -ll
- run: .venv/Scripts/python.exe -m pip_audit
- run: .venv/Scripts/python.exe -m detect_secrets scan --baseline .secrets.baseline
```

Add via a future PR; cite this skill in the ADR.

## Audit checklist

```
Code (Bandit pass):
- [ ] No SQL injection risk (all queries via SQLAlchemy ORM)
- [ ] No subprocess with shell=True
- [ ] No hardcoded creds (only AGORA_* env vars via config.py)
- [ ] No MD5/SHA1 for security (checksums only)
- [ ] No pickle.loads on untrusted data

Saga-specific:
- [ ] saga_event.payload contains only IDs + state, no creds
- [ ] outbox.payload contains only action + args (no auth)
- [ ] Idempotency keys are ULIDs (no PII embedded)

Dependencies (pip-audit pass):
- [ ] No HIGH or CRITICAL CVEs in installed deps
- [ ] Transitive deps via httpx, sqlalchemy, fastapi reviewed

Secrets (detect-secrets pass):
- [ ] .secrets.baseline up to date
- [ ] No new findings vs baseline
- [ ] .env / *.pem / *.key remain gitignored

Operational:
- [ ] AGORA_RESHARE_BASIC_AUTH rotation plan documented
- [ ] No creds in commit history (git log -p | rg PATTERN)
```

## Out of scope

- FedRAMP control implementation (alignment-noted only — see
  `docs/fedramp-future.md` if/when written)
- Penetration testing / red team
- Real Okapi token flow (separate ADR)
- Auth on the staff-console UI (no UI yet)

## Pair tools

- `adr-new` — when audit findings need a decision recorded (e.g.
  switching off Basic auth, changing the credential storage plan)
- `docs-stale-check` — after audit, runbook/PRD may need a freshness
  bump if audit changed any operational guidance

## Bundled scripts

- `scripts/security_scan.py` — runs bandit + pip-audit + detect-secrets
  and produces a unified report. Originally from `wdm0006/python-skills`,
  MIT-licensed. Cherry-picked into agora rather than installing the full
  plugin (only this skill applied — most others are PyPI-library shaped,
  not app shaped). Modified from upstream:
  * Scanners are invoked via `sys.executable -m <module>` instead of bare
    PATH lookup, so the script works against a venv-only install
    (Windows `.venv\Scripts\`, or any layout where the scanners aren't on
    the system `PATH`).
  * The `safety` scanner branch was dropped — the package is unmaintained
    and `pip-audit` covers the same vulnerability database.
  * Known limitation: the detect-secrets call runs raw `scan` and does
    NOT diff against `.secrets.baseline`, so it lists everything in the
    baseline as a "finding." Use `make audit` (or the
    `detect-secrets-hook --baseline .secrets.baseline` invocation in
    CI) for the gate-clean answer; the bundled script is a fast
    "what would a fresh auditor see" view.

## Provenance

Skill adapted from
[`wdm0006/python-skills/security-audit`](https://github.com/wdm0006/python-skills/tree/main/skills/security-audit)
(MIT, Will McGinnis). Adaptation: dropped library framing, added
agora-specific surfaces (saga-event payload, outbox, ReShare Basic
auth, NCIP future), aligned commands with agora's
`.venv/Scripts/python.exe` invocation pattern.
