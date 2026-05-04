"""Gate that test count + ADR count in docs match runtime truth.

The same numbers were drifting on every PR cycle — fix-up commits
shipped on 2026-05-04 (#72: 76→212, #73: 212→218 + 10→14, #75 again)
prompted the user feedback "could we keep the numbers in one place
and reference it where required." `scripts/sync_doc_counts.py` is the
single source of truth (registry of doc locations + regexes; reads
truth from runtime); this test asserts a clean run, so drift becomes
an immediate triple-gate failure rather than something a stale-check
sweep catches days later.

To fix a drift locally: `make sync-doc-counts` (or
`python scripts/sync_doc_counts.py --fix`) — the script rewrites the
docs in place against runtime truth. To extend coverage to a new doc
recitation, add a `CountPattern` row in the script's registry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sync_doc_counts.py"


def test_doc_counts_match_runtime_truth() -> None:
    """`scripts/sync_doc_counts.py` must exit 0 against the current tree.

    A non-zero exit means at least one doc recites a stale test count
    or ADR count. The script's stderr/stdout names the file + finding
    so the failure is immediately actionable. Run
    ``python scripts/sync_doc_counts.py --fix`` (or ``make
    sync-doc-counts``) to rewrite.
    """
    proc = subprocess.run(  # nosec B603  # python -m, trusted args
        [sys.executable, str(_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"sync_doc_counts.py reported drift (rc={proc.returncode}). "
        f"Run `make sync-doc-counts` to fix.\n\n"
        f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
    )
