"""PreToolUse hook: block writes that contain known-shape secrets.

Scans Write/Edit content for high-confidence secret prefixes. The
goal is to catch obvious leaks (an AWS key pasted into a config, a
GitHub token in a script) before the file lands. Low-recall by
design — only well-known formats with low false-positive rates.

Patterns:
- AWS access key:        ``AKIA[0-9A-Z]{16}``
- AWS session token:     ``ASIA[0-9A-Z]{16}``
- GitHub PAT:            ``gh[pousr]_[A-Za-z0-9]{36,}``
- Slack token:           ``xox[baprs]-[A-Za-z0-9-]{10,}``
- OpenAI API key:        ``sk-[A-Za-z0-9]{32,}``  (also matches Anthropic ``sk-ant-...``)
- Google API key:        ``AIza[0-9A-Za-z_-]{35}``
- Stripe live key:       ``sk_live_[0-9A-Za-z]{24,}``
- Private key block:     ``-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----``

If the file is ``.env.example`` or contains placeholder shapes (all
``X``s, all ``0``s, "example", "your-...-here"), allow it.
"""

from __future__ import annotations

import json
import re
import sys

SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\bASIA[0-9A-Z]{16}\b", "AWS session token"),
    (r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", "GitHub token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\bsk-[A-Za-z0-9_-]{32,}\b", "OpenAI/Anthropic-style key"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "Google API key"),
    (r"\bsk_live_[0-9A-Za-z]{24,}\b", "Stripe live key"),
    (
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        "Private key block",
    ),
]

# If the file path looks like an example/template, downgrade to allow.
PLACEHOLDER_PATHS = (
    ".env.example",
    "README.md",
    "/docs/",
    "/tests/fixtures/",
)

# Common placeholder shapes inside an apparent secret. If the match
# string contains any of these, it's likely a doc/example.
PLACEHOLDER_TOKENS = (
    "EXAMPLE",
    "XXXXXX",
    "000000",
    "your-",
    "YOUR-",
    "REPLACE",
    "redacted",
    "REDACTED",
)


def _content_for(tool: str, ti: dict) -> str:
    if tool == "Edit":
        return str(ti.get("new_string", ""))
    if tool == "Write":
        return str(ti.get("content", ""))
    return ""


def _is_placeholder(match_text: str) -> bool:
    return any(tok in match_text for tok in PLACEHOLDER_TOKENS)


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
    content = _content_for(tool, ti)
    if not content:
        return 0

    is_example_path = any(frag in path for frag in PLACEHOLDER_PATHS)

    findings: list[str] = []
    for pattern, label in SECRET_PATTERNS:
        for m in re.finditer(pattern, content):
            text = m.group(0)
            if _is_placeholder(text):
                continue
            if is_example_path:
                # Allow but log to stderr so Claude sees it.
                print(
                    f"NOTE ({path}): {label} shape in placeholder/example "
                    f"path — allowed.",
                    file=sys.stderr,
                )
                continue
            preview = text[:8] + "..." if len(text) > 8 else text
            findings.append(f"  - {label}: {preview} at offset {m.start()}")

    if findings:
        print(
            f"BLOCKED ({path}): possible secret(s) detected. If these are "
            f"intentional placeholders, add 'EXAMPLE' / 'YOUR-' / 'REDACTED' "
            f"markers, or write to .env.example / docs/. Findings:",
            file=sys.stderr,
        )
        print("\n".join(findings), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
