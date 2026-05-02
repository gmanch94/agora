"""PreToolUse hook: block dangerous git commands.

Reads the tool-call JSON from stdin. Exits 2 with a stderr message to
block the call (Claude is told why and reconsiders). Exits 0 to allow.

Blocks (per CLAUDE.md behavioural rules):
- ``git commit --no-verify`` / ``--no-gpg-sign``
- ``git -c commit.gpgsign=false commit ...``
- ``git push --force ... main|master``
- ``git push -f ... main|master``
- ``git reset --hard``
- ``git branch -D``
- ``git clean -f`` / ``-fd``
- ``git checkout -- .``
- ``git restore .``

False-positive safety: only matches when the dangerous flag is on a
git invocation. Plain ``rm -rf`` or other destructive non-git
operations are out of scope (sandboxing handles those).
"""

from __future__ import annotations

import json
import re
import sys

PATTERNS: list[tuple[str, str]] = [
    (
        r"\bgit\s+commit\b[^|;&]*--no-verify\b",
        "git commit --no-verify is forbidden -- fix the failing hook instead "
        "(see CLAUDE.md). If the user explicitly asked, have them confirm.",
    ),
    (
        r"\bgit\s+commit\b[^|;&]*--no-gpg-sign\b",
        "git --no-gpg-sign is forbidden -- get explicit user permission first "
        "(see CLAUDE.md).",
    ),
    (
        r"\bgit\s+-c\s+commit\.gpgsign=false\b",
        "Bypassing GPG signing via -c commit.gpgsign=false is forbidden -- "
        "get explicit user permission first (see CLAUDE.md).",
    ),
    (
        r"\bgit\s+push\b[^|;&]*(--force|-f)\b[^|;&]*\b(main|master)\b",
        "Force-push to main/master is forbidden.",
    ),
    (
        r"\bgit\s+push\b[^|;&]*\b(main|master)\b[^|;&]*(--force|-f)\b",
        "Force-push to main/master is forbidden.",
    ),
    (
        r"\bgit\s+reset\s+--hard\b",
        "git reset --hard is destructive -- confirm with the user before running.",
    ),
    (
        r"\bgit\s+branch\s+-D\b",
        "git branch -D is destructive -- confirm with the user before running.",
    ),
    (
        r"\bgit\s+clean\s+-[fd]+\b",
        "git clean -f is destructive -- confirm with the user before running.",
    ),
    (
        r"\bgit\s+checkout\s+--\s+\.\b",
        "git checkout -- . discards uncommitted work -- confirm with the user.",
    ),
    (
        r"\bgit\s+restore\s+\.\b",
        "git restore . discards uncommitted work -- confirm with the user.",
    ),
]


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = data.get("tool_input", {}).get("command", "")
    if not cmd:
        return 0

    for pattern, message in PATTERNS:
        if re.search(pattern, cmd):
            print(f"BLOCKED: {message}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
