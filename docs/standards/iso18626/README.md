# ISO 18626 cached schemas

This directory is the cache target for the ISO 18626 XSDs used by:

- `scripts/validate_iso18626.py` — runtime + CI validator
- `tests/test_iso18626_validation.py` — real-schema test path
  (skips cleanly when the XSD is absent)
- `.claude/skills/iso18626-validate` — ad-hoc validation
  during PR review

The XSDs are **not bundled in the repo by default**. Reasons:

1. **Source-of-truth pinning.** The canonical ISO 18626 schemas are
   published by the ISO 18626 maintenance group at
   <https://www.illtransactions.org/>. Cache one specific version
   here and pin it in this README rather than committing whatever
   version was current the day the script was written.
2. **License clarity.** The XSD files have their own redistribution
   terms; deferring to a per-deployment cache step keeps the
   prototype's license footprint clean.
3. **Network availability.** This repo has no live consumer of the
   wire today (mod-rs handles ISO 18626 wire-level correctness — see
   `docs/architecture.md`). Bundling the XSD ahead of a real
   integration is dead weight in the diff.

## What runs always vs cached opt-in

Two layers, both shipped in #52:

- **Always-on (CI + local).** `tests/test_iso18626_validation.py`
  exercises `scripts/validate_iso18626.py` against the hand-rolled
  minimal fixtures under `tests/fixtures/iso18626/` (`minimal.xsd`,
  `minimal-valid.xml`, `minimal-invalid.xml`) on every PR. This
  proves the validator plumbing — XSD parsing, XML parsing, lxml
  schema bind, error surfacing — without depending on the real
  ISO XSD being present.
- **Opt-in (per-developer cache).** Real ISO 18626 v1.3 schema
  validation kicks in once you complete the Cache step below. The
  same test file then picks up the cached XSD path; until then the
  real-schema test path skips with a clear "missing file" message.

## Cache step (one-time, manual)

When you need to validate real ISO 18626 payloads (review pre-flight
before flipping `RESHARE_ENABLED=true`, peer-reported schema
rejection debug, etc.):

1. Visit <https://www.illtransactions.org/>, navigate to the ISO
   18626 page, and download the current revision's XSD pair (top
   schema + imported types schema).
2. Save under this directory as:
   - `iso18626-v1_3.xsd` (top schema)
   - `iso18626-types-v1_3.xsd` (imported types)
   - Adjust filenames if the published version is different; update
     this README's "Currently cached" line.
3. Append a row to `Currently cached` below with the version + date
   you fetched it.
4. Run `python scripts/validate_iso18626.py --xsd
   docs/standards/iso18626/iso18626-v1_3.xsd --xml <payload>` to
   smoke-test the harness end-to-end against a peer payload.

The companion `tests/test_iso18626_validation.py` real-schema test
path will start exercising automatically once the XSD lands; until
then the test skips with a clear message naming the missing file.

## Currently cached

(none — populate per the cache step above)

## Why we test the harness without the real XSD

`tests/test_iso18626_validation.py` ships hand-rolled minimal
fixtures under `tests/fixtures/iso18626/` (`minimal.xsd`,
`minimal-valid.xml`, `minimal-invalid.xml`) so the validation plumbing
in `scripts/validate_iso18626.py` is exercised in CI even when this
directory is empty. The real-XSD test path is additive — it adds
real-schema confidence on top of the always-on plumbing test.

## Companion tools

- `.claude/skills/iso18626-validate/SKILL.md` — full procedure for
  ad-hoc validation, including the common 2021-revision pitfalls
  (DeliveryMethod rename, namespace drift, missing
  `confirmationHeader` fields).
