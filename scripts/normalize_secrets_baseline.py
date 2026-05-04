"""Normalize backslashes to forward slashes in `.secrets.baseline`.

`detect-secrets scan` records filenames using the host OS path
separator. On Windows checkouts this produces ``docs\\runbook.md``
entries; the same baseline read from Linux CI normalises to
``docs/runbook.md`` and the audit job rewrites the file (exit 1
with "Please ``git add .secrets.baseline``") on every run, even
when no real secret has changed.

This script flips every ``\\`` to ``/`` in the baseline's
``results`` keys and the per-finding ``filename`` field, idempotent.
Run after ``detect-secrets scan --baseline .secrets.baseline`` on
Windows. Wired into ``make audit`` as a pre-step so a forgotten
manual run can't ship the un-normalised baseline.

Lesson context: ``docs/lessons.md`` 2026-05-04 entry on
``.secrets.baseline`` filename platform-shaping. Second occurrence
on PR #77 prompted promotion from one-shot fix to permanent script
(per the SoT-script-on-second-drift principle codified in the
companion lesson on the same date).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / ".secrets.baseline"


def main() -> int:
    if not BASELINE.exists():
        print(f"ERROR: {BASELINE} not found", file=sys.stderr)
        return 1
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    if "results" not in data:
        print(f"ERROR: {BASELINE} has no 'results' key", file=sys.stderr)
        return 1
    new_results: dict[str, list[dict[str, object]]] = {}
    rewrites = 0
    for fname, findings in data["results"].items():
        norm = fname.replace("\\", "/")
        if norm != fname:
            rewrites += 1
        for f in findings:
            if isinstance(f, dict) and isinstance(f.get("filename"), str):
                f["filename"] = f["filename"].replace("\\", "/")
        new_results[norm] = findings
    data["results"] = new_results
    BASELINE.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"normalized {rewrites} backslash filename(s) in {BASELINE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
