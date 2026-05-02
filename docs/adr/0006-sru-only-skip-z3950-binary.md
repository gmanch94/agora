# ADR 0006 — SRU-only discovery; skip Z39.50 binary protocol

**Status:** Accepted
**Date:** 2026-05-02

## Context

Z39.50 is a 1988-era TCP-binary protocol (BER-encoded) for library
catalog search. SRU (Search/Retrieve via URL) is its REST/HTTP
successor; SRW is a SOAP variant. Most modern catalogs (LoC, OCLC,
many ILS) expose both. Some legacy targets expose Z39.50 only.

Implementing Z39.50 in Python requires `pyz3950` or `yaz` bindings —
both pulled, neither well-maintained on Windows.

## Decision

Discovery layer speaks SRU (HTTP) only. Targets that don't expose SRU
are excluded from the prototype's holders list, with a logged note.

OpenURL parsing is a pure string concern; no protocol issue there.

## Consequences

**Positive**
- One HTTP-only client; trivial to test and run locally.
- Cross-platform clean (no native deps).
- All major modern catalogs are covered.

**Negative**
- Some smaller libraries with Z39.50-only catalogs are invisible.
  Prototype impact: none (sandbox catalogs all support SRU). Production
  impact: tracked; revisit when use case appears.

## Implementation

`src/agora/clients/sru.py` — `httpx.AsyncClient` against SRU 1.1 / 2.0
endpoints, CQL query, MARCXML response parsing.

## Future

If production need arises, add a thin adapter that wraps `yaz-client`
via subprocess, since native Python Z39.50 libs are unmaintained.
