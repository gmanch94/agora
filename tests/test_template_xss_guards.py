"""Audit 2026-05-09 #39 — CI guard for Jinja template XSS bypasses.

Runs ``scripts/check_template_xss_guards.py`` and asserts a clean
exit. The script enumerates patterns that bypass Jinja2's default
autoescape (`|safe`, `|raw`, `{% autoescape false %}`, `Markup()`,
inline `on*=` event handlers, `javascript:` URIs, data interpolated
into inline `<script>` blocks).

This is the CI-as-enforcement-layer pattern from the global security
rules — a code-grep gate that catches the next regression without
needing a full audit pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_template_xss_guards.py"


def test_templates_pass_xss_guard_audit() -> None:
    """All HTML templates must be free of autoescape bypasses.

    A failure here means either:

    1. Someone added ``|safe`` / ``|raw`` / ``Markup(...)`` / a
       ``javascript:`` URI / an ``on*=`` event handler with
       interpolated data — fix the template (use HTML-escaped output;
       move JS to unobtrusive listeners; data-* attrs for inline
       data); OR
    2. Someone added a legitimate use-case the script doesn't know
       about — extend ``_BAD_PATTERNS`` in ``scripts/check_template_xss_guards.py``
       with a more specific match instead of broadening the existing
       patterns.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "Template XSS-guard script reported violations:\n"
        f"  stdout:\n{result.stdout}\n"
        f"  stderr:\n{result.stderr}\n"
        "Fix the template or extend scripts/check_template_xss_guards.py."
    )
