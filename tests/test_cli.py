"""Tests for the agora CLI entry point (src/agora/cli.py)."""

from __future__ import annotations

import pytest

import agora
from agora.cli import main


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == agora.__version__


def test_config_flag_prints_key_value_pairs(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=" in out


def test_no_args_prints_help_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    # argparse writes help to stdout
    assert "agora" in capsys.readouterr().out


# ---------------------------------------------------------------------
# Audit 2026-05-09 #10 — credential redaction
# ---------------------------------------------------------------------


def test_config_flag_redacts_credentials(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit #10: --config must NOT print plaintext passwords or db credentials.

    Pre-fix the loop dumped ``s.model_dump()`` raw, which printed
    ``reshare_password=<actual>`` when the env-var was set. Post-fix
    every credential field is ``SecretStr``-typed (renders as
    ``**********``) AND the CLI redacts any field whose key contains
    ``password`` / ``token`` / ``secret`` / ``key`` / ``credential`` as
    a defense in depth.
    """
    from agora.config import get_settings

    monkeypatch.setenv("RESHARE_PASSWORD", "supersecret123")
    monkeypatch.setenv("AGORA_CONSOLE_PASSWORD", "consolepw456")
    monkeypatch.setenv(
        "AGORA_DB_URL", "postgresql+asyncpg://user:realpw@db:5432/prod"
    )
    get_settings.cache_clear()
    try:
        rc = main(["--config"])
        out = capsys.readouterr().out
        assert rc == 0

        # Plaintext credentials must not appear anywhere.
        assert "supersecret123" not in out
        assert "consolepw456" not in out
        assert "realpw" not in out

        # Each redacted field renders the masked sentinel.
        assert "reshare_password=**********" in out
        assert "console_password=**********" in out
        assert "db_url=**********" in out
    finally:
        get_settings.cache_clear()


def test_config_flag_empty_credentials_render_blank(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty credential fields render as empty (not as ``**********``).

    Avoids misleading operators into thinking a credential is set
    when the env-var is actually unset.
    """
    from agora.config import get_settings

    monkeypatch.delenv("RESHARE_PASSWORD", raising=False)
    monkeypatch.delenv("AGORA_CONSOLE_PASSWORD", raising=False)
    get_settings.cache_clear()
    try:
        rc = main(["--config"])
        out = capsys.readouterr().out
        assert rc == 0
        # Empty SecretStr → empty string output, not the mask sentinel.
        assert "reshare_password=" in out
        assert "reshare_password=**********" not in out
        assert "console_password=" in out
        assert "console_password=**********" not in out
    finally:
        get_settings.cache_clear()
