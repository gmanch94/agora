# Agora project hooks

Project-scoped Claude Code hooks for guardrails. Wired in
`.claude/settings.json`.

| Hook | Event | Matcher | Behaviour |
|---|---|---|---|
| `block_dangerous_git.py` | PreToolUse | Bash | Blocks `--no-verify`, `--no-gpg-sign`, GPG-bypass via `-c`, force-push to main/master, `reset --hard`, `branch -D`, `clean -f`, `checkout -- .`, `restore .` |
| `scan_secrets.py` | PreToolUse | Write\|Edit | Blocks files containing AWS / GitHub / Slack / OpenAI / Anthropic / Google / Stripe key shapes or PEM private keys. Allows when the match contains placeholder tokens (`EXAMPLE`, `YOUR-`, `REDACTED`) or path is `.env.example` / `docs/` / `tests/fixtures/` / `README.md` |
| `check_datetime_utc.py` | PostToolUse | Write\|Edit | Flags bare `datetime.now()` and `datetime.utcnow()` in `*.py`. Project invariant: timezone-aware UTC only |
| `db_migration_reminder.py` | PostToolUse | Write\|Edit | When `src/agora/saga/db.py` gets ORM-shaped edits, reminds Claude to add an Alembic revision |

## Hook protocol

Each script reads a single JSON object from stdin:
```json
{"tool_name": "Bash", "tool_input": {"command": "..."}}
```

Exit codes:
- `0` — allow / no comment
- `2` — block (PreToolUse) / send Claude back to revise (PostToolUse)
  with stderr message
- other non-zero — error reported to user

Scripts use stdlib only — no project imports — so they run with plain
`python` on PATH (no venv needed). All stderr messages use ASCII
`--` instead of em-dashes for Windows console compatibility.

## Smoke tests

```bash
# Dangerous git blocked
echo '{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}' \
  | python .claude/hooks/block_dangerous_git.py
# exit=2, stderr=BLOCKED: ...

# Bare datetime flagged
echo '{"tool_name":"Edit","tool_input":{"file_path":"foo.py","new_string":"datetime.now()"}}' \
  | python .claude/hooks/check_datetime_utc.py
# exit=2, stderr=WARN: ...

# Real-shaped AWS key blocked
echo '{"tool_name":"Write","tool_input":{"file_path":"foo.py","content":"k=AKIA1234567890ABCDEF"}}' \
  | python .claude/hooks/scan_secrets.py
# exit=2, stderr=BLOCKED: ...

# Placeholder allowed
echo '{"tool_name":"Write","tool_input":{"file_path":"foo.py","content":"k=AKIAIOSFODNN7EXAMPLE"}}' \
  | python .claude/hooks/scan_secrets.py
# exit=0
```

## Adding a new hook

1. Write `.claude/hooks/<name>.py` (stdlib only, exit 0/2)
2. Wire in `.claude/settings.json` under the appropriate event +
   matcher
3. Smoke-test by piping a sample JSON to the script

## Disabling temporarily

Comment out the relevant block in `.claude/settings.json`. Don't
delete the script — keep it for reference.
