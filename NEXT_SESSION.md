# Next session resume note

**Last updated:** 2026-05-06 (PR #96 open — master clean, PR-C awaiting merge).

## Repo state

- `master` is fully synced and clean.
- Test count: **257** (248 passing + 9 skipped postgres/reshare).
- ADR count: **15**.
- Open PR: **#96** (feat(reshare): probe findings — ready to merge).

## PRs landed / open this session

| PR | Title | Status |
|----|-------|--------|
| #96 | feat(reshare): document probe findings from live mod-rs sandbox | **Open — merge me** |

## What PR #96 resolves

Probe ran against `mod-rs:2.19.0-rc17` via `make reshare-probe`:

1. **Body shape confirmed** — camelCase fields accepted by mod-rs.
   Caveat: probe created a Responder-side record (RES_IDLE) because
   `supplyingInstitutionSymbol` was included. Requester-side (REQ_*)
   creation still unconfirmed against a real borrower-tenant.
2. **Response fields confirmed** — `id` = UUID, `hrid` may be null,
   `state` dict with `.code`, `isoMessageId`/`supplyingAgencyId` absent.
3. **Recall confirmed absent** — `Actions.groovy` has no recall action.
   `recall_request` keeps `ClientError`. Compensate-SHIP path needs ADR.

Also fixed: probe Unicode crashes on Windows, FOLIO tenant ID hyphen
(Postgres schema name error), two lessons.md entries added.

## Immediate next step

Merge PR #96, then address the ADR decision for compensate-SHIP:

**ADR-0016 (proposed): Compensate-SHIP path in mod-rs**

Two options:
- **Option A** — ISO 18626 Cancel via `message` performAction with
  reason code (protocol-correct; needs wire-level testing).
- **Option B** — `manualClose` force-close (local-only; no supplier
  notification; rename `recall_request` to `force_close_request`).

Write ADR, implement choice, update `HttpReShareClient.recall_request`,
add test. This unblocks the SHIP compensator.

## Backlog (current)

### Sandbox-blocked
1. Real ReShare wire (PR-D) — PR-C landed; ADR for recall path needed
2. Real NCIP HTTP/SOAP client
3. WorldCat holdings lookup (OCLC sandbox key)

### ADR needed
- ADR-0016: compensate-SHIP path (recall vs force-close; see above)

### Revisit later
- **FOLIO community sandbox**: https://wiki.folio.org/display/COMMUNITY/FOLIO+Reference+Environments
- **Index Data / OLE**: email info@indexdata.com or FOLIO Slack `#reshare`

## Resume protocol

- Merge PR #96 first (`gh pr merge 96 --squash`).
- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
- **FOLIO tenant IDs must be alphanumeric only** — hyphens cause Postgres
  schema name syntax error in mod-rs (`consortium-a` -> use `diku`).
- **Docker daemon polling:** if `docker` hangs once, stop. Tell user to
  confirm Docker Desktop fully started then retry.
