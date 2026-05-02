"""PostToolUse hook: warn when docs miss or have stale freshness header.

Convention started in PR #6/#7: every revised PRD / architecture /
runbook / SDD has a line like::

    > Last reviewed against code: YYYY-MM-DD.

near the top. This hook flags two failure modes when Claude writes
or edits a doc under ``docs/`` (excluding ``docs/adr/`` — ADRs are
historical and don't carry freshness):

1. **Missing header** — no ``Last reviewed against code:`` line in
   the new content.
2. **Stale header** — date present but older than ``MAX_AGE_DAYS``
   (default 30) compared to today.

Exits 2 with stderr so Claude considers updating the header in the
same edit pass. Exits 0 silently when fresh, missing-but-not-a-doc,
or content unavailable.

This hook does **not** rewrite the header for you — keeping the
human in the loop on what "reviewed against code" actually means.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime

MAX_AGE_DAYS = 30

# Match either the prose form (preferred):
#   "> Last reviewed against code: 2026-05-02."
# or a tolerant variant without the blockquote arrow.
HEADER_RE = re.compile(
    r"Last reviewed against code:\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _content_for(tool: str, ti: dict) -> str:
    if tool == "Edit":
        # For Edit, the new content we care about lives in new_string;
        # if the user is touching the freshness line itself we'll see
        # the date there. If they're not, we can't see it (Edit doesn't
        # show whole-file content), so we err on the side of silence.
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
    path = str(ti.get("file_path", "")).replace("\\", "/")

    # Only docs Markdown, skip ADRs (immutable historical records).
    if not path.endswith(".md") or "/docs/" not in path:
        return 0
    if "/docs/adr/" in path:
        return 0

    content = _content_for(tool, ti)
    if not content:
        return 0

    # For Edit calls we only inspect the slice we just wrote. If the
    # header isn't in that slice, we can't conclude it's missing —
    # only that *this edit* didn't touch it. So we only enforce
    # "missing" on Write (full-file replacement). For Edit we only
    # check freshness *if* the header appears in new_string.
    match = HEADER_RE.search(content)

    if tool == "Write":
        if match is None:
            print(
                f"WARN ({path}): missing freshness header. Add a line near "
                f"the top:\n  > Last reviewed against code: "
                f"{datetime.now(UTC).date().isoformat()}.\n"
                f"Convention from PR #6/#7 — see docs-stale-check skill.",
                file=sys.stderr,
            )
            return 2

    if match is None:
        return 0

    try:
        reviewed = date.fromisoformat(match.group(1))
    except ValueError:
        return 0

    today = datetime.now(UTC).date()
    if reviewed > today:
        # Don't flag future dates — could be a deliberate post-dated
        # review. Just no-op.
        return 0

    age_days = (today - reviewed).days
    if age_days > MAX_AGE_DAYS:
        print(
            f"WARN ({path}): freshness header is {age_days} days old "
            f"(reviewed {reviewed.isoformat()}, today {today.isoformat()}, "
            f"limit {MAX_AGE_DAYS}). If your edit reflects a fresh review "
            f"against code, bump the date to {today.isoformat()}.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
