"""Single source of truth for test count + ADR count in docs.

Truth is computed at runtime:

* Test count: subprocess ``python -m pytest --collect-only -q tests/`` and
  parse the trailing ``N tests collected`` line.
* ADR count: ``len(list(docs/adr/*.md))``.

A small registry of ``(file, regex)`` pairs declares every doc location
that recites either count. The default ``check`` mode walks the registry,
diffs each captured number against truth, and exits ``1`` on mismatch
(used by CI). The ``--fix`` mode rewrites the docs in place. The
``--report`` mode prints the truth and exits ``0``.

Run from repo root::

    python scripts/sync_doc_counts.py            # check, exit 1 on drift
    python scripts/sync_doc_counts.py --fix      # rewrite docs in place
    python scripts/sync_doc_counts.py --report   # print counts only

The list of patterns below is the canonical place to add a new doc
recitation. Adding ``"**N tests** green"`` somewhere new? Add the file +
regex here and the gate / fix logic flows through automatically.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"


@dataclass(frozen=True)
class CountPattern:
    """One doc location that recites a runtime-computed count."""

    file: Path
    regex: str
    description: str


# Each regex MUST contain exactly one ``(\d+)`` capture group. The script
# substitutes that capture with the runtime-truth value when ``--fix`` is
# active. Keep regexes narrow so they don't accidentally match unrelated
# numbers (e.g. ADR numbers like ``0014``, port numbers like ``5433``).
_TEST_COUNT_PATTERNS: tuple[CountPattern, ...] = (
    CountPattern(
        REPO_ROOT / "README.md",
        r"\*\*(\d+) tests\*\* green",
        "README status line",
    ),
    CountPattern(
        REPO_ROOT / "README.md",
        r"# (\d+) unit \+ property \+ e2e \(\+6 postgres-only\)",
        "README quick-layout tests/ comment",
    ),
    CountPattern(
        REPO_ROOT / "CLAUDE.md",
        r"# (\d+) tests \(\+6 postgres-only\)",
        "CLAUDE.md quick-start verify line",
    ),
    CountPattern(
        REPO_ROOT / "CLAUDE.md",
        r"# (\d+) tests \(unit \+ property \+ e2e\)",
        "CLAUDE.md repo-layout tests/ comment",
    ),
    CountPattern(
        REPO_ROOT / "docs" / "prd" / "00-overview.md",
        r"(\d+) tests green at time of review",
        "PRD-00 success criteria",
    ),
    CountPattern(
        REPO_ROOT / "docs" / "solution.md",
        r"- (\d+) tests across unit, property",
        "solution.md test strategy",
    ),
)

_ADR_COUNT_PATTERNS: tuple[CountPattern, ...] = (
    CountPattern(
        REPO_ROOT / "CLAUDE.md",
        r"adr/  \((\d+) docs\)",
        "CLAUDE.md repo-layout adr/ comment",
    ),
    CountPattern(
        REPO_ROOT / "docs" / "solution.md",
        r"architecture decisions \((\d+) docs;",
        "solution.md ADR ToC line",
    ),
    CountPattern(
        REPO_ROOT / "docs" / "prd" / "00-overview.md",
        # Newline tolerant — file is CRLF on Windows checkouts.
        r"ADRs\r?\n  \((\d+)\)",
        "PRD-00 success criteria ADR count",
    ),
)


def _collect_test_count() -> int:
    """Return the number of tests pytest collects from ``tests/``.

    Subprocess so the count is independent of the current invocation
    (running ``pytest tests/test_doc_counts.py`` alone must still report
    the full-suite truth, not 2).
    """
    proc = subprocess.run(  # nosec B603  # python -m pytest, trusted args
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 5):
        # 5 = pytest "no tests collected"; everything else is unexpected.
        raise RuntimeError(
            f"pytest --collect-only failed (rc={proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    match = re.search(r"(\d+) tests collected", proc.stdout)
    if match is None:
        raise RuntimeError(
            "could not parse pytest collection output:\n" + proc.stdout
        )
    return int(match.group(1))


def _adr_count() -> int:
    """Return the number of ADR markdown files (excluding any README)."""
    return len(
        [p for p in ADR_DIR.glob("*.md") if p.name.lower() != "readme.md"]
    )


@dataclass(frozen=True)
class Drift:
    pattern: CountPattern
    found: str
    expected: str

    def __str__(self) -> str:
        rel = self.pattern.file.relative_to(REPO_ROOT).as_posix()
        return (
            f"  {rel}: {self.pattern.description} — found {self.found}, "
            f"expected {self.expected}"
        )


def _read_preserving_endings(path: Path) -> str:
    """Read text without converting CRLF→LF (so writes round-trip cleanly)."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write_preserving_endings(path: Path, text: str) -> None:
    """Write text without inserting platform-default newlines."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _scan(patterns: tuple[CountPattern, ...], expected: int) -> list[Drift]:
    drifts: list[Drift] = []
    for pat in patterns:
        text = _read_preserving_endings(pat.file)
        # Each declared pattern MUST appear at least once; if it doesn't
        # the registry is stale (the doc was edited away from the
        # canonical phrase). Surface as drift so the registry stays
        # honest.
        matches = list(re.finditer(pat.regex, text, flags=re.MULTILINE))
        if not matches:
            drifts.append(Drift(pat, "<no match>", str(expected)))
            continue
        for m in matches:
            if m.group(1) != str(expected):
                drifts.append(Drift(pat, m.group(1), str(expected)))
    return drifts


def _fix(patterns: tuple[CountPattern, ...], expected: int) -> int:
    """Rewrite each pattern's capture group to ``expected``. Returns count rewritten."""
    rewrites = 0
    for pat in patterns:
        text = _read_preserving_endings(pat.file)

        def _sub(m: re.Match[str]) -> str:
            old = m.group(0)
            return old.replace(m.group(1), str(expected), 1)

        new_text, n = re.subn(pat.regex, _sub, text, flags=re.MULTILINE)
        if n and new_text != text:
            _write_preserving_endings(pat.file, new_text)
            rewrites += n
    return rewrites


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite docs in place to match runtime truth",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print runtime truth and exit",
    )
    args = parser.parse_args(argv)

    test_count = _collect_test_count()
    adr_count = _adr_count()

    if args.report:
        print(f"test count: {test_count}")
        print(f"ADR count:  {adr_count}")
        return 0

    if args.fix:
        n_t = _fix(_TEST_COUNT_PATTERNS, test_count)
        n_a = _fix(_ADR_COUNT_PATTERNS, adr_count)
        print(f"rewrote {n_t} test-count match(es), {n_a} ADR-count match(es)")
        return 0

    drifts = _scan(_TEST_COUNT_PATTERNS, test_count) + _scan(
        _ADR_COUNT_PATTERNS, adr_count
    )
    if drifts:
        print(
            f"doc-count drift detected (test count = {test_count}, "
            f"ADR count = {adr_count}):"
        )
        for d in drifts:
            print(d)
        print("\nrun `python scripts/sync_doc_counts.py --fix` to rewrite.")
        return 1
    print(
        f"OK — {len(_TEST_COUNT_PATTERNS) + len(_ADR_COUNT_PATTERNS)} "
        f"doc recitation(s) match runtime truth "
        f"(test={test_count}, adr={adr_count})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
