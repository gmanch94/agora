# ADR 0009 — Docker Compose for ReShare sandbox

**Status:** Accepted
**Date:** 2026-05-02

## Context

ReShare requires Postgres, Kafka, Okapi, and several FOLIO modules to
run. Manual setup is error-prone and platform-specific. We need a
one-command bring-up for development.

The official ReShare project provides a [`reshare-docker`][1] compose
recipe. We can either use it directly, fork, or write our own.

[1]: https://github.com/openlibraryenvironment/reshare-docker

## Decision

Use the upstream `reshare-docker` compose recipe as a git submodule
or external dependency. Agora's own `docker-compose.yml` defines:

- Agora's Postgres (separate from ReShare's, to keep schemas isolated)
- Agora's API service
- Agora's mock NCIP responder for testing

Agora services connect to ReShare via its Okapi gateway URL configured
through `.env`.

## Consequences

**Positive**
- One `make up` brings the world online.
- Upstream maintains ReShare; we just consume.
- Schema isolation prevents accidental cross-writes during dev.

**Negative**
- Resource-heavy (~4 GB RAM minimum for the full stack).
- ReShare boot time is multi-minute; a `make wait-ready` helper that
  polls Okapi health was planned but later dropped from the Makefile —
  the Postgres-only sandbox doesn't need it.
- Requires Docker Desktop on Windows (user environment is Windows).

## Implementation note

`docker-compose.yml` will reference ReShare via:
```yaml
include:
  - vendor/reshare-docker/docker-compose.yml
```
(or git submodule). For the prototype's first cut, write a stub
compose with just Agora's own services and a mock ReShare HTTP
responder so the team can iterate without running the full FOLIO
stack.
