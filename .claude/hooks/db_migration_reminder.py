"""PostToolUse hook: remind to write an Alembic revision when ORM changes.

Triggers on Edit/Write to ``src/agora/saga/db.py``. The repo uses
Alembic as the source of truth for production schema; tests use
``Base.metadata.create_all()`` but production must run migrations.
Editing ORM without a migration is a silent footgun.

Heuristic: if the diff/content touches any of these substrings, prompt
to add a migration:

- ``mapped_column(``
- ``__tablename__``
- ``Column(``
- ``ForeignKey(``
- ``UniqueConstraint(``
- ``Index(``

Exits 2 with a stderr reminder. Non-blocking by intent -- Claude reads
the message and decides whether a migration is warranted.
"""

from __future__ import annotations

import json
import sys

ORM_MARKERS = (
    "mapped_column(",
    "__tablename__",
    "Column(",
    "ForeignKey(",
    "UniqueConstraint(",
    "Index(",
)

TARGET_PATH_FRAGMENT = "src/agora/saga/db.py"


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
    path = str(ti.get("file_path", "")).replace("\\", "/")
    if TARGET_PATH_FRAGMENT not in path:
        return 0

    content = _content_for(tool, ti)
    if not content:
        return 0
    if not any(marker in content for marker in ORM_MARKERS):
        return 0

    print(
        f"REMINDER ({path}): ORM schema appears to have changed. Production "
        "uses Alembic -- add a new revision under alembic/versions/ in the "
        "same commit. Tests pass via Base.metadata.create_all(); production "
        "does NOT. If this edit is non-schema (e.g. helper/comment only), "
        "ignore this reminder.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
