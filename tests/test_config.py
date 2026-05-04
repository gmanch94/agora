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
