# Agora project skills

Project-scoped Claude Code skills for the Agora ILL prototype. Each
directory is one skill; `SKILL.md` describes when to invoke and what
to do.

| Skill | Purpose |
|---|---|
| `saga-trace` | Pretty-print a saga's event timeline + flag invariant violations |
| `lifecycle-extend` | Add a new state/step to the lifecycle without breaking saga invariants (touches 6+ files in lockstep) |
| `iso18626-validate` | Validate ISO 18626 XML against the published XSD; catches the common 2021-revision pitfalls |
| `reshare-probe` | Probe a running ReShare instance and diff actual endpoints/payloads vs `HttpReShareClient` |
| `policy-rule-add` | Add a rule to `PolicyAgent` with consistent code, tests, rationale format |
| `adr-new` | Bootstrap a new ADR with the project's standard template + next sequential number |

## Adding a new skill

```bash
mkdir .claude/skills/<name>
$EDITOR .claude/skills/<name>/SKILL.md
```

The `SKILL.md` frontmatter must include `name` and `description`.
Description should answer "when should this skill be invoked" — that's
how Claude decides to load it.
