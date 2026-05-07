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
