"""ISO 18626 XSD validator — small, dependency-light wrapper over lxml.

Usage::

    python scripts/validate_iso18626.py \
        --xsd docs/standards/iso18626/iso18626-v1_3.xsd \
        --xml path/to/payload.xml

Exit codes:
- ``0`` — payload validates
- ``1`` — payload fails validation; errors printed to stderr
- ``2`` — XSD missing or unreadable

Why this script (and not just ``xmllint``):

- ``lxml`` is already a project dependency (no new tooling on the
  contributor's path).
- We want the *same* validation surface in CI, in tests, and from the
  ``iso18626-validate`` skill — one entry point, one error format.
- Future-proofing: when we eventually emit ISO 18626 XML directly
  (today mod-rs handles the wire), the same harness validates our
  output against the cached XSD before ship.

The companion ``tests/test_iso18626_validation.py`` exercises this
script against a hand-rolled minimal XSD so CI proves the validation
plumbing works even when the real ISO 18626 XSD is not cached locally.
The real-XSD test path skips cleanly when
``docs/standards/iso18626/iso18626-v1_3.xsd`` is absent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lxml import etree


def validate(xsd_path: Path, xml_path: Path) -> tuple[bool, list[str]]:
    """Validate ``xml_path`` against ``xsd_path``.

    Returns ``(ok, errors)``. ``errors`` is a list of human-readable
    one-line strings; empty on success. Imports inside lxml's
    ``etree.XMLSchema`` are followed automatically — for the ISO 18626
    pair this means pointing at the top-level schema; the imported
    types schema is resolved by ``schemaLocation`` in the parent.
    """
    if not xsd_path.is_file():
        return False, [f"xsd not found: {xsd_path}"]
    if not xml_path.is_file():
        return False, [f"xml not found: {xml_path}"]

    try:
        xsd_doc = etree.parse(str(xsd_path))
        schema = etree.XMLSchema(xsd_doc)
    except etree.XMLSyntaxError as exc:
        return False, [f"xsd parse error: {exc}"]
    except etree.XMLSchemaParseError as exc:
        return False, [f"xsd schema error: {exc}"]

    try:
        xml_doc = etree.parse(str(xml_path))
    except etree.XMLSyntaxError as exc:
        return False, [f"xml parse error: {exc}"]

    if schema.validate(xml_doc):
        return True, []

    errors = [
        f"line {err.line}, col {err.column}: {err.message}"
        for err in schema.error_log
    ]
    return False, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an ISO 18626 XML payload against the published XSD.",
    )
    parser.add_argument(
        "--xsd",
        type=Path,
        required=True,
        help="path to the XSD (e.g. docs/standards/iso18626/iso18626-v1_3.xsd)",
    )
    parser.add_argument(
        "--xml",
        type=Path,
        required=True,
        help="path to the XML payload to validate",
    )
    args = parser.parse_args(argv)

    if not args.xsd.is_file():
        print(f"ERROR: xsd missing at {args.xsd}", file=sys.stderr)
        return 2

    ok, errors = validate(args.xsd, args.xml)
    if ok:
        print(f"OK: {args.xml} validates against {args.xsd}")
        return 0

    print(f"FAIL: {args.xml} does not validate against {args.xsd}", file=sys.stderr)
    for err in errors:
        print(f"  - {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
