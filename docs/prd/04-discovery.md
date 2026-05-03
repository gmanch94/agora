# PRD 04 — Discovery

> Last reviewed against code: 2026-05-03 (post CrossRef client PR —
> client + ``MockCrossrefClient`` + tests landed; DiscoveryAgent
> integration is queued as PR-B).

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
  Parse → Item + Citation (KEV fields)
       │
       ▼
  Identifier lookup (DOI → CrossRef: client landed in PR-A;
                     OCLC# → WorldCat: still future, sandbox blocker)
       │
       ▼
  Holdings search (SRU; LoC default; consortium union catalog planned)
       │
       ▼
  HolderCandidate list (deduped by symbol)
```

**Today** `DiscoveryAgent.run` searches by ISBN → ISSN → title (in
that order of preference) via the SRU client. The CrossRef client
exists at `src/agora/clients/crossref.py` (PR-A) but is not yet
wired into the agent — DOI inputs land in `IllRequest.item.doi`
without a lookup. WorldCat and consortium-union SRU remain
unimplemented.

**Roles, two clients.** CrossRef confirms *bibliographic identity*
for a DOI (title, ISSN, year, container, item kind); SRU finds *who
holds* the item (MARC 852). PR-B will fan out DiscoveryAgent to
both: CrossRef when a DOI is present, then SRU keyed off the
confirmed ISBN/ISSN, with merge-rank in the candidate list.

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

`DiscoveryRecommendation` (`src/agora/agents/discovery.py`):

```python
@dataclass(slots=True)
class DiscoveryRecommendation:
    candidates: list[HolderCandidate]   # deduped by symbol
    diagnostics: list[str]              # e.g. "zero holders matched"
    rationale: str                      # human-readable, ≤ 1-2 sentences
```

Each `HolderCandidate` (`src/agora/models/candidate.py`):

```python
class HolderCandidate(BaseModel):
    symbol: str                 # ISIL or consortium-local
    name: str | None = None
    status: str = "unknown"     # 'available'|'on_loan'|'reference_only'|'unknown'
    distance_km: float | None = None
    is_consortium_member: bool = False
    preferred_score: float = 0.0  # 0..1; 1.0 if in consortium today
    raw: dict[str, Any] = {}
```

The `IllRequest.item` already carries title / author / ISBN / ISSN /
DOI / OCLC# (see `src/agora/models/request.py`). The CrossRef client
covers DOI→identity in isolation; OCLC#-keyed WorldCat lookups
remain out of scope until the WorldCat sandbox integration lands.

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
