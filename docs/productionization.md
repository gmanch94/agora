# Productionizing & Operating Agora

> **Status of this document.** Agora is a research prototype. This
> doc describes what would have to be true — in code, infrastructure,
> process, and organisation — to take it from "demo runs end-to-end"
> to "consortium runs it for real patrons in 2027." It is *not* a
> claim that Agora is production-ready today. Read it as a punch list.
>
> **Last reviewed against code:** 2026-05-09 (post audit-remediation
> sprint, master at `a6eb6fa`, 553 tests collected / 542 pass + 11
> skipped, 18 ADRs, 36 of 42 audit findings closed).
>
> **Audiences.** This doc serves two readers:
> - **Leadership** (Library Director, CIO, consortium board, funder
>   program officer) — read §0 (Executive summary) and the
>   *Leadership view* call-outs in each section. About a 10-min read.
> - **Operations** (IT director, eng ops lead, on-call engineer) —
>   read §1 onward in full. The gap matrix in §2 is the spine; every
>   later section refers back to gap IDs.

## 0. Executive summary

**What Agora is.** An agentic Inter-Library Loan (ILL) system that
wraps existing FOLIO/ReShare standards plumbing (ISO 18626, NCIP,
SRU) with intelligent routing, advisory agents, and a saga-based
ledger that records every state change with paired forward +
compensator (rollback) operations. Humans approve every transition;
agents recommend, never decide. Codebase: 553 tests collected / 542
pass + 11 skipped, 18 ADRs, post-audit-remediation hardening (Basic
auth on all endpoints + tenant-scoping stopgap, patron-portal HMAC,
proactive Okapi token refresh, prompt-injection guard, CSRF + rate
limit + HTTPS + security headers, SecretStr credentials, XML XXE
guard).

**What "production" means here.** One consortium, single-tenant,
on-prem or single-tenant cloud. Real patrons placing real ILL
requests. Not multi-consortium SaaS, not federal deployment.

**The decision being asked.** Whether to commit a consortium and a
small engineering team (≈ 1–2 FTE) to a phased rollout — Pilot
(Phase 1, ~6 months) → GA (Phase 2, ~12 months from start). Total
calendar: ≈ 18 months from "go" to "GA on a single consortium."

**The cost.**

| Bucket                    | Phase 0 → 1 (build to pilot) | Phase 1 → 2 (pilot to GA, ongoing) |
| ------------------------- | ---------------------------- | ---------------------------------- |
| Engineering FTE           | 1.5 FTE × 6 months           | 1 FTE × 12 months, then ~0.5 ongoing |
| Cloud / infra (monthly)   | ~$200 (staging only)         | ~$400–$900/mo at pilot sizing (§8.4) |
| External licences         | $0 incremental — FOLIO/ReShare assumed already deployed by consortium | same |
| One-off (DR drill, threat model, two-tenant probe) | ~$15–25k consulting if not in-house | quarterly DR drills folded into ops cadence |

**The risks (top three).**
1. **ReShare two-tenant verification** (gap G-03). Sandbox probe
   confirmed Responder-side flow; Requester-side and recall
   semantics still unverified. **Phase 1 cannot start until this
   closes.** Mitigation: 2-week focused engagement against a live
   consortium tenant.
2. **Patron PII retention** (gap G-07). State library-record statutes
   require destruction-on-completion; Agora retains indefinitely
   today. Pre-Phase-1 ADR + scrub job. Mitigation: ADR-0019, ~1-week
   eng work, library-counsel review.
3. **Multi-principal authentication** (gap G-01). Single-principal
   Basic auth + tenant-scoping stopgap landed in the 2026-05-09
   audit-remediation sprint ([ADR-0018](adr/0018-tenant-scoping-stopgap.md));
   real per-staff identity needs OIDC SSO with library-symbol claim
   per principal. Pre-Phase-1: replace the `ConsolePrincipal`
   dependency seam with the SSO integration. Mitigation: well-trodden
   FastAPI integration; ~2-week eng work.

**What leadership signs off on, by phase.**

| Phase    | Leadership decision                                  | Ops responsibility                                   |
| -------- | ---------------------------------------------------- | ---------------------------------------------------- |
| 0 → 1    | Commit pilot consortium + FTE; approve Phase-1 budget | Close Phase-0 exit criteria (§3); deliver four pre-Phase-1 ADRs (§11) |
| 1 → 2    | Approve GA scope after pilot retrospective; expand consortium scope or stay single-pilot | Achieve pilot SLOs (§6.4); document gap-matrix delta |
| 2 → 3    | Decide multi-tenant SaaS vs. stay single-consortium  | If SaaS: deliver tenant-isolation roadmap (§8.2)     |

**FedRAMP is out of scope.** Federal deployment is not part of this
plan ([ADR-0007](adr/0007-fedramp-deferred.md)). State/local
library-record statutes (e.g. CA Govt Code 6267, IL 75 ILCS 70) are
in scope and addressed in §4.3.

**The honest bottom line for leadership.** Agora's design seams are
clean, the test suite is rigorous, and the core saga + idempotency
primitives are well-tested. The work to productionize is real but
mostly *known and bounded* — twenty gaps documented in §2, three of
them genuinely blocking. This is not a science project; it is an
unfinished engineering deliverable with a credible path to
completion.

---

## 1. Scope of this document

| Question                | Answer                                                                |
| ----------------------- | --------------------------------------------------------------------- |
| Target deployment       | **Single consortium, on-prem or single-tenant cloud.** One Postgres, one API cluster, one set of staff/patron users. Multi-tenant SaaS is a Phase 3+ concern (§8.2). |
| Compliance posture      | Non-federal deployments. **FedRAMP out of scope** per [ADR-0007](adr/0007-fedramp-deferred.md). State/local privacy regimes (CIPA, FERPA where applicable, state library-record statutes) still in scope. |
| Out of scope            | Multi-tenant SaaS economics, federal-government deployment, ML/LLM training pipelines, replacing FOLIO/ReShare. |
| Companion docs          | [`runbook.md`](runbook.md) — day-to-day ops. [`solution.md`](solution.md) — design. [`architecture.md`](architecture.md) — diagrams. This doc *extends* the runbook with what's missing for production; it does not duplicate it. |

## 2. Production-readiness gap matrix

This is the spine of the document. Every later section refers back
to a gap row by ID. Severity reflects "how blocking is this for a
real consortium going live." All gap IDs are stable — quote them in
issues, ADRs, and PRs.

| Gap ID | Area                  | Current state                                                          | Production requirement                                                | Severity | Owner section |
| ------ | --------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------- | -------- | ------------- |
| G-01   | Authentication        | None on API/console. `block_dangerous_git.py` only protects commits.   | OIDC SSO (SAML where required). Service-account auth for ReShare/NCIP. | **P0**   | §4.1          |
| G-02   | Authorisation         | **Phase-0 closed (single-user stopgap, ADR-0019).** Three-tier RBAC (`viewer`/`approver`/`admin`) wired at the FastAPI dependency layer via `_require_role`. Roster via `AGORA_CONSOLE_ROLES`. Multi-user lands with G-01. | RBAC: viewer / approver / admin / cross-consortium-relay (multi-user via G-01 OIDC).  | **P1**   | §4.2          |
| G-03   | ReShare wire surface  | Probed against mod-rs 2.19.0-rc17 sandbox; Requester-side state path unconfirmed. `recall_request` raises (no first-class action in mod-rs). | Two-tenant probe (borrower + supplier roles); recall path resolved per [ADR-0016](adr/0016-compensate-ship-via-manualclose.md); CI smoke against staging tenant. | **P0**   | §5.1          |
| G-04   | NCIP wire surface     | `HttpNcipClient` source-review-only (PR #98/#99). `MockNcipClient` in default config. Smoke test scaffold (#101) gated on `AGORA_TEST_NCIP_*`. | Live mod-ncip probe; ILS round-trip verified for at least the consortium's 2 most-deployed ILSes (Folio, Sierra, Alma). | **P0**   | §5.2          |
| G-05   | ISO 18626 conformance | XSD validator + fixture self-tests (#52). Real v1.3 XSD opt-in cache step, not bundled. Wire conformance delegated to mod-rs. | XSD bundled (or fetched at build); CI gates conformance for any payload Agora generates directly (today: none — but a regression here would be silent until a peer rejects). | **P1**   | §5.3          |
| G-06   | LLM tie-breaker       | Vertex AI `gemini-2.5-flash`, ε=0.03, eval top-1 0.95. Quota-project + Studio enablement required. Failure path falls back to rules silently. | Vertex project owned by ops, not dev. Per-consortium model selection (cost vs. accuracy). LLM-call audit log feeding the rationale to the saga ledger. | **P1**   | §5.4          |
| G-07   | Patron PII            | **Phase-0 closed (ADR-0020).** 90-day post-terminal scrub via `RetentionScanner` background task. Admin DSAR endpoints (`/admin/patrons/{id}/sagas` + `/forget`) ADMIN-role-gated. Off-system audit log of scrub events still pending (depends on G-08). | Documented retention window + purge job + tested DSAR flow. Off-system audit log pending G-08. | **P1**   | §4.3          |
| G-08   | Audit logging         | structlog JSON to stdout. No sink; no tamper-evidence; no retention policy. | Centralised log sink (Loki/CloudWatch/Splunk). Retention ≥ 1 year for saga events, ≥ 90 days for app logs. Log integrity (signed/append-only) for compliance audits. | **P0**   | §6.2          |
| G-09   | Tracing               | None. Plain structlog only.                                            | OpenTelemetry on FastAPI + outbox worker + tracking scanner; trace IDs propagated to ReShare/NCIP calls. | **P1**   | §6.3          |
| G-10   | Metrics & SLOs        | None. No Prometheus/StatsD. SLOs not defined.                          | Saga-stage timing, outbox lag, dead-letter rate, gate latency, scanner heartbeat. SLOs published in §6.4. | **P1**   | §6.4          |
| G-11   | Backups & DR          | `docker-compose.yml` Postgres only. No documented backup, no restore drill, no RPO/RTO. | RPO ≤ 15 min, RTO ≤ 4 h. Daily logical + WAL-archived continuous; quarterly restore drill. | **P0**   | §7.1          |
| G-12   | Migrations under load | `alembic upgrade head` tested in CI against empty DB. No live-traffic migration tested. | Online-DDL discipline (no rewriting locks on saga_event/outbox). Pre-prod migration rehearsal on a clone. | **P1**   | §7.2          |
| G-13   | CI/CD to envs         | CI runs pytest+ruff+mypy+audit on PR. No deployment automation.        | Tagged-release → staging → prod with manual gate. Rollback story.    | **P0**   | §8.1          |
| G-14   | Capacity              | Async-first, `db_pool_size=10`, never load-tested.                     | Documented capacity envelope (req/s, sagas/day) per node sizing. k6 or Locust harness in CI nightly. | **P1**   | §8.3          |
| G-15   | Multi-tenant readiness| Single implicit tenant. No tenant column on `saga_event`, `outbox_row`, `policy_rule`. | Phase 3 — explicit `tenant_id` migration, RLS or per-tenant DB, agent rosters per-tenant. | **P2**   | §8.2          |
| G-16   | Cost observability    | None.                                                                  | Per-saga cost attribution (LLM tokens, Vertex calls, ReShare requests, NCIP messages). Monthly close-the-loop report. | **P2**   | §8.4          |
| G-17   | Threat model          | None written down.                                                     | STRIDE pass on staff console, outbox worker, saga ledger, ReShare client. Reviewed annually. | **P1**   | §4.4          |
| G-18   | On-call & runbooks    | `runbook.md` covers happy paths and 9 common failures. No DR runbook, no on-call rotation defined. | Pager rotation, severity ladder, comms templates. DR runbook with step-by-step restore. | **P1**   | §9           |
| G-19   | Patron portal hardening | Read-only portal at `/portal/*` (#117, #134). No rate limit, patron-id 404 disambiguates real-vs-typo. | Rate limiting, login (G-01 dependency), no-cache headers verified against target ILS pages. | **P1**   | §4.5          |
| G-20   | Vendor lock-in / exit | Postgres + FastAPI + ReShare. Exit story = "stand up own mod-rs."       | Documented data export path (saga events as JSONL) + import schema for adjacent tools. | **P2**   | §10          |

**Severity legend:** P0 = blocks pilot, P1 = blocks GA, P2 = blocks scale.

## 3. Phased path to production

> **Leadership view.** This section is what you sign off on. Each
> phase has explicit *exit criteria* — these are the deliverables
> ops will produce before asking for the next budget commitment.
> Three pre-Phase-1 gates are non-negotiable: ReShare two-tenant
> verification (G-03), patron PII retention policy (G-07), and OIDC
> auth (G-01). Without all three, do not start the pilot.

Three phases. Each phase has explicit entry+exit criteria tied to
gap IDs. Don't skip; don't blur boundaries.

### Phase 0 — Sandbox parity (where we are)

**Status:** This is master today.
**Goal:** Demo + tests + docs are credible enough to commit pilot resources.
**Evidence:** 553 tests collected (542 pass + 11 skipped), 18 ADRs, mod-rs 2.19 sandbox probed (#56 → CLAUDE.md), routing-LLM eval top-1 0.95, 36 of 42 audit findings closed in the 2026-05-09 remediation sprint (`docs/security-audits/2026-05-09.md`, `docs/SECURITY_MODEL.md` § 7).
**Exit criteria** (all true to enter Phase 1):
- [ ] Two-tenant ReShare probe complete (G-03).
- [ ] Live mod-ncip probe against pilot consortium's primary ILS (G-04).
- [ ] OIDC SSO behind a feature flag, default off (G-01).
- [ ] Backup + restore drill documented and rehearsed once (G-11).
- [ ] Threat-model walkthrough (G-17), output committed under `docs/security/`.

### Phase 1 — Pilot (1 consortium, 50 staff, opt-in patrons)

**Goal:** Run real ILL traffic for one consortium for ≥ 90 days. ≤ 100 sagas/day. Read-only patron portal, gated to opted-in patrons.
**SLOs:** see §6.4. Pilot relaxes them by 2× to absorb learnings.
**Exit criteria** (all true to enter Phase 2):
- [ ] G-01, G-02, G-07, G-08, G-11, G-13 closed.
- [ ] G-03, G-04 verified end-to-end against pilot ILS + ReShare tenant for ≥ 30 days.
- [ ] DR drill completed once with documented RPO/RTO measurements (G-11).
- [ ] On-call rotation operating (G-18) — at least one real page resolved per the runbook.
- [ ] Post-pilot retrospective — what changed in the gap matrix, what's new.

### Phase 2 — General availability (single consortium, full traffic)

**Goal:** Sole production system for the pilot consortium. Patron portal active. ≥ 500 sagas/day.
**Exit criteria** (steady-state, not for promotion):
- [ ] All P0 + P1 gaps closed.
- [ ] Quarterly restore drill green.
- [ ] Annual threat-model refresh + dependency audit (`make audit` + manual review).
- [ ] LLM tie-breaker accuracy (`evals/routing/baseline.json`) within 0.02 of last published baseline.

### Phase 3 — Multi-tenant / multi-consortium (out of current scope)

Treat as a separate program. G-15, G-16, G-20 become critical.
Likely requires: per-tenant DB or row-level security, separate
control vs. data planes, contract-grade SLAs, dedicated SRE function.

## 4. Security & privacy

> **Leadership view.** Three items in this section need legal/policy
> review, not just engineering: §4.3 (patron PII retention — likely
> needs library counsel sign-off given state library-record statutes),
> §4.4 (annual threat model — assign an owner), and the auth model
> ADR (§4.1, ADR-0018 reserved). Budget legal time, not just eng time.

### 4.1 Authentication (G-01)

Today: none. Production needs:

- **Staff console** — OIDC SSO. Library SSO (Shibboleth/InCommon
  federation) is common in academic consortia; pure Azure AD/Google
  Workspace covers public-library systems. FastAPI middleware
  (e.g. `authlib`, `fastapi-users`) gates all `/ui/*` and
  `/sagas/*` routes. Session cookie scoped to first-party origin only.
- **API service-to-service** — bearer token (issued by the SSO
  provider with a service-account scope) for the outbox worker
  calling ReShare / NCIP. **Not** API keys checked into code.
- **Patron portal (G-19)** — initially patron-ID + email-link
  (magic link) is acceptable for opt-in patrons; full SSO is
  Phase 2. Lock down with rate limits.

ADR needed before Phase 1: **ADR-0018 — staff/patron auth model.**

### 4.2 Authorisation (G-02)

Roles to introduce:

| Role               | Can do                                                                       |
| ------------------ | ---------------------------------------------------------------------------- |
| `viewer`           | Read sagas, read patrons, read events. No mutations.                         |
| `approver`         | All viewer + commit gates (`POST /sagas/{id}/approve`, `/compensate`, `/override`, `/renew`). |
| `admin`            | All approver + manage consortium roster, env-var tweaks, dead-letter purge.  |
| `relay`            | Service identity used by ReShare/NCIP outbox handlers. No console access.    |

Implementation: claim-based check at the FastAPI dependency layer
(`Depends(require_role("approver"))`); RBAC matrix tested in
`tests/test_authz.py` (does not yet exist).

### 4.3 Patron PII retention (G-07)

The saga ledger today retains:
- `patron_id`, `patron_type` (in `saga_event.payload`)
- `item_barcode` (when supplied)
- Optional `patron_email` (portal magic-link, when implemented)

ALA model policy and many state library-record statutes (e.g.
California Govt Code 6267, Illinois 75 ILCS 70) require destruction
of borrower records once the transaction completes and any disputes
are resolved. Production policy:

- **90 days post-terminal-state** (Returned / Cancelled / Unfilled
  with no open dispute), patron-identifying fields are scrubbed in
  place: `patron_id` → hash, `item_barcode` → null, `patron_email`
  → null. Saga lifecycle and event timeline preserved (anonymised).
- **DSAR support**: `GET /admin/patron/{patron_id}/sagas` (admin
  role only) returns all sagas + scrub status. `POST /admin/patron/{patron_id}/forget`
  triggers immediate scrub regardless of age.
- **Audit log** of every scrub, immutable, off-system.

ADR needed before Phase 1: **ADR-0019 — patron PII retention.**

### 4.4 Threat model (G-17)

Out-of-scope for this doc; the punch list is:

- Saga ledger as audit-of-record — what protects integrity? (Today:
  app-layer UNIQUE constraint and append-only convention; no
  database-layer hardening.)
- Outbox worker as the *only* component that talks to peers — what
  if it's compromised? (Today: same trust boundary as the rest of
  the API.)
- Staff-console XSS through patron citation fields — Jinja2
  autoescape is on; verify under `pragma: HTML-safe?` annotations.
- LLM tie-breaker prompt-injection via patron-supplied citation
  text — currently the prompt template renders the citation
  verbatim. **Mitigation:** sanitise + length-cap input;
  rules-fallback when output schema fails. See [`routing_tiebreak_prompt.py`](../src/agora/agents/routing_tiebreak_prompt.py).

### 4.5 Patron portal hardening (G-19)

- **Rate limit** `/portal/login` and `/portal/requests/*` — start
  at 30 req/min/IP, tune from access logs.
- **No-cache** on `/portal/requests/*` responses (patron PII).
- **Patron-ID enumeration** — current behaviour returns 404 for both
  unknown patron and patron-with-no-sagas. Don't add a "did you mean"
  hint that would discriminate.
- **Browser back-button** — magic-link tokens single-use, ≤ 15-min
  TTL, and the resulting session cookie scoped to `/portal/*` only
  so a patron at a shared library terminal can't expose their
  history by hitting Back.

## 5. Standards integration

### 5.1 ReShare / mod-rs (G-03)

What's verified (sandbox probe 2026-05-06, mod-rs 2.19.0-rc17, see
`CLAUDE.md` "Known gaps"):

- Body shape — camelCase fields accepted on POST.
- Response shape — `id` is UUID, `state.code` is the discriminator,
  `hrid`/`isoMessageId`/`supplyingAgencyId` may be null on basic
  responses.
- mod-rs **does not honour `Idempotency-Key`** — replay safety
  lives entirely in the saga ledger UNIQUE constraint. Document this
  in the runbook §10 (new section needed).
- No requester-initiated recall action exists in `Actions.groovy`.
  [ADR-0016](adr/0016-compensate-ship-via-manualclose.md) elects
  `manualClose` as the SHIP-compensator path; this still needs a
  live two-tenant test before Phase 1 cutover.

What's not verified:

- Requester-side state path (`REQ_*` states). Probe created a
  Responder-side record because `supplyingInstitutionSymbol` was
  set; we still don't have a live trace of a borrower-tenant flow.
- `manualClose` semantics on the supplier tenant (does it close the
  supplier-side record cleanly, or leave a zombie?).

Production CI: smoke test against a long-lived staging tenant on
every release. Failure → block deploy.

### 5.2 NCIP / mod-ncip (G-04)

Today:
- `HttpNcipClient` shipped (PR #98/#99) — source-reviewed only.
- `MockNcipClient` is the default per-`AGORA_NCIP_BASE_URL`-absent
  factory.
- Smoke-test scaffolding (`tests/test_ncip_http_smoke.py`, #101)
  gated on `AGORA_TEST_NCIP_*` env vars; not run in CI.

Production needs round-trip verification against the consortium's
ILSes. Each ILS has its own NCIP-toolkit bugs:

- **Folio mod-ncip** — actively maintained, easiest path.
- **Sierra** — III's NCIP responder; field-name quirks documented
  in their support portal.
- **Alma** — Ex Libris NCIP service; auth via X-Institution-Code.

Pilot scope: pick one. GA scope: at least the consortium's two
most-deployed ILSes pass the smoke test in nightly CI against a
test institution code.

### 5.3 ISO 18626 conformance (G-05)

The wire is mod-rs's responsibility. Agora's exposure is bounded:

- We **don't** generate ISO 18626 XML directly today.
- The validator at `scripts/validate_iso18626.py` and fixtures
  under `tests/fixtures/iso18626/` exercise the harness on every PR.
- The real ISO 18626 v1.3 XSD is an opt-in cache step at
  `docs/standards/iso18626/iso18626-v1_3.xsd` (see that directory's
  README).

**For production:** bundle the XSD (or fetch in the build step)
and fail CI if any code path generates a payload that doesn't
validate. This catches a regression where future code routes around
mod-rs (e.g. peer-to-peer relay outside ReShare).

### 5.4 LLM tie-breaker (G-06)

Production checklist:

- Vertex project owned by ops, not the dev's personal `gcloud auth`.
- Quota-project pinned (`gcloud auth application-default
  set-quota-project <prod-project>` — also relevant per the
  GCP-auth warning surfaced at session start).
- Studio click-through enablement on the prod project.
- Per-environment model selection — pilot uses `gemini-2.5-flash`,
  evaluate `gemini-2.5-pro` for accuracy/cost trade-off.
- LLM-call audit: every tie-break attempt writes an OBSERVATION
  event with the prompt+response (or hash thereof) so the rationale
  is reconstructable post-hoc. Today the rationale lives only in
  routing-decision logs, not in the saga ledger.
- **Rules-fallback monitoring** — silent fallback (LLM down, schema
  invalid, abstain) is by design (advisory contract per ADR-0005)
  but should emit a metric (G-10).

## 6. Observability & SLOs

> **Leadership view.** Pick a sink (§6.2). Self-hosted Grafana/Loki
> ≈ $30/mo + 0.25 FTE; managed Datadog/Splunk ≈ $300+/mo + minimal
> ops time. The trade-off is real and recurring — make it
> deliberately, not by drift. SLOs in §6.4 are the contract you
> publish to your consortium.

### 6.1 What runbook.md already covers

`runbook.md` covers normal startup, outbox/scanner cadence, and 9
common failure modes (GPG, mypy noise, lifespan in tests, settings
cache, SQLite quirks, gates, recall, Postgres port). Use it.

### 6.2 Logging (G-08)

- Today: structlog JSON to stdout. Saga ID, step, actor, idempotency
  key bound on every line.
- Production: ship to a centralised sink with structured-query
  support. Loki + Grafana is the cheapest credible stack;
  CloudWatch / Datadog / Splunk if procurement is easier.
- Retention:
  - **Saga events** (`saga_event` table): ≥ 1 year (audit horizon).
  - **App logs**: ≥ 90 days.
  - **Access logs**: ≥ 1 year (compliance).
- Log integrity: append-only sink + periodic hash anchoring (sign
  daily log digests with a key in a separate trust boundary from
  the API). Cheap, satisfies most state library-record audits.

### 6.3 Tracing (G-09)

OpenTelemetry plan:

- **FastAPI** — `opentelemetry-instrumentation-fastapi`. Trace ID
  propagated as `X-B3-TraceId` (or W3C TraceContext) into outbox
  rows.
- **Outbox worker** — span per delivery attempt, child of the
  forward-step span that enqueued the row.
- **Tracking scanner** — span per scan pass; child spans per saga
  observation written.
- **External calls** — ReShare/NCIP/SRU/CrossRef/Vertex all
  instrumented; trace ID propagated via standard W3C headers (mod-rs
  ignores them today; document and move on).
- Sink: Tempo / Honeycomb / Datadog APM.

### 6.4 Metrics & SLOs (G-10)

Metrics to publish (Prometheus exposition or OTLP):

| Metric                                | Type      | Used for SLO?                |
| ------------------------------------- | --------- | ---------------------------- |
| `saga_forward_duration_seconds`       | histogram | yes — gate latency           |
| `saga_compensator_duration_seconds`   | histogram | yes — recovery latency       |
| `outbox_queue_depth{status="pending"}`| gauge     | yes — outbox lag             |
| `outbox_dead_letter_total`            | counter   | yes — error budget           |
| `tracking_scanner_pass_duration_seconds` | gauge   | freshness check              |
| `routing_llm_calls_total{outcome=…}`  | counter   | LLM availability + fallback rate |
| `gate_latency_seconds`                | histogram | yes — staff perceived perf   |
| `db_pool_in_use`                      | gauge     | capacity                     |

**Pilot SLOs** (Phase 1; tighten in Phase 2):

| SLO                                        | Target  | Window      |
| ------------------------------------------ | ------- | ----------- |
| `/sagas/*` API availability                | 99.5%   | 30 days     |
| Gate latency (commit → response)           | p95 < 2s | 30 days    |
| Outbox queue lag (oldest pending → in-flight) | p95 < 60s | 30 days |
| Outbox dead-letter rate                    | < 1% of outbox rows | 30 days |
| Saga progression freshness (no stalled-stuck > 24h) | 99% | 30 days |

GA SLOs tighten availability to 99.9%, gate latency p95 < 1s,
outbox lag p95 < 30s.

## 7. Data plane

> **Leadership view.** RPO/RTO targets in §7.1 are the consortium's
> recovery contract. A 4-hour RTO means a 4-hour outage is a normal
> Tuesday, not an incident. Tighten only if you can fund the
> standby + drill cadence to back it up.

### 7.1 Backups & DR (G-11)

Production Postgres (single-cluster, primary + 1 hot standby):

- **Continuous WAL archiving** to off-cluster object storage
  (S3 / GCS / Azure Blob) with server-side encryption.
- **Daily logical dumps** (`pg_dump --format=custom`) retained 30
  days, monthly archived to cold storage 1 year.
- **Point-in-time recovery** target: any timestamp in the last 7
  days.
- **RPO ≤ 15 min, RTO ≤ 4 h.**
- **Restore drill:** quarterly. Drill = restore latest dump to a
  scratch instance, run `make test`, verify `tests/test_alembic_postgres.py`
  green against the restored DB. Document in `runbook.md` § (new
  DR section needed).

### 7.2 Migrations under load (G-12)

Today: `tests/test_alembic_postgres.py` exercises forward + downgrade
+ ORM/metadata parity in CI against an empty `postgres:15-alpine`.

Production needs:

- **Online-DDL discipline** — no `ALTER TABLE` that takes an
  `ACCESS EXCLUSIVE` lock on `saga_event` or `outbox_row`. Use
  `pg_repack` or careful rewrite if a column needs to change type.
- **Pre-prod rehearsal** — every migration runs against a clone of
  prod (`pg_dump`-loaded staging) within 24 h of the deploy
  scheduled for prod.
- **Rollback playbook** — Alembic downgrade only works if the
  migration was reversible. We exercise round-trip in CI; document
  the operator command in the §9.3 runbook addition.

### 7.3 Schema evolution (lessons baked in)

`docs/lessons.md` documents the trapdoors:

- Lifecycle column is `VARCHAR`, not Postgres ENUM — adding a state
  needs no DDL but does need an empty-marker revision.
- BigInteger PKs on Postgres / Integer on SQLite (helper:
  `_bigint_pk()`).
- UUID columns via `_PortableUUID` TypeDecorator.

These are testable invariants — keep them in CI.

## 8. Deployment, scaling & cost

> **Leadership view.** §8.4 has the running-cost table. At pilot
> sizing the system runs ≈ $400–$900/mo all-in. Engineering time
> dominates; cloud is rounding error. The two cost levers worth
> watching are **observability stack** (self-host vs. managed) and
> **Postgres tier** (right-size the DB, don't over-buy).

### 8.1 CI/CD (G-13)

Today CI runs `triple-gate.yml` (pytest+ruff+mypy), `audit.yml`
(bandit + pip-audit + detect-secrets), `postgres-tests.yml` (alembic
parity), `routing-eval-floor.yml` (rules-baseline floor). All gate
PRs to master.

Production needs:

- **Tagged release** (`v0.x.y`) → containerise → push to private
  registry → deploy to **staging** → automated smoke (subset of
  `tests/test_api.py` against staging) → manual approval → deploy
  to **prod**.
- **Rollback**: deploy = container tag swap; rollback = swap back
  + run last-known-good Alembic head if migration was applied.
- **Database migrations** are *separate* from app deploys: applied
  pre-deploy via a one-shot job, never inline in the app's startup
  path (current code does not run migrations at boot — keep it
  that way).

### 8.2 Multi-tenant readiness (G-15) — Phase 3

The minimal Phase 3 migration:

- Add `tenant_id UUID NOT NULL DEFAULT '<single-tenant-uuid>'` to
  `saga`, `saga_event`, `outbox_row`, `policy_rule`,
  `idempotency_key`. Backfill with the implicit single tenant;
  swap default off.
- Index `(tenant_id, ...)` on every query path.
- Settings become per-tenant: consortium roster, routing weights,
  policy rules, env-var overlays. Today these all live in
  `Settings`/process env.
- Agent rosters per tenant — `DiscoveryAgent.consortium_members`
  comes from a tenant table, not a comma-separated env var.
- Either RLS (Postgres `ROW LEVEL SECURITY` policies) or per-tenant
  schema, depending on isolation requirements. RLS is cheaper;
  per-tenant schema is cleaner for backup-per-tenant restores.

This is one ADR + ~3 weeks of focused engineering. Don't start
until at least one consortium has run Phase 2 for ≥ 6 months.

### 8.3 Capacity (G-14)

Sizing envelope for a *typical* US public-library consortium
(~10 institutions, ~500k catalogue records, ~5k ILL/year):

| Component       | Sizing (start)             | Notes                                            |
| --------------- | -------------------------- | ------------------------------------------------ |
| API             | 2 × 2vCPU/4GB              | Async-first; expect bottleneck at db pool.       |
| Outbox worker   | 1 × 1vCPU/2GB              | Multi-worker safe via `FOR UPDATE SKIP LOCKED` (PR #25). Add second worker only if measured queue lag breaches SLO. |
| Postgres        | 1 × 4vCPU/8GB + 100GB SSD  | `db_pool_size=10` per API node = 20 conns.       |
| Tracking scanner| co-located with API         | One per cluster; lifespan-managed. No external scheduler needed. |

5k ILLs/year ≈ ~14 sagas/day, ~50 events/saga = ~700 events/day.
Trivial. The system's bottleneck under any realistic single-consortium
load is staff gate-latency, not throughput.

**Load-test harness** (build before Phase 1): k6 or Locust scenario
that drives `POST /requests` → `/discover` → `/approve` → `/ship` →
`/receive` → `/return` for N concurrent sagas, asserts the SLOs.
Run nightly.

### 8.4 Cost model (G-16) — illustrative

For the Phase 1 sizing above on a single cloud (AWS-equivalent):

| Item                            | Monthly est. | Notes                                       |
| ------------------------------- | ------------ | ------------------------------------------- |
| 2× API nodes (t4g.medium)       | $50          | spot/savings plan can halve                 |
| Postgres (db.t4g.large + 100GB) | $200         | + WAL archive to S3 ~$10                    |
| Outbox worker                   | $25          |                                             |
| Object storage (logs+backups)   | $30          | depends on retention                        |
| Vertex AI (LLM tie-breaker)     | $5–$50       | 14 sagas/day × ~30% tie-break rate × ~2k input tokens × $0.075/M = ~$0.20/day. Pro model 5×. |
| Observability (managed)         | $100–$500    | self-host Loki/Grafana for ~$30 vs. Datadog $300+ |
| **Total**                       | **~$400–$900** | excludes staff time, ReShare/FOLIO licences |

Cost will be dominated by **observability** and **Postgres** at
this scale — the LLM is rounding error. Self-hosting the
observability stack is the single biggest lever. Update this table
with actuals after Phase 1.

## 9. Operations cadence

> **Leadership view.** This is the staffing ask. One engineer on
> weekly rotation is the floor for a single-consortium pilot; less
> than that and you have no on-call. Two-engineer rotation is the
> healthier minimum for GA. Quarterly DR drills and annual
> threat-model refreshes need calendar holds.

`runbook.md` covers per-incident behaviour. Production needs a
written **rotation** + **calendar**.

### 9.1 On-call rotation (G-18)

- **Tier 1**: 1× engineer on weekly rotation. Pages on:
  - API availability SLO burn (alert from §6.4 metrics).
  - Outbox dead-letter spike (>5 in 1h).
  - Tracking scanner heartbeat missing > 30 min.
  - Postgres replication lag > 60s.
- **Tier 2**: project lead, paged only if Tier 1 escalates.
- **Severity ladder**: SEV-1 = pilot consortium can't transact ILL;
  SEV-2 = degraded but workable (e.g. portal down, console up);
  SEV-3 = SLO burn but no user impact.

### 9.2 Calendar

| Cadence    | Activity                                                                      |
| ---------- | ----------------------------------------------------------------------------- |
| Daily      | Dead-letter triage (5 min). Deploy queue check.                               |
| Weekly     | On-call handoff. Open-PR sweep.                                               |
| Monthly    | Cost report. SLO review (was last month's error budget consumed?).            |
| Quarterly  | DR restore drill. Dependency audit (`make audit` + manual review).            |
| Annually   | Threat-model refresh. RBAC review. Peer-tenant probe re-run.                  |

### 9.3 DR runbook (new content for `runbook.md`)

A separate addendum, not in this doc. Skeleton:

1. Confirm primary-DB unrecoverable.
2. Spin up replacement instance (Terraform module reference).
3. Restore latest WAL-archived snapshot to PITR target.
4. Run `alembic current` — verify head matches deployed app version.
5. Cut DNS / connection-string swap.
6. Replay `outbox` rows in `in_flight` status (orphan recovery
   sweeps them back to `pending` automatically — see PR #25).
7. Notify consortium per comms template.

## 10. Vendor / lock-in story (G-20)

Agora is intentionally light on lock-in:

- **Saga events** are JSONB rows; export via
  `pg_dump --table=saga_event --data-only --column-inserts` is
  viable.
- **No proprietary serialisation** — everything is JSON.
- **Runtime** — FastAPI + Postgres run on any compliant Linux host.
  No managed-service bindings (no Lambda, no managed-Vertex
  *required* path; LLM tie-breaker is degradable).
- **Standards layer** — ReShare/mod-rs and mod-ncip are open-source.
  The wire conformance lives there.

Exit story (operationally): "stand up own mod-rs" is realistic if
the consortium parts ways with FOLIO. Document the data export +
re-import path in a "Migrate-out" section of the runbook before
Phase 2.

## 11. Decision log for future productionization

Track each Phase-0 → Phase-1 transition decision as an ADR. Names
already reserved:

- ADR-0018 — Auth model (G-01, G-02)
- ADR-0019 — Patron PII retention (G-07)
- ADR-0020 — DR target / RPO / RTO (G-11)
- ADR-0021 — Multi-tenant strategy (G-15) *(deferred to Phase 3)*

These ADRs are the load-bearing artefacts that turn this document
from a wish list into a delivery plan. When you write one, link it
back to the gap row(s) it closes and update §2.

---

## References

- [`CLAUDE.md`](../CLAUDE.md) — invariants, known gaps, behavioural rules
- [`runbook.md`](runbook.md) — operational reference (this doc extends it)
- [`solution.md`](solution.md) — solution overview
- [`architecture.md`](architecture.md) — Mermaid diagrams
- [`adr/`](adr/) — architecture decisions, 17 records through ADR-0017
- [`prd/`](prd/) — product requirements, 7 docs
- [`lessons.md`](lessons.md) — accumulated gotchas
- ReShare / mod-rs: https://github.com/openlibraryenvironment/mod-rs
- mod-ncip: https://github.com/folio-org/mod-ncip
- ISO 18626:2021 spec — illtransactions.org
- ALA model patron-record retention policy — https://www.ala.org/advocacy/intfreedom
