"""PostToolUse hook: flag stale ADR references in docs writes.

Agora invariant: every ADR mention (e.g. "ADR-0007", "0011-outbox-...")
in a docs file must map to a real file under ``docs/adr/``. This
hook catches the kind of drift fixed in PR #6 (PRD-06 cited ADR-0008
for FedRAMP — the actual ADR is 0007) before it lands.

Triggers on Write/Edit when ``file_path`` is under ``docs/`` and ends
in ``.md``. For each 4-digit ADR identifier mentioned in the new
content, verifies a matching file exists in ``docs/adr/``.

Exits 2 with stderr to send Claude back to fix. Exits 0 silently
when there is no drift.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Match 4-digit ADR ids in a few common shapes:
#   "ADR-0007", "ADR 0007", "adr-0007", "0011-outbox-commit-then-enqueue.md"
ADR_REF = re.compile(
    r"\b(?:ADR[-\s]*)?(0\d{3})(?:-[a-z0-9-]+(?:\.md)?)?",
    re.IGNORECASE,
)


def _content_for(tool: str, ti: dict) -> str:
    if tool == "Edit":
        return str(ti.get("new_string", ""))
    if tool == "Write":
        return str(ti.get("content", ""))
    return ""


def _project_dir() -> Path | None:
    """Resolve project root via env or git fallback."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    # Fallback: walk up from this hook's dir.
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / ".claude").is_dir() and (parent / "docs").is_dir():
            return parent
    return None


def _existing_adrs(adr_dir: Path) -> set[str]:
    """Return the set of 4-digit ADR ids that have files."""
    out: set[str] = set()
    if not adr_dir.is_dir():
        return out
    for p in adr_dir.glob("*.md"):
        m = re.match(r"(0\d{3})-", p.name)
        if m:
            out.add(m.group(1))
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool = data.get("tool_name")
    if tool not in ("Edit", "Write"):
        return 0
    ti = data.get("tool_input", {}) or {}
    path = str(ti.get("file_path", "")).replace("\\", "/")
    # Only check Markdown files under docs/ (skip ADR files themselves
    # — their own filename always self-references).
    if not path.endswith(".md") or "/docs/" not in path:
        return 0
    if "/docs/adr/" in path:
        return 0

    content = _content_for(tool, ti)
    if not content:
        return 0

    proj = _project_dir()
    if proj is None:
        return 0
    existing = _existing_adrs(proj / "docs" / "adr")
    if not existing:
        return 0

    referenced: set[str] = {m.group(1) for m in ADR_REF.finditer(content)}
    # Filter out hits that don't look like ADR references — bare 4-digit
    # codes (e.g. "0042" in a phone number) are too noisy. We only flag
    # ones that appear in an ADR-ish context: prefixed with "ADR" or
    # followed by "-<word>" (ADR filename pattern) or "/adr/".
    flagged: list[str] = []
    for adr_id in sorted(referenced):
        if adr_id in existing:
            continue
        # Re-check: is this ID actually used in an ADR context?
        for m in ADR_REF.finditer(content):
            if m.group(1) != adr_id:
                continue
            # Look at ~30 chars of context around the match.
            start = max(0, m.start() - 30)
            end = min(len(content), m.end() + 30)
            window = content[start:end].lower()
            if (
                "adr" in window
                or "/adr/" in window
                or m.group(0).endswith(".md")
                or re.search(r"0\d{3}-[a-z]", m.group(0).lower())
            ):
                flagged.append(adr_id)
                break

    if not flagged:
        return 0

    msg_lines = [
        f"WARN ({path}): ADR reference(s) point to non-existent file(s):",
    ]
    for adr_id in sorted(set(flagged)):
        msg_lines.append(
            f"  - {adr_id} not found in docs/adr/ "
            f"(have: {', '.join(sorted(existing))})"
        )
    msg_lines.append(
        "Fix: update the reference to point at the correct ADR id, "
        "or create the missing ADR via the adr-new skill."
    )
    print("\n".join(msg_lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
