---
name: iso18626-validate
description: Validate an ISO 18626 XML payload against the published XSD. Use when reviewing/generating peer-facing wire messages, before flipping `RESHARE_ENABLED=true`, or when a peer reports a schema rejection. Catches the common 2021-revision pitfalls (DeliveryMethod rename, namespace drift, missing required header fields).
---

# iso18626-validate

ReShare/mod-rs is the production source of ISO 18626 wire correctness.
But any time we generate or inspect a payload directly — debugging,
adding a new message type, integrating a peer that's strict about
schema — we need to validate against the XSD.

## When to invoke

- User pastes/points to an ISO 18626 XML payload and asks "is this
  valid?"
- Adding a new message type to `clients/reshare.py` or extending the
  payload shape in a forward step
- Peer reports a schema-rejection error and we need to find the
  offending element
- Pre-flight before flipping `reshare_enabled=true` against a real peer

## What to do

### Step 1 — locate / fetch the XSD

The ISO 18626:2021 schema is published by the ISO 18626 maintenance
group. Cache it locally on first use:

```
docs/standards/iso18626/iso18626-v1_3.xsd        (or current version)
docs/standards/iso18626/iso18626-types-v1_3.xsd  (imported types)
```

If not present, fetch from the published location at
illtransactions.org and save under `docs/standards/iso18626/`. **Tell
the user before fetching.** Use `WebFetch`. Pin the version in a
`README.md` alongside the XSDs.

### Step 2 — validate

Use `lxml` (already a project dependency):

```python
from lxml import etree

xsd_doc = etree.parse("docs/standards/iso18626/iso18626-v1_3.xsd")
schema = etree.XMLSchema(xsd_doc)
xml_doc = etree.parse("<payload-path>")  # or etree.fromstring(<bytes>)
schema.assertValid(xml_doc)  # raises etree.DocumentInvalid on failure
```

Run via `.venv/Scripts/python.exe -c "..."` or a small script in
`scripts/validate_iso18626.py`.

### Step 3 — report

On success: print `OK: <element> validates against ISO 18626 v<X>`.

On failure: print the validation error with line/column from
`schema.error_log`, then check for these common issues and call them
out specifically:

- **`DeliveryMethod` vs `deliveryMethod`** — 2021 revision renamed
  this; older clients send the wrong case.
- **Missing `confirmationHeader` fields** — `requestingAgencyId`,
  `supplyingAgencyId`, `timestamp` are required on every message.
- **Namespace drift** — payload uses
  `http://illtransactions.org/2013/iso18626` (v1.0) instead of
  `http://illtransactions.org/2021/iso18626/...` (v1.3).
- **Empty optional elements** — XSD rejects empty `<note/>` etc; emit
  the element only if it has content.
- **Date-time format** — must be ISO 8601 with timezone, not naive.

### Step 4 — log to the project

If validation fails on a payload our code generated, file the finding
in `docs/standards/iso18626/known-issues.md` (create if missing) with
the symptom + fix, so we don't relearn it.

## Don'ts

- Don't validate against a randomly-fetched XSD without recording the
  version. Schema drift across versions is the bug we're trying to
  catch.
- Don't suppress validation errors silently. Either fix the payload
  or write an ADR explaining why we deviate.
- Don't use this to validate inbound messages from ReShare — mod-rs
  has already validated those. Use it on outbound payloads we
  generate, or on payloads from peers a customer is reporting issues
  with.
