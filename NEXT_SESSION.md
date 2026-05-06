# Next session resume note

**Last updated:** 2026-05-06 (PR #97 open — merge me first).

## Repo state

- `master` clean at PR #96 (d3bce32).
- Test count: **251** (248 passing + 9 skipped postgres/reshare/iso).
- ADR count: **16**.
- Open PR: **#97** (ADR-0016 / recall via manualClose — ready to merge).

## PRs this session (in order)

| PR | Title | Status |
|----|-------|--------|
| #96 | feat(reshare): probe findings from live mod-rs sandbox | Merged |
| #97 | feat(reshare): ADR-0016 — recall_request via manualClose | **Open — merge me** |

## What to do at session start

```
gh pr merge 97 --squash   # merge PR-D
git checkout master && git pull
pytest -q                 # 251 pass, 9 skip
ruff check src tests      # clean
```

## What PR #97 does

ADR-0016 resolves the compensate-SHIP path. `recall_request` now calls
`performAction` with `action="manualClose"` (force-close; no supplier
notification). SHIP compensator outbox row delivers instead of
dead-lettering. Saga reaches DISPUTED as designed. 3 new respx unit
tests added.

## Backlog (current, prioritised)

### Immediate (self-contained)
- **None** — all self-contained work is done.

### Sandbox-blocked
1. **Real NCIP HTTP/SOAP client** — `MockNcipClient` still in use;
   real `mod-ncip` integration future work.
2. **WorldCat holdings lookup** — paid OCLC sandbox key needed.

### Needs ADR / design decision
- **ADR-0016 follow-up (production recall)**: Option A (ISO 18626
  Cancel via `message` performAction) is the production path. Needs
  two-tenant sandbox and wire-level testing. Not urgent for prototype.

### Revisit later
- FOLIO community sandbox: folio-snapshot.dev.folio.org
- Index Data / OLE: info@indexdata.com, FOLIO Slack #reshare

## Key gotchas (session 2026-05-06)

- **FOLIO tenant IDs: alphanumeric only.** `consortium-a` → Postgres
  schema syntax error in mod-rs. Use `diku`.
- **mod-rs probe creates Responder-side record** when
  `supplyingInstitutionSymbol` is set. Requester-side (REQ_*)
  creation via direct API still unverified.
- **No requester recall action in mod-rs.** Actions.groovy confirmed.
  manualClose used as prototype stand-in (ADR-0016).

## Resume protocol

- Merge PR #97 first.
- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- `scripts/sync_doc_counts.py --fix` after test count changes.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
