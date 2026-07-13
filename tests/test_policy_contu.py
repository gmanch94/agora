"""CONTU recency-window boundary tests for PolicyAgent.

CONTU's rule of 5 restricts copy requests for materials published
within FIVE years of the request date. At year granularity that is the
current year plus the four preceding years — 5 publication years
total. The pre-fix arithmetic (``current_year - window_years``)
silently included a 6th year.

These tests pin the boundary exactly:

- publication year == ``current_year - window + 1`` (the oldest year
  inside the 5-year window) → CONTU applies, hard flag fires.
- publication year == ``current_year - window`` (the 6th year back,
  counting the current year as the 1st) → outside the window, no flag.

The broader behavioural tests (ledger counting, missing year/issn
early-outs) live in ``tests/test_agents.py``; neither of those pins
the boundary year, which is why this file exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agora.agents.policy import CopyrightLedgerEntry, PolicyAgent
from agora.models.request import (
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)

_ISSN = "12345678"
_WINDOW = 5


def _copy_request(year: int) -> IllRequest:
    return IllRequest(
        request_type=RequestType.COPY,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="T", issn=_ISSN, year=year, item_kind="article"),
    )


def _agent_with_full_ledger(**overrides: Any) -> PolicyAgent:
    """Agent whose ledger already holds 5 in-window copies this year."""
    current_year = datetime.now(UTC).year
    kwargs: dict[str, Any] = dict(
        contu_recent_window_years=_WINDOW,
        copyright_ledger=[
            CopyrightLedgerEntry(
                issn=_ISSN,
                article_year=current_year,
                fulfilled_at=datetime.now(UTC),
            )
            for _ in range(5)
        ],
    )
    kwargs.update(overrides)
    return PolicyAgent(**kwargs)


@pytest.mark.asyncio
async def test_contu_oldest_in_window_year_is_restricted() -> None:
    """Publication year exactly at the window edge (5th year counting
    the current year as the 1st) is still CONTU-restricted."""
    current_year = datetime.now(UTC).year
    boundary_year = current_year - _WINDOW + 1  # oldest in-window year
    decision = await _agent_with_full_ledger().run(_copy_request(boundary_year))
    assert not decision.passed
    assert any(f.code == "contu_violation" and f.is_hard for f in decision.flags)


@pytest.mark.asyncio
async def test_contu_sixth_year_back_is_not_restricted() -> None:
    """Publication year one past the window edge (6th year counting the
    current year as the 1st) is outside CONTU — no flag even with a
    full ledger."""
    current_year = datetime.now(UTC).year
    outside_year = current_year - _WINDOW  # first out-of-window year
    decision = await _agent_with_full_ledger().run(_copy_request(outside_year))
    assert decision.passed
    assert not any(f.code == "contu_violation" for f in decision.flags)


@pytest.mark.asyncio
async def test_contu_current_year_is_restricted() -> None:
    """Sanity: the newest in-window year (current year) is restricted."""
    current_year = datetime.now(UTC).year
    decision = await _agent_with_full_ledger().run(_copy_request(current_year))
    assert not decision.passed
    assert any(f.code == "contu_violation" for f in decision.flags)


@pytest.mark.asyncio
async def test_contu_window_spans_exactly_five_publication_years() -> None:
    """The window covers exactly 5 distinct publication years."""
    current_year = datetime.now(UTC).year
    restricted = []
    for year in range(current_year - _WINDOW - 1, current_year + 1):
        decision = await _agent_with_full_ledger().run(_copy_request(year))
        if any(f.code == "contu_violation" for f in decision.flags):
            restricted.append(year)
    assert restricted == list(range(current_year - _WINDOW + 1, current_year + 1))
    assert len(restricted) == _WINDOW
