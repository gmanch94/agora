"""PostToolUse hook: nudge ``docs-stale-check`` after sizeable edits.

Counts Write/Edit operations against drift-prone code paths (the ones
listed in the ``docs-stale-check`` skill description). When the
counter hits ``THRESHOLD``, exits 2 with a stderr nudge so Claude
considers running the skill before docs accumulate more drift.

State lives in ``.claude/state/nudge_stale_check.json``; the counter
resets to 0 after a nudge fires. Counts only edits that *change* a
file (Write always counts; Edit counts when ``old_string != new_string``)
so passive reads don't tick the counter.

Watched roots (any of these prefixes match):
- ``src/agora/saga/``
- ``src/agora/api/``
- ``src/agora/agents/``
- ``src/agora/models/``
- ``src/agora/config.py``
- ``src/agora/clients/reshare.py``
- ``Makefile``
- ``alembic/versions/``

Exits 0 silently for unwatched paths or below threshold. Exits 2
(non-blocking — PostToolUse just relays stderr to Claude) at threshold.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

THRESHOLD = 5

WATCHED_PREFIXES = (
    "src/agora/saga/",
    "src/agora/api/",
    "src/agora/agents/",
    "src/agora/models/",
    "src/agora/clients/reshare.py",
    "src/agora/config.py",
    "Makefile",
    "alembic/versions/",
)


def _project_dir() -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / ".claude").is_dir() and (parent / "src").is_dir():
            return parent
    return None


def _is_watched(rel_path: str) -> bool:
    p = rel_path.lstrip("./")
    return any(p.startswith(prefix) for prefix in WATCHED_PREFIXES)


def _is_no_op_edit(tool: str, ti: dict) -> bool:
    """Skip Edit calls whose old_string == new_string (no actual change)."""
    if tool != "Edit":
        return False
    return str(ti.get("old_string", "")) == str(ti.get("new_string", ""))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool = data.get("tool_name")
    if tool not in ("Write", "Edit"):
        return 0
    ti = data.get("tool_input", {}) or {}
    if _is_no_op_edit(tool, ti):
        return 0

    raw_path = str(ti.get("file_path", ""))
    if not raw_path:
        return 0
    rel_path = raw_path.replace("\\", "/")

    proj = _project_dir()
    if proj is None:
        return 0

    proj_str = str(proj).replace("\\", "/").rstrip("/") + "/"
    if rel_path.startswith(proj_str):
        rel_path = rel_path[len(proj_str):]

    if not _is_watched(rel_path):
        return 0

    state_dir = proj / ".claude" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "nudge_stale_check.json"

    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    count = int(state.get("count", 0)) + 1

    if count >= THRESHOLD:
        # Reset and nudge.
        state_file.write_text(json.dumps({"count": 0}))
        print(
            f"NUDGE: {THRESHOLD} edits to drift-prone code paths since last "
            f"check. Consider invoking the docs-stale-check skill to surface "
            f"any drift in docs/ before it compounds. (Last edit: {rel_path}.)",
            file=sys.stderr,
        )
        return 2

    state_file.write_text(json.dumps({"count": count}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
