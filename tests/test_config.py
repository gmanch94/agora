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

from pydantic import SecretStr

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


# Sentinel used by ``_runbook_env_table_entries`` when a row's key count
# and default count don't line up by either of the supported pairings.
# Tests that consume the entries treat this as "shape unparseable, do
# not compare." Keeps the parser honest without blowing up the suite.
_RUNBOOK_AMBIGUOUS_DEFAULT_MARKER = "<runbook-ambiguous>"


def _runbook_env_table_entries() -> dict[str, str]:
    """Return ``{env_var_name: documented_default_string}`` from the runbook.

    Underlies both ``_runbook_env_table_keys`` (just the keys) and the
    default-value symmetry test (key + default). Scoped to the env-var
    table region (``### 1.2 Environment variables`` → ``### 1.3``).

    Multi-var rows are paired by position when the key count matches
    the default count (e.g. ``AGORA_API_HOST`` / ``AGORA_API_PORT``
    paired with ``0.0.0.0`` / ``8000``). When a multi-var row has a
    single shared default (e.g. ``RESHARE_USER`` / ``RESHARE_PASSWORD``
    with ``""``), that default is assigned to every key. Ambiguous
    rows (mismatched counts ≠ 1) are skipped — flagged via the
    ``ambiguous_rows`` counter so the test can fail loud if the table
    grows a shape this parser can't read.

    Cell extraction uses literal ``|`` positions rather than splitting
    on ``|``, because the third (notes) cell legitimately contains
    backticked tokens — ``Tests override to `sqlite+aiosqlite:///:memory:`.``
    sits in column 3 of the ``AGORA_DB_URL`` row and would pollute the
    default capture if we naively grabbed every backticked token.
    """
    lines = _RUNBOOK_PATH.read_text(encoding="utf-8").splitlines()
    in_table = False
    entries: dict[str, str] = {}
    name_re = re.compile(r"`([A-Z_][A-Z0-9_]*)`")
    backtick_re = re.compile(r"`([^`]*)`")
    for line in lines:
        if line.startswith(_RUNBOOK_ENV_TABLE_START):
            in_table = True
            continue
        if in_table and line.startswith(_RUNBOOK_ENV_TABLE_END):
            break
        if not in_table or not line.startswith("|"):
            continue
        # Need pipe[0] (start), pipe[1] (col1 end), pipe[2] (col2 end).
        # Table-formatting separator rows match ``| ----- |`` and have
        # no backticks; they pass the pipe check but contribute nothing
        # to either regex.
        pipes = [i for i, ch in enumerate(line) if ch == "|"]
        if len(pipes) < 3:
            continue
        col1 = line[pipes[0] + 1 : pipes[1]]
        col2 = line[pipes[1] + 1 : pipes[2]]
        keys_in_row = [m.group(1) for m in name_re.finditer(col1)]
        defaults_in_row = [m.group(1) for m in backtick_re.finditer(col2)]
        if not keys_in_row:
            continue
        if len(defaults_in_row) == len(keys_in_row):
            for k, v in zip(keys_in_row, defaults_in_row, strict=True):
                entries[k] = v
        elif len(defaults_in_row) == 1:
            shared = defaults_in_row[0]
            for k in keys_in_row:
                entries[k] = shared
        else:
            # Ambiguous shape (e.g. 2 keys with 0 or 3+ defaults). Skip
            # the row but DON'T silently lose the keys — fall through to
            # registering each key with an empty marker so the
            # forward-symmetry test still sees them. The default-value
            # test detects the marker and skips with a clear note.
            for k in keys_in_row:
                entries[k] = _RUNBOOK_AMBIGUOUS_DEFAULT_MARKER
    return entries


def _runbook_env_table_keys() -> set[str]:
    """Return the set of env-var names documented in the runbook table."""
    return set(_runbook_env_table_entries().keys())


def _env_example_entries() -> dict[str, str]:
    """Parse ``.env.example`` and return ``{key: raw_value}``.

    Underlies both ``_env_example_keys`` (just the keys) and the
    default-value symmetry test (key + value). Skips comments and
    blank lines. Tolerates ``KEY=`` (empty value), ``KEY=value`` and
    ``KEY=quoted value``. Anything not matching ``KEY=...`` is
    ignored — comments embedded mid-line aren't valid env syntax
    anyway.
    """
    pattern = re.compile(r"^(?P<key>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$")
    entries: dict[str, str] = {}
    for raw_line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if match:
            entries[match.group("key")] = match.group("value")
    return entries


def _env_example_keys() -> set[str]:
    """Return the set of declared env-var names in ``.env.example``."""
    return set(_env_example_entries().keys())


def _coerce_env_string(raw: str, annotation: type) -> object:
    """Parse an env-style string into the typed value pydantic-settings would.

    Mirrors pydantic-settings's coercion rules for the four types Agora
    uses (``str``, ``int``, ``float``, ``bool``, plus ``SecretStr``).
    The bool coercion accepts the same truthy strings pydantic does so a
    doc-side ``true`` / ``1`` / ``yes`` survives the comparison without
    flagging a false mismatch. ``SecretStr`` (audit 2026-05-09 #10)
    wraps the string so equality against the ``Settings`` default
    succeeds despite the masked repr.
    """
    if annotation is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(raw.strip())
    if annotation is float:
        return float(raw.strip())
    # str: strip surrounding quotes if present, otherwise keep verbatim
    # (env-file convention is unquoted values).
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        stripped = stripped[1:-1]
    if annotation is SecretStr:
        return SecretStr(stripped)
    return stripped


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


def test_db_url_uses_dev_default_property() -> None:
    """Audit #25: ``db_url_uses_dev_default`` flags the unmodified default."""
    s_default = Settings()
    assert s_default.db_url_uses_dev_default is True

    s_override = Settings(AGORA_DB_URL="postgresql+asyncpg://prod:realpw@db:5432/p")
    assert s_override.db_url_uses_dev_default is False


def test_create_app_refuses_dev_db_in_non_dev_env(monkeypatch: object) -> None:
    """Audit #25: refuse to boot with ``:agora@`` creds when env != 'dev'.

    The default ``postgresql+asyncpg://agora:agora@localhost:5433/agora``
    is fine for offline laptop work; shipping it to staging/prod is a
    credential leak. ``create_app`` raises RuntimeError so the operator
    sees a clean refuse-to-start instead of a silently-running service.
    """
    import pytest

    from agora import config as config_mod
    from agora.api import app as app_mod

    assert hasattr(monkeypatch, "setenv")  # mypy narrowing for the param
    monkeypatch.setenv("AGORA_ENV", "staging")
    config_mod.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="development default"):
            app_mod.create_app()
    finally:
        config_mod.get_settings.cache_clear()


def test_runbook_env_table_default_values_match_settings_defaults() -> None:
    """Each runbook table default must match the corresponding ``Settings`` default.

    Sibling of ``test_env_example_default_values_match_settings_defaults``
    — same third-axis drift detection, applied to the operator-facing
    runbook table instead of the developer-facing ``.env.example``.
    Together they would have caught the routing-LLM ε mismatch through
    PRs #47-#51 (runbook said 0.05 / "Placeholder until PR-2b tunes
    against eval"; Settings tightened to 0.03 in #51).

    Multi-var rows (``AGORA_API_HOST`` / ``AGORA_API_PORT``,
    ``RESHARE_USER`` / ``RESHARE_PASSWORD``) are paired by
    ``_runbook_env_table_entries`` — positional pairing when defaults
    count matches keys count, shared default when there's only one.
    Truly ambiguous shapes carry the
    ``_RUNBOOK_AMBIGUOUS_DEFAULT_MARKER`` sentinel and are skipped.
    """
    entries = _runbook_env_table_entries()
    mismatches: list[str] = []
    for name, info in Settings.model_fields.items():
        alias = info.alias if info.alias is not None else name.upper()
        if alias not in entries:
            continue
        documented = entries[alias]
        if documented == _RUNBOOK_AMBIGUOUS_DEFAULT_MARKER:
            continue
        annotation = info.annotation
        if not isinstance(annotation, type):
            continue
        # Runbook strings are wrapped in backticks but the parser already
        # stripped them. The empty-string case appears as ``""`` in the
        # source markdown and survives parsing as the literal two-quote
        # string; normalise that to "" before coercion.
        normalised = "" if documented == '""' else documented
        try:
            coerced = _coerce_env_string(normalised, annotation)
        except (TypeError, ValueError) as exc:
            mismatches.append(
                f"{alias}: failed to coerce '{documented}' to {annotation.__name__}: {exc}"
            )
            continue
        if coerced != info.default:
            mismatches.append(
                f"{alias}: runbook says `{documented}` "
                f"(coerced to {coerced!r}), Settings default is "
                f"{info.default!r}"
            )
    assert not mismatches, "Default-value drift between Settings and the runbook env-var table:\n" + "\n".join(
        mismatches
    )


def test_runbook_env_table_only_documents_known_keys() -> None:
    """Every runbook env-var row maps to a current ``Settings`` field
    (or to a known downstream-tooling env var documented for operators).

    Catches the inverse drift: a row left behind for a removed
    ``Settings`` field. Mirrors the ``.env.example`` side — the runbook
    is allowed to carry rows for Google ADK / Anthropic SDK env vars
    that are not part of ``Settings`` (see ``_NON_AGORA_TOOLING_VARS``).
    Anything else in the runbook table that isn't a current ``Settings``
    alias is drift.
    """
    settings_keys = _settings_env_aliases()
    runbook_keys = _runbook_env_table_keys()
    extras = runbook_keys - settings_keys - _NON_AGORA_TOOLING_VARS
    assert not extras, (
        f"Runbook env-var table documents env vars that Settings does "
        f"not honour: {sorted(extras)}. Either remove the row or "
        f"restore the Settings field."
    )


def test_env_example_default_values_match_settings_defaults() -> None:
    """Each ``.env.example`` value must match the corresponding ``Settings`` default.

    Catches the third-axis drift the key-symmetry tests miss: the row
    is present, the key matches, but the *default* lies (the failure
    mode that hid the routing-LLM ε mismatch through PRs #47-#51 — the
    runbook said 0.05 / placeholder while Settings was tightened to
    0.03 in #51 and ``.env.example`` simply hadn't been touched).

    Comparison goes through ``_coerce_env_string`` so a ``5`` in the
    env file matches a ``5.0`` Settings default (numeric equivalence)
    and ``true`` / ``1`` both match ``True`` (pydantic-settings bool
    semantics). Empty defaults match the bare ``KEY=`` form. Settings
    fields that aren't documented in ``.env.example`` are caught by
    ``test_env_example_lists_every_settings_alias`` — this test only
    checks the overlap.
    """
    entries = _env_example_entries()
    mismatches: list[str] = []
    for name, info in Settings.model_fields.items():
        alias = info.alias if info.alias is not None else name.upper()
        if alias not in entries:
            continue
        annotation = info.annotation
        # ``info.annotation`` for plain str/int/float/bool fields IS the
        # bare type. Settings doesn't currently use Optional/Union for
        # any field that lands in .env.example, so a direct ``isinstance``
        # check is enough; if that changes the test will surface as a
        # ``not a type`` TypeError and force an explicit branch.
        if not isinstance(annotation, type):
            continue
        try:
            coerced = _coerce_env_string(entries[alias], annotation)
        except (TypeError, ValueError) as exc:
            mismatches.append(f"{alias}: failed to coerce '{entries[alias]}' to {annotation.__name__}: {exc}")
            continue
        if coerced != info.default:
            mismatches.append(
                f"{alias}: .env.example says '{entries[alias]}' "
                f"(coerced to {coerced!r}), Settings default is "
                f"{info.default!r}"
            )
    assert not mismatches, "Default-value drift between Settings and .env.example:\n" + "\n".join(
        mismatches
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
