# Next session resume note

**Last updated:** 2026-05-06 (PR #101 merged — master is clean).

## Repo state

- `master` clean at PR #101.
- Test count: **271** (260 passing + 11 skipped — postgres/reshare/ncip-smoke).
- ADR count: **16**.
- No open PRs.

## PRs this session (in order)

| PR | Title | Status |
|----|-------|--------|
| #96 | feat(reshare): probe findings from live mod-rs sandbox | Merged |
| #97 | feat(reshare): ADR-0016 — recall_request via manualClose | Merged |
| #98 | feat(ncip): HttpNcipClient — NCIP 2.0 XML client | Merged |
| #99 | feat(ncip): wire HttpNcipClient factory into app.py lifespan | Merged |
| #100 | feat(discovery): consortium-member fallback when SRU yields no holdings | Merged |
| #101 | test(ncip): NCIP HTTP smoke test — health + checkout/checkin round-trip | Merged |

## What to do at session start

```
git checkout master && git pull
pytest -q                 # 271 pass, 11 skip
ruff check src tests      # clean
mypy --strict             # clean
```

## Backlog (current, prioritised)

### Sandbox-blocked
1. **NCIP live probe** — need a real FOLIO tenant with mod-ncip
   deployed + configured. Smoke test is ready (`test_ncip_http_smoke.py`,
   PR #101). Set `AGORA_TEST_NCIP_URL` + `RESHARE_TENANT` + `NCIP_AGENCY_ID`
   and run:
   ```
   pytest tests/test_ncip_http_smoke.py -v
   ```
   For the checkout/checkin round-trip also set `AGORA_TEST_NCIP_ITEM_ID`
   and `AGORA_TEST_NCIP_PATRON_ID` (mutates ILS state — use a test tenant).
2. **WorldCat holdings lookup** — **structural gap; POC uses consortium
   roster as fallback (PR #100 shipped).**
   WorldCat Search API v2 (the only current OCLC API; v1 EOL'd Dec 2024)
   requires a paid OCLC subscription — no free or developer tier.
   Probed open SRU union catalogs (DNB, GBV K10plus, SWB, SUDOC, Library
   Hub, LoC): none carry MARC 852 holdings in accessible MARCXML form —
   national-library SRU targets are bibliographic-only. No freely
   accessible union holdings catalog exists.
   **POC resolution (PR #100):** `DiscoveryAgent._records_to_candidates`
   falls back to `AGORA_CONSORTIUM_MEMBERS` when SRU returns no 852
   holdings, synthesising candidates with `status='unverified_holdings'`.
   Revisit when institutional OCLC access or a live multi-tenant pilot
   materialises.

### Needs ADR / design decision
- **ADR-0016 follow-up (production recall)**: Option A (ISO 18626
  Cancel via `message` performAction) is the production path. Needs
  two-tenant sandbox and wire-level testing. Not urgent for prototype.

### Revisit later
- FOLIO community sandbox: folio-snapshot.dev.folio.org
- Index Data / OLE: info@indexdata.com, FOLIO Slack #reshare

## Key gotchas

- **FOLIO tenant IDs: alphanumeric only.** `consortium-a` -> Postgres
  schema syntax error in mod-rs. Use `diku`.
- **HttpNcipClient source-review-only** — unverified against live
  mod-ncip tenant. Wire-in done; live probe still needed.
- **MockNcipClient._state removed** — check_in now returns patron_id=""
  (symmetric with Http client). No test reads patron_id after check_in.
- **WorldCat v1 EOL'd Dec 2024.** Any code referencing the old
  `worldcat.org/webservices` endpoint is dead. v2 API requires
  institutional OCLC subscription.
- **No open SRU union holdings catalog exists.** DNB/SUDOC/GBV/SWB/LoC
  all carry bib-only MARCXML — no MARC 852 subfields. POC routes via
  `AGORA_CONSORTIUM_MEMBERS` fallback (PR #100). Do not re-probe
  these targets expecting 852 data.

## Resume protocol

- Triple gate: `pytest -q`, `ruff check src tests`, `mypy --strict`, `make audit`.
- `scripts/sync_doc_counts.py --fix` after test count changes.
- GPG signing disabled (`commit.gpgsign=false`).
- Python: `.venv/Scripts/python.exe`.
- **Always branch + PR, never commit directly to master.**
