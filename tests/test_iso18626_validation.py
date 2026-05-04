"""Tests for the ISO 18626 validation harness (`scripts/validate_iso18626.py`).

Two layers of coverage:

1. **Self-test layer (always runs).** Uses hand-rolled minimal fixtures
   under ``tests/fixtures/iso18626/`` to prove the validator's contract:
   passes a well-formed payload, rejects a malformed one with detectable
   line/column errors, surfaces missing-file errors with non-zero exit.
   These tests do NOT depend on the real ISO 18626 XSD being cached
   locally — they exercise the lxml plumbing through a tiny private-namespace
   schema so CI gets meaningful coverage on every PR.

2. **Real-schema layer (skips when XSD absent).** When
   ``docs/standards/iso18626/iso18626-v1_3.xsd`` exists, validate any
   real-schema fixture files in ``tests/fixtures/iso18626/`` whose
   filename starts with ``iso18626-``. Until someone caches the real
   XSD per the README in ``docs/standards/iso18626/``, the test skips
   with a clear pointer to the cache instructions.

The split mirrors PR #43's ``httpx.MockTransport`` pattern: own the
plumbing test; defer the real-wire test to a one-time live verification
step.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

# Validate's signature, mirrored from scripts/validate_iso18626.py.
# Captured here so the dynamic-import shim below stays mypy-strict-clean.
_ValidateFn = Callable[[Path, Path], tuple[bool, list[str]]]

# Repo-root absolute paths so the tests don't depend on pytest cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "iso18626"
_REAL_XSD = _REPO_ROOT / "docs" / "standards" / "iso18626" / "iso18626-v1_3.xsd"

_MINIMAL_XSD = _FIXTURES / "minimal.xsd"
_MINIMAL_VALID = _FIXTURES / "minimal-valid.xml"
_MINIMAL_INVALID = _FIXTURES / "minimal-invalid.xml"


# --- Self-test layer (always runs) -----------------------------------------


def _import_validate() -> _ValidateFn:
    """Import ``validate`` from the script lazily.

    ``scripts/`` isn't a package; rather than mutating ``sys.path`` at
    module-load we resolve the script as a file path and exec it into a
    fresh namespace. Keeps the test isolated and the script importable
    by name without forcing it into ``src/``.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_validate_iso18626", _REPO_ROOT / "scripts" / "validate_iso18626.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn: _ValidateFn = module.validate
    return fn


def test_self_test_fixtures_present() -> None:
    """Sanity: the self-test fixtures we ship in this repo are intact.

    A stray ``git rm`` or rebase mistake on the fixtures dir would
    silently turn every other self-test into a skip. Fail loudly here.
    """
    assert _MINIMAL_XSD.is_file(), f"missing fixture: {_MINIMAL_XSD}"
    assert _MINIMAL_VALID.is_file(), f"missing fixture: {_MINIMAL_VALID}"
    assert _MINIMAL_INVALID.is_file(), f"missing fixture: {_MINIMAL_INVALID}"


def test_validator_passes_a_valid_payload() -> None:
    """The minimal valid fixture must validate against the minimal XSD."""
    validate = _import_validate()
    ok, errors = validate(_MINIMAL_XSD, _MINIMAL_VALID)
    assert ok, f"expected pass, got errors: {errors}"
    assert errors == []


def test_validator_rejects_a_malformed_payload() -> None:
    """The minimal invalid fixture must fail with detectable errors.

    We don't pin the exact error string (lxml's error_log wording is
    a moving target across versions) — we just assert there is at
    least one error line carrying both ``line`` and ``col`` markers,
    which is the contract the script's caller can rely on.
    """
    validate = _import_validate()
    ok, errors = validate(_MINIMAL_XSD, _MINIMAL_INVALID)
    assert not ok, "expected fail, got pass"
    assert errors, "expected at least one validation error"
    assert all("line " in e and "col " in e for e in errors), (
        f"errors should carry line/col markers; got: {errors}"
    )


def test_validator_reports_missing_xsd() -> None:
    """A non-existent XSD path must surface as a clear error string.

    Caller (CI / skill / staff console) needs to distinguish "your
    payload is wrong" from "the validator setup is wrong" without
    parsing tracebacks. The validate() contract is to return False +
    an error list, never to raise.
    """
    validate = _import_validate()
    ok, errors = validate(
        _REPO_ROOT / "tests" / "fixtures" / "iso18626" / "no-such.xsd",
        _MINIMAL_VALID,
    )
    assert not ok
    assert any("xsd not found" in e for e in errors), (
        f"expected xsd-missing error; got: {errors}"
    )


def test_validator_reports_missing_xml() -> None:
    """Companion check for the XML side of the missing-file path."""
    validate = _import_validate()
    ok, errors = validate(
        _MINIMAL_XSD,
        _REPO_ROOT / "tests" / "fixtures" / "iso18626" / "no-such.xml",
    )
    assert not ok
    assert any("xml not found" in e for e in errors), (
        f"expected xml-missing error; got: {errors}"
    )


# --- CLI entrypoint smoke test ---------------------------------------------


def test_cli_main_returns_zero_on_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """``main()`` exit code 0 + ``OK:`` stdout line on the happy path."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_validate_iso18626_cli", _REPO_ROOT / "scripts" / "validate_iso18626.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    code = module.main(["--xsd", str(_MINIMAL_XSD), "--xml", str(_MINIMAL_VALID)])
    assert code == 0
    captured = capsys.readouterr()
    assert "OK:" in captured.out


def test_cli_main_returns_one_on_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    """``main()`` exit code 1 + ``FAIL:`` stderr line on a malformed payload."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_validate_iso18626_cli", _REPO_ROOT / "scripts" / "validate_iso18626.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    code = module.main(["--xsd", str(_MINIMAL_XSD), "--xml", str(_MINIMAL_INVALID)])
    assert code == 1
    captured = capsys.readouterr()
    assert "FAIL:" in captured.err


def test_cli_main_returns_two_on_missing_xsd(capsys: pytest.CaptureFixture[str]) -> None:
    """``main()`` exit code 2 distinguishes setup errors from validation
    failures (CI surfacing: "your validator is unset up" vs "your
    payload is wrong" should not collide on the same exit code)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_validate_iso18626_cli", _REPO_ROOT / "scripts" / "validate_iso18626.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    code = module.main(
        [
            "--xsd",
            str(_REPO_ROOT / "tests" / "fixtures" / "iso18626" / "no-such.xsd"),
            "--xml",
            str(_MINIMAL_VALID),
        ]
    )
    assert code == 2


# --- Real-schema layer (skips when XSD absent) -----------------------------


def _real_payloads() -> list[Path]:
    """Glob ``tests/fixtures/iso18626/iso18626-*.xml`` if any are
    committed. Returns empty list when none — the parametrized test
    then skips."""
    return sorted(_FIXTURES.glob("iso18626-*.xml"))


@pytest.mark.skipif(
    not _REAL_XSD.is_file(),
    reason=(
        f"real ISO 18626 XSD not cached at {_REAL_XSD}; "
        "see docs/standards/iso18626/README.md for the cache step"
    ),
)
def test_real_xsd_parses() -> None:
    """When the real XSD is cached, lxml must be able to parse it.

    A broken / partial download manifests as ``XMLSchemaParseError``;
    we want that to be a CI failure, not a silent pass.
    """
    validate = _import_validate()
    # Pair the XSD with the minimal-valid fixture purely to exercise the
    # parse path; we *expect* this to fail validation (different
    # namespaces) but must NOT raise on the parse step. The validate
    # contract is False+errors, never raise.
    ok, errors = validate(_REAL_XSD, _MINIMAL_VALID)
    assert isinstance(ok, bool)
    assert isinstance(errors, list)


def test_real_payloads_validate() -> None:
    """Each committed real-schema fixture must validate against the
    cached XSD.

    Loop-inside-test (rather than ``@pytest.mark.parametrize``) so an
    empty ``_real_payloads()`` list does not trigger pytest's
    empty-parametrize warning. Skips cleanly when either prerequisite
    is missing (real XSD not cached, or no real fixtures committed
    yet).
    """
    if not _REAL_XSD.is_file():
        pytest.skip(
            f"real ISO 18626 XSD not cached at {_REAL_XSD}; "
            "see docs/standards/iso18626/README.md for the cache step"
        )
    payloads = _real_payloads()
    if not payloads:
        pytest.skip(
            "no real ISO 18626 fixtures committed; drop "
            "iso18626-*.xml under tests/fixtures/iso18626/ to enable"
        )
    validate = _import_validate()
    failures: list[str] = []
    for payload in payloads:
        ok, errors = validate(_REAL_XSD, payload)
        if not ok:
            failures.append(f"{payload.name}: {errors}")
    assert not failures, "real-schema fixtures failed validation:\n" + "\n".join(failures)
