# PRD 04 — Discovery

## Inputs

The DiscoveryAgent accepts any of:

- **OpenURL 1.0 ContextObject** (KEV or XML) — primary path; emitted by
  link resolvers and citation managers.
- **Free-text citation** — parsed with a citation parser; lower confidence.
- **Identifier-only** — ISBN, ISSN, DOI, OCLC#.

## Resolution flow

```
OpenURL/citation
       │
       ▼
  Parse → metadata (title, author, ISBN, ISSN, DOI, year, pages, ...)
       │
       ▼
  Identifier lookup (DOI → CrossRef, OCLC# → WorldCat)
       │
       ▼
  Holdings search (SRU against consortium union catalog + LoC)
       │
       ▼
  Candidate holders [{symbol, status, distance, preferred}]
```

## SRU usage

SRU = REST/HTTP successor to Z39.50. Query strings via CQL.

Example query against LoC SRU:

```
https://lx2.loc.gov/voyager?
  version=1.1&operation=searchRetrieve
  &query=bath.isbn=9780262033848
  &maximumRecords=20
```

Agora's SRU client lives at `src/agora/clients/sru.py`. Returns parsed
MARCXML records. We do **not** speak Z39.50 binary protocol in the
prototype — if a target lacks SRU, the holder is excluded.

## OpenURL parsing

Use `openurl` Python lib (or hand-rolled KEV parser if it lacks features).
Key fields: `rft.atitle`, `rft.title`, `rft.au`, `rft.issn`, `rft.isbn`,
`rft.doi`, `rft.date`, `rft.pages`, `rft.spage`, `rft.epage`. Also
`req_id` for patron and `rfr_id` for referrer.

## Output schema

```json
{
  "item": {
    "title": "...",
    "author": "...",
    "isbn": "...",
    "oclc_number": "...",
    "type": "book|article|chapter|other"
  },
  "candidates": [
    {
      "symbol": "ABCDE",
      "name": "Library Name",
      "status": "available|on_loan|reference_only|unknown",
      "distance_km": 42,
      "is_consortium_member": true,
      "preferred_score": 0.92,
      "raw": { "marc": "...", "src": "sru:union" }
    }
  ],
  "ambiguous": false,
  "diagnostics": []
}
```

## Failure modes

| Symptom | Action |
|---------|--------|
| Citation unparseable | Mark `ambiguous=true`, surface to staff for cleanup |
| No identifier resolvable | Search by title+author with confidence score; flag low-confidence matches |
| Zero holders | Saga goes to `Unfilled` terminal state |
| All holders status=unknown | Pass through; RoutingAgent decides whether to try anyway |

## Out of scope (prototype)

- Inter-consortium discovery beyond LoC + sandbox union catalog
- Real WorldCat (paid API; mock with a static holders fixture)
- Z39.50 binary protocol
- Article-level full-text discovery (defer to OpenURL link resolver)
