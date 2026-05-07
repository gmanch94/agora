"""Smoke test for the happy-path demo (src/agora/demos/happy_path.py).

The demo is end-to-end: in-memory SQLite + MockReShareClient + the full
saga registry. Running it as a test exercises the entire lifecycle —
the test passing means the demo runs to completion (saga reaches a
terminal state) without exception.
"""

from __future__ import annotations

import pytest

from agora.demos.happy_path import main


@pytest.mark.asyncio
async def test_demo_main_runs_to_completion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The demo's main() must complete without raising. Output is captured
    and not asserted on — the assertion is implicit via no-exception."""
    await main()
    out = capsys.readouterr().out
    # Sanity: the final ledger header is printed.
    assert "SAGA" in out
