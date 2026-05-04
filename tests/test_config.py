"""Tests for ``agora.config.Settings``.

Pin invariants that get easy to break silently — chiefly the symmetry
between ``Settings`` fields and ``.env.example``. The runbook env-var
table § Configuration claims ".env.example in the repo lists the same
set"; PR #57 caught that this had been silently broken for ~13 PRs.
PR #58 captured the lesson "symmetry claims between artifacts need a
CI check or they're aspirational." This file is the CI check.
"""

from __future__ import annotations

import re
from pathlib import Path

from agora.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE_PATH = _REPO_ROOT / ".env.example"
_RUNBOOK_PATH = _REPO_ROOT / "docs" / "runbook.md"

# Runbook env-var table delimiters. The table sits inside section 1.2
# "Environment variables"; section 1.3 "Schema" follows. We scope the
# scan to that region so backticked tokens elsewhere in the document
# (e.g. ``AGORA_TEST_DB_URL`` in the postgres-tests section, which is
# NOT a ``Settings`` field) don't pollute the comparison.
_RUNBOOK_ENV_TABLE_START = "### 1.2 Environment variables"
_RUNBOOK_ENV_TABLE_END = "### 1.3"

# Env vars referenced by ``.env.example`` for downstream tooling that is
# not part of Agora's own ``Settings`` (read directly by ``google-adk``
# / Vertex / Anthropic SDKs). They appear in the file as documentation
# of the broader runtime environment, but they're not Agora config —
# so the symmetry check ignores them on the .env.example side.
_NON_AGORA_TOOLING_VARS: frozenset[str] = frozenset(
    {
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "ANTHROPIC_API_KEY",
    }
)


def _settings_env_aliases() -> set[str]:
    """Return the set of env-var names every ``Settings`` field binds to.

    pydantic-settings exposes the alias on each ``FieldInfo``; for a
    field declared without an explicit alias the ``alias`` attribute is
    ``None``, in which case pydantic-settings would honour the field
    name itself (case-insensitive). All Agora fields ship with explicit
    aliases today, but the fallback keeps this honest.
    """
    aliases: set[str] = set()
    for name, info in Settings.model_fields.items():
        aliases.add(info.alias if info.alias is not None else name.upper())
    return aliases


def _runbook_env_table_keys() -> set[str]:
    """Return the set of env-var names documented in the runbook table.

    Reads ``docs/runbook.md``, scopes to the env-var table region
    (``### 1.2 Environment variables`` → ``### 1.3``), and pulls every
    backticked all-caps token from rows that start with ``|``. Some
    rows pack two vars in a single cell with ``/`` (e.g.
    ``AGORA_API_HOST`` / ``AGORA_API_PORT``); the inner regex catches
    both.

    Scoping the scan to the table region matters: ``AGORA_TEST_DB_URL``
    appears in the postgres-tests section but is not a ``Settings``
    field, and a whole-file scan would generate false positives.
    """
    lines = _RUNBOOK_PATH.read_text(encoding="utf-8").splitlines()
    in_table = False
    keys: set[str] = set()
    token_re = re.compile(r"`([A-Z_][A-Z0-9_]*)`")
    for line in lines:
        if line.startswith(_RUNBOOK_ENV_TABLE_START):
            in_table = True
            continue
        if in_table and line.startswith(_RUNBOOK_ENV_TABLE_END):
            break
        if not in_table or not line.startswith("|"):
            continue
        # First cell carries the env var name(s); table-formatting rows
        # like ``| ----- |`` have no backticks so the regex misses them
        # naturally.
        first_cell_end = line.find("|", 1)
        if first_cell_end == -1:
            continue
        first_cell = line[1:first_cell_end]
        for match in token_re.finditer(first_cell):
            keys.add(match.group(1))
    return keys


def _env_example_keys() -> set[str]:
    """Parse ``.env.example`` and return the set of declared env-var names.

    Skips comments (lines starting ``#``), blank lines, and section
    dividers. Tolerates ``KEY=`` (empty default), ``KEY=value`` and
    ``KEY=quoted value``. Anything that doesn't match the
    ``KEY=...`` shape is ignored — comments embedded mid-line aren't
    valid env syntax anyway.
    """
    pattern = re.compile(r"^(?P<key>[A-Z_][A-Z0-9_]*)=")
    keys: set[str] = set()
    for raw_line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if match:
            keys.add(match.group("key"))
    return keys


def test_env_example_lists_every_settings_alias() -> None:
    """``.env.example`` must document every ``Settings`` env-var alias.

    Failure mode this catches: a feature PR adds a new ``Settings``
    field (with a new ``alias=...``) but forgets to extend
    ``.env.example``. The runbook env-var table § Configuration claims
    symmetry; without this test the claim drifted unnoticed for ~13
    PRs (see ``docs/lessons.md`` § Workflow / process under
    "``.env.example`` drifts silently").

    Asymmetric on purpose: ``.env.example`` is allowed to carry
    documentation rows for downstream-tool env vars that are not part
    of ``Settings`` (Google ADK / Anthropic SDK config; see
    ``_NON_AGORA_TOOLING_VARS``). The test only fails when ``Settings``
    has a key the example file forgot.
    """
    settings_keys = _settings_env_aliases()
    example_keys = _env_example_keys()
    missing = settings_keys - example_keys
    assert not missing, (
        f"Settings declares env aliases that .env.example is missing: "
        f"{sorted(missing)}. Add a row for each (with a comment "
        f"explaining the toggle) and update the runbook env-var table."
    )


def test_runbook_env_table_lists_every_settings_alias() -> None:
    """``docs/runbook.md`` § 1.2 must document every ``Settings`` alias.

    Sibling of ``test_env_example_lists_every_settings_alias``: the
    runbook env-var table is the canonical operator-facing
    documentation; ``.env.example`` is the canonical developer-facing
    template. Both drift independently if untested. Operationalises the
    PR #58 lesson "symmetry claims between artifacts need a CI check or
    they're aspirational."
    """
    settings_keys = _settings_env_aliases()
    runbook_keys = _runbook_env_table_keys()
    missing = settings_keys - runbook_keys
    assert not missing, (
        f"Settings declares env aliases that the runbook table is "
        f"missing: {sorted(missing)}. Add a row to "
        f"``docs/runbook.md`` § 1.2 with the default + a one-line "
        f"description of the toggle's effect."
    )


def test_runbook_env_table_only_documents_known_keys() -> None:
    """Every runbook env-var row maps to a current ``Settings`` field.

    Catches the inverse drift: a row left behind for a removed
    ``Settings`` field. The runbook table only documents Agora's own
    config (no Google ADK / Anthropic SDK rows like ``.env.example``
    has), so the comparison is strict — no allowlist needed.
    """
    settings_keys = _settings_env_aliases()
    runbook_keys = _runbook_env_table_keys()
    extras = runbook_keys - settings_keys
    assert not extras, (
        f"Runbook env-var table documents env vars that Settings does "
        f"not honour: {sorted(extras)}. Either remove the row or "
        f"restore the Settings field."
    )


def test_env_example_only_documents_known_keys() -> None:
    """Every ``.env.example`` row maps to a ``Settings`` field or a
    known-tooling exception.

    Catches the inverse drift: a stale row for an env var that
    ``Settings`` has stopped honouring. Without this we'd accumulate
    dead docs for removed config knobs. New downstream-tooling vars
    (e.g. a future Anthropic-SDK env) extend ``_NON_AGORA_TOOLING_VARS``
    explicitly so additions are reviewed, not waved through.
    """
    settings_keys = _settings_env_aliases()
    example_keys = _env_example_keys()
    extras = example_keys - settings_keys - _NON_AGORA_TOOLING_VARS
    assert not extras, (
        f".env.example documents env vars that Settings does not honour "
        f"and that aren't in _NON_AGORA_TOOLING_VARS: {sorted(extras)}. "
        f"Either remove the row, restore the Settings field, or add the "
        f"key to _NON_AGORA_TOOLING_VARS with rationale."
    )
