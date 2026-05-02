"""PostToolUse hook: flag bare ``datetime.now()`` in Python writes.

Agora invariant (CLAUDE.md): all datetimes are timezone-aware UTC.
Bare ``datetime.now()`` returns naive local time -- historically the
source of subtle CONTU/window-comparison bugs in this repo.

Allowed forms:
- ``datetime.now(UTC)``
- ``datetime.now(timezone.utc)``
- ``datetime.now(tz=...)`` with any keyword
- ``datetime.utcnow()`` is **also flagged** (deprecated; returns naive)

Exits 2 with stderr to send Claude back to fix the issue. The edit
has already happened, but Claude will see the message and revise.
"""

from __future__ import annotations

import json
import re
import sys

# Match `datetime.now(` followed by zero-or-more whitespace then `)`.
BARE_NOW = re.compile(r"\bdatetime\.now\(\s*\)")
# Also catch deprecated utcnow() (returns naive even though it's UTC).
UTCNOW = re.compile(r"\bdatetime\.utcnow\(\s*\)")


def _content_for(tool: str, ti: dict) -> str:
    if tool == "Edit":
        return str(ti.get("new_string", ""))
    if tool == "Write":
        return str(ti.get("content", ""))
    return ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool = data.get("tool_name")
    if tool not in ("Edit", "Write"):
        return 0
    ti = data.get("tool_input", {}) or {}
    path = str(ti.get("file_path", ""))
    if not path.endswith(".py"):
        return 0
    # Skip the hook scripts themselves and tests of datetime semantics.
    if ".claude/hooks/" in path.replace("\\", "/"):
        return 0

    content = _content_for(tool, ti)
    if not content:
        return 0

    bare = BARE_NOW.search(content)
    utcnow = UTCNOW.search(content)
    if not bare and not utcnow:
        return 0

    msgs = []
    if bare:
        msgs.append(
            f"WARN ({path}): bare datetime.now() -- Agora invariant requires "
            "datetime.now(UTC). Replace with datetime.now(UTC) and import UTC "
            "from datetime."
        )
    if utcnow:
        msgs.append(
            f"WARN ({path}): datetime.utcnow() is deprecated and returns "
            "naive time. Use datetime.now(UTC) instead."
        )
    print("\n".join(msgs), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
