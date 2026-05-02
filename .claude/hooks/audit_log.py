"""PostToolUse hook: append every tool call to a local audit log.

Writes one JSON line per invocation to ``.claude/logs/audit.jsonl``.
Always exits 0 (never blocks). Useful when the ``/hooks`` UI is
unavailable and you want passive evidence that hooks are firing.

Log fields:
- ts: ISO8601 UTC timestamp
- tool: tool name (Bash, Edit, Write, ...)
- summary: short, one-line description of the call
  - Bash: first 120 chars of command
  - Edit/Write: file_path + content size
  - other: tool_input keys
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

LOG_DIR_ENV = "AGORA_HOOK_LOG_DIR"


def _summary(tool: str, ti: dict) -> str:
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    if tool in ("Edit", "Write"):
        path = ti.get("file_path", "")
        size = len(ti.get("new_string") or ti.get("content") or "")
        return f"{path} ({size}b)"
    return ",".join(sorted(k for k in ti.keys()))[:120]


def _resolve_log_dir() -> Path:
    """Anchor logs next to this script: ``<repo>/.claude/logs/``.

    Trusting ``CLAUDE_PROJECT_DIR`` is fragile on Windows because msys
    bash hands Python a ``/c/Users/...`` path that ``pathlib`` parses
    as relative. Script location is always correct since this file
    lives at ``.claude/hooks/audit_log.py``.
    """
    if (env := os.environ.get(LOG_DIR_ENV)):
        return Path(env)
    return Path(__file__).resolve().parent.parent / "logs"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool = data.get("tool_name", "?")
    ti = data.get("tool_input", {}) or {}

    record = {
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool,
        "summary": _summary(tool, ti),
    }

    log_dir = _resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Never fail the tool call because of a log write.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
