# ISO 18626 test fixtures

Two roles for files in this directory:

## 1. Self-test fixtures (always present)

- `minimal.xsd` — hand-rolled tiny schema that exercises the
  ``scripts/validate_iso18626.py`` validator end-to-end.
- `minimal-valid.xml` — passes against `minimal.xsd`.
- `minimal-invalid.xml` — fails against `minimal.xsd` with
  detectable line / column errors.

These let `tests/test_iso18626_validation.py` prove the validation
plumbing works in CI even when the real ISO 18626 XSD is not cached
locally. They are NOT stand-ins for the real schema — they only
exercise the code path in `scripts/validate_iso18626.py`.

## 2. Real-schema fixtures (optional; you cache them)

When you cache the real ISO 18626 v1.3 XSD under
`docs/standards/iso18626/` (see that directory's README), drop sample
peer-facing payloads here as `iso18626-<message-type>.xml` (e.g.
`iso18626-request.xml`, `iso18626-supplyingAgencyMessage.xml`) and the
test harness will validate them automatically.

The fixtures committed in role 1 use a private namespace
(`http://example.test/agora/minimal`) so they cannot accidentally be
mistaken for real ISO 18626 payloads.
