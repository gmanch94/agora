"""Audit 2026-05-09 #39 — Jinja template XSS-guard CI check.

Scans ``src/agora/api/templates/*.html`` for patterns that bypass
Jinja2's default autoescape:

- ``|safe`` filter
- ``|raw`` filter
- ``{% autoescape false %}`` blocks
- ``Markup(...)`` calls (require Python-side rendering, but flag if
  someone smuggles them into a template)
- Inline ``onclick=`` / ``onload=`` / other ``on*=`` event-handler
  attributes (data interpolated into JS context skips HTML escaping
  semantics — needs explicit JSON encoding instead)
- ``javascript:`` URI scheme in ``href=`` / ``src=`` attributes
- ``<script>`` blocks containing ``{{ }}`` expressions (data
  rendered directly into JS context — HTML escape doesn't make this
  safe)

Run via ``make audit`` or directly via ``python -m scripts.check_template_xss_guards``.
Exits 0 on clean, 1 with a punch list on findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES_DIR = _REPO_ROOT / "src" / "agora" / "api" / "templates"

# Patterns that should NEVER appear in a safe Jinja template.
_BAD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\{\{[^}]*\|\s*safe\b"), "Jinja `|safe` filter bypasses autoescape"),
    (re.compile(r"\{\{[^}]*\|\s*raw\b"), "Jinja `|raw` filter bypasses autoescape"),
    (re.compile(r"\{%\s*autoescape\s+false\s*%\}", re.IGNORECASE), "explicit autoescape disable"),
    (re.compile(r"Markup\("), "MarkupSafe Markup() smuggled into template"),
    # on*= event handlers in attribute context.
    (
        re.compile(r"\bon[a-z]+\s*=\s*[\"'][^\"']*\{\{"),
        "data interpolated into on*= event handler (use unobtrusive JS instead)",
    ),
    (re.compile(r"javascript:", re.IGNORECASE), "javascript: URI scheme in attribute"),
]


def _check_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (lineno, snippet, message) findings for one file."""
    findings: list[tuple[int, str, str]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip("\n")
            for pattern, message in _BAD_PATTERNS:
                if pattern.search(line):
                    findings.append((lineno, line.strip(), message))
    findings.extend(_check_inline_script_interpolation(path))
    return findings


def _check_inline_script_interpolation(path: Path) -> list[tuple[int, str, str]]:
    """Detect ``<script>...{{ ... }}...</script>`` data interpolation.

    Inline ``<script>`` blocks render data into JS context, where HTML
    escape doesn't prevent string-breakout (a ``"`` in a ``{{ x }}``
    inside a JS string literal escapes the JS quote regardless of
    HTML autoescape). Flag the pattern; safe alternatives are
    ``data-*`` attributes + JS reading them from the DOM.
    """
    findings: list[tuple[int, str, str]] = []
    text = path.read_text(encoding="utf-8")
    # Naive script-block scan; multiline.
    for match in re.finditer(
        r"<script\b(?![^>]*\bsrc=)[^>]*>.*?</script>", text, re.DOTALL | re.IGNORECASE
    ):
        block = match.group(0)
        if "{{" in block or "{%" in block:
            # Locate line number.
            line_no = text[: match.start()].count("\n") + 1
            snippet = block.split("\n")[0][:80]
            findings.append(
                (
                    line_no,
                    snippet,
                    "data interpolated into inline <script> block (use data-* attrs + JS DOM read)",
                )
            )
    return findings


def main() -> int:
    if not _TEMPLATES_DIR.exists():
        print(f"templates dir not found: {_TEMPLATES_DIR}", file=sys.stderr)
        return 2

    all_findings: list[tuple[Path, int, str, str]] = []
    for path in sorted(_TEMPLATES_DIR.glob("*.html")):
        for lineno, snippet, message in _check_file(path):
            all_findings.append((path, lineno, snippet, message))

    if not all_findings:
        print(
            f"OK: {_TEMPLATES_DIR.relative_to(_REPO_ROOT)} — no XSS-guard "
            "violations across "
            f"{sum(1 for _ in _TEMPLATES_DIR.glob('*.html'))} templates."
        )
        return 0

    print("Template XSS-guard violations:")
    for path, lineno, snippet, message in all_findings:
        rel = path.relative_to(_REPO_ROOT)
        print(f"  {rel}:{lineno} — {message}")
        print(f"    {snippet}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
