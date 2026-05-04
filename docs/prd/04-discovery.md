# PRD 04 — Discovery

> Last reviewed against code: 2026-05-04 (post CrossRef client PR-A
> + DiscoveryAgent integration PR-B + factory wiring PR-C + endpoint
> wiring #46/#53 — DOI input now triggers a CrossRef identity
> confirmation that re-keys the SRU search; CrossRef errors and 404s
> downgrade to diagnostics so SRU still runs; both clients now expose
> ``get_*_client()`` factories selecting mock vs HTTP via
> ``AGORA_CROSSREF_ENABLED`` / ``AGORA_SRU_ENABLED``;
> `POST /sagas/{id}/discover` runs DiscoveryAgent against a saga's
> stored request and writes a ROUTE-anchored OBSERVATION).

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
  Identifier confirmation (DOI → CrossRef: PR-A client + PR-B agent
                            integration; OCLC# → WorldCat: future,
                            sandbox blocker)
       │
       ▼
  Holdings search (SRU; LoC default; consortium union catalog planned)
       │
       ▼
  HolderCandidate list (deduped by symbol)
```

**Today** `DiscoveryAgent.run` consults CrossRef when (a) the patron
supplied a DOI AND (b) the agent was constructed with a CrossRef
client; uses the confirmed ISBN/ISSN/title to seed the SRU search
(preferring CrossRef-confirmed values over the patron's own); then
searches SRU by ISBN → ISSN → title in that order of preference.
Existing callers that built the agent without `crossref=` keep
working unchanged. WorldCat and consortium-union SRU remain
unimplemented.

**Two clients, two roles — sequential pipeline, not a merge.**
CrossRef confirms *bibliographic identity* for a DOI (title, ISSN,
ISBN, container, year, item kind); it returns no holdings. SRU
finds *who holds* the item (MARC 852). DiscoveryAgent therefore
runs them sequentially — CrossRef sharpens the identifier, then
SRU answers "who has it." The candidate list is always
SRU-derived; CrossRef enrichment only changes which identifier
seeds the SRU search. There is no candidate-list merge.

**CrossRef is best-effort.** A 404 (DOI unknown to CrossRef), a
5xx, or a network failure produces a diagnostic and the SRU search
runs against the request's own identifiers. Discovery never fails
because of CrossRef; the `RemoteUnavailableError` is caught inside
the agent.

**Saga durability.** `request.item` is never mutated — CrossRef
confirmation happens runtime-only and feeds local "effective
identifier" variables consumed by the SRU call. Re-running
discovery on a saga always starts from the patron's submitted
metadata.

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

## Client selection

`get_crossref_client()` and `get_sru_client()` are the production
factories. Both default to the in-memory mock and switch to the live
HTTP client when their respective env flag is set:

| Factory | Env flag | Default | URL |
| ------- | -------- | ------- | --- |
| `agora.clients.crossref.get_crossref_client` | `AGORA_CROSSREF_ENABLED` | mock | `CROSSREF_BASE_URL` (default `https://api.crossref.org`) |
| `agora.clients.sru.get_sru_client`           | `AGORA_SRU_ENABLED`      | mock | `SRU_LOC_URL` (default `https://lx2.loc.gov/voyager`)   |

Both factories use **explicit boolean toggles** rather than the
URL-presence convention `agora.clients.reshare.get_client` uses,
because both URLs ship with non-empty production defaults — a
presence check would always select http and break offline workflows.
The DiscoveryAgent itself takes both clients as constructor kwargs
(`DiscoveryAgent(sru, *, crossref=None, ...)`) so callers can wire
the factories at app startup or pass test doubles directly.
Wiring into `agora.api.app.create_app` shipped in #46/#53:
`create_app()` constructs `DiscoveryAgent` from the two factories at
top and stashes it on `app.state.discovery`; the lifespan calls
`aclose()` on both http pools at shutdown alongside ReShare. The
synchronous staff-handler shape was chosen over a background
lifespan task — `POST /sagas/{id}/discover` invokes the agent
inline and writes a single ROUTE-anchored OBSERVATION (kind
`"discovery"`) per call.

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
