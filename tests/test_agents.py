"""Agent unit tests (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agora.agents.discovery import DiscoveryAgent
from agora.agents.policy import CopyrightLedgerEntry, PolicyAgent, PolicyFlag
from agora.agents.routing import RoutingAgent
from agora.clients.openurl import OpenUrlParseError, _genre_to_kind, _safe_int, parse_openurl
from agora.clients.sru import MockSruClient, SruRecord
from agora.models.candidate import HolderCandidate
from agora.models.request import (
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)


def _request(**overrides: Any) -> IllRequest:
    base = dict(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="Test", isbn="9780000000001"),
    )
    base.update(overrides)
    return IllRequest(**base)


def test_openurl_parse_book() -> None:
    url = (
        "ctx_ver=Z39.88-2004&rft.genre=book&rft.btitle=Brave+New+World"
        "&rft.au=Huxley&rft.isbn=9780060850524&rft.date=2006"
    )
    item, citation = parse_openurl(url)
    assert item.title == "Brave New World"
    assert item.isbn == "9780060850524"
    assert item.year == 2006
    assert citation.parsed_from == "openurl"


def test_openurl_parse_article_uses_atitle_fallback() -> None:
    url = (
        "ctx_ver=Z39.88-2004&rft.genre=article&rft.atitle=A+Cool+Paper"
        "&rft.issn=12345678"
    )
    item, _ = parse_openurl(url)
    assert item.article_title == "A Cool Paper"
    assert item.issn == "12345678"
    assert item.item_kind == "article"


@pytest.mark.asyncio
async def test_discovery_returns_consortium_first() -> None:
    sru = MockSruClient(
        records=[
            SruRecord(
                title="Test",
                authors=["Anon"],
                isbn="9780000000001",
                issn=None,
                holdings=["MEMBER1", "OTHER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, consortium_members={"MEMBER1"})
    rec = await agent.run(_request())
    assert rec.has_candidates
    member_scores = [c.preferred_score for c in rec.candidates if c.is_consortium_member]
    nonmember_scores = [c.preferred_score for c in rec.candidates if not c.is_consortium_member]
    assert member_scores and nonmember_scores
    assert max(member_scores) >= max(nonmember_scores)


@pytest.mark.asyncio
async def test_routing_picks_consortium_available_first() -> None:
    candidates = [
        HolderCandidate(symbol="EXT", is_consortium_member=False, status="available"),
        HolderCandidate(symbol="MEM-A", is_consortium_member=True, status="on_loan"),
        HolderCandidate(symbol="MEM-B", is_consortium_member=True, status="available"),
    ]
    rec = await RoutingAgent().run(candidates)
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-B"


@pytest.mark.asyncio
async def test_routing_format_affinity_flips_article_to_digital() -> None:
    """ADR-0014 addendum: for article requests, electronic-delivery
    candidates should beat consortium-but-physical-only.

    Pre-feature: rules pick MEM-A on consortium weight (gap 0.46).
    Post-feature: format-affinity (+0.3 for electronic, -0.3 for
    physical_only when item_kind in {article, chapter}) flips the
    pick to EXT-DIG. Pinned to prevent regression of the 7e fix.
    """
    from agora.models.request import ItemMetadata

    candidates = [
        HolderCandidate(
            symbol="MEM-A",
            is_consortium_member=True,
            status="available",
            preferred_score=0.5,
            raw={"holds_format": "print", "delivery": "physical_only"},
        ),
        HolderCandidate(
            symbol="EXT-DIG",
            is_consortium_member=False,
            status="available",
            preferred_score=0.7,
            raw={"holds_format": "digital", "delivery": "electronic"},
        ),
    ]
    article = ItemMetadata(title="Some article", item_kind="article")
    rec = await RoutingAgent().run(candidates, item=article)
    assert rec.chosen is not None
    assert rec.chosen.symbol == "EXT-DIG"

    # Same candidates, book request → no affinity adjustment, rules
    # default wins (consortium beats external).
    book = ItemMetadata(title="Some book", item_kind="book")
    rec = await RoutingAgent().run(candidates, item=book)
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"

    # No item kwarg at all → backward-compatible behaviour, also rules
    # default (consortium wins).
    rec = await RoutingAgent().run(candidates)
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"


@pytest.mark.asyncio
async def test_routing_uses_distance_km_for_proximity_score() -> None:
    """Candidates with distance_km set exercise the proximity branch (line 265)."""
    candidates = [
        HolderCandidate(
            symbol="NEAR",
            is_consortium_member=True,
            status="available",
            distance_km=10.0,
        ),
        HolderCandidate(
            symbol="FAR",
            is_consortium_member=True,
            status="available",
            distance_km=5000.0,
        ),
    ]
    rec = await RoutingAgent().run(candidates)
    assert rec.chosen is not None
    # Both are consortium/available; NEAR has higher proximity score.
    assert rec.chosen.symbol == "NEAR"


@pytest.mark.asyncio
async def test_policy_blocks_contu_violation() -> None:
    issn = "12345678"
    ledger = [
        CopyrightLedgerEntry(
            issn=issn,
            article_year=2025,
            fulfilled_at=datetime.now(UTC),
        )
        for _ in range(5)
    ]
    agent = PolicyAgent(copyright_ledger=ledger)
    req = _request(
        request_type=RequestType.COPY,
        item=ItemMetadata(title="T", issn=issn, year=2025, item_kind="article"),
    )
    decision = await agent.run(req)
    assert not decision.passed
    assert any(f.code == "contu_violation" for f in decision.flags)


@pytest.mark.asyncio
async def test_policy_passes_when_quiet() -> None:
    decision = await PolicyAgent().run(_request())
    assert decision.passed
    assert decision.flags == []


# ---------------------------------------------------------------------------
# OpenURL error paths (lines 33, 50, 82-84, 92, 95-96)
# ---------------------------------------------------------------------------


def test_openurl_raises_on_empty_string() -> None:
    """parse_openurl('') raises OpenUrlParseError (line 33)."""
    with pytest.raises(OpenUrlParseError, match="empty OpenURL"):
        parse_openurl("")


def test_openurl_raises_on_whitespace_only() -> None:
    """Whitespace-only string is empty after strip (line 33)."""
    with pytest.raises(OpenUrlParseError, match="empty OpenURL"):
        parse_openurl("   ")


def test_openurl_raises_when_title_and_atitle_both_missing() -> None:
    """Missing both rft.btitle and rft.atitle raises (line 50)."""
    with pytest.raises(OpenUrlParseError, match="missing title"):
        parse_openurl("ctx_ver=Z39.88-2004&rft.genre=book&rft.au=Nobody")


def test_genre_to_kind_chapter() -> None:
    """'chapter' and 'proceeding' map to 'chapter' (lines 82-83)."""
    assert _genre_to_kind("chapter") == "chapter"
    assert _genre_to_kind("proceeding") == "chapter"
    assert _genre_to_kind("conference") == "chapter"


def test_genre_to_kind_other() -> None:
    """Unknown genre returns 'other' (line 84)."""
    assert _genre_to_kind("unknown") == "other"
    assert _genre_to_kind("") == "other"


def test_safe_int_non_four_digit_returns_none() -> None:
    """Fewer than 4 digit characters returns None (line 92)."""
    assert _safe_int("abc") is None
    assert _safe_int("12") is None


def test_safe_int_none_input_returns_none() -> None:
    """None input returns None immediately (line 89)."""
    assert _safe_int(None) is None


def test_safe_int_valid_year() -> None:
    """Four-digit string parses correctly."""
    assert _safe_int("2024") == 2024
    assert _safe_int("2024-01-01") == 2024  # takes first 4 chars


# ---------------------------------------------------------------------------
# PolicyDecision.hard_flags property (line 39)
# ---------------------------------------------------------------------------


def test_policy_decision_hard_flags_filters_correctly() -> None:
    """hard_flags returns only flags where is_hard=True (line 39)."""
    from agora.agents.policy import PolicyDecision

    soft = PolicyFlag(code="budget_exceeded", message="over", is_hard=False)
    hard = PolicyFlag(code="patron_suspended", message="suspended", is_hard=True)
    decision = PolicyDecision(request_id="r1", passed=False, flags=[soft, hard])
    assert decision.hard_flags == [hard]


# ---------------------------------------------------------------------------
# PolicyAgent — uncovered rule branches (lines 78, 101-103, 129, 132-133)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_blocks_suspended_patron() -> None:
    """Suspended patron triggers hard patron_suspended flag (line 78)."""
    agent = PolicyAgent(suspended_patrons={"A:p1"})
    decision = await agent.run(_request())
    assert not decision.passed
    assert any(f.code == "patron_suspended" and f.is_hard for f in decision.flags)


@pytest.mark.asyncio
async def test_policy_soft_flags_budget_exceeded() -> None:
    """Fee exceeding budget cap adds soft budget_exceeded flag (lines 101-103)."""
    agent = PolicyAgent(budget_remaining={"A": 5.00})
    decision = await agent.run(_request(), fee_estimate=10.00)
    # Soft flag — passes (no hard flags)
    assert decision.passed
    assert any(f.code == "budget_exceeded" and not f.is_hard for f in decision.flags)


@pytest.mark.asyncio
async def test_policy_contu_no_year_skips_check() -> None:
    """CONTU check skipped when year is absent (line 129 early return in _violates_contu).

    The outer guard in run() requires issn to call _violates_contu at all.
    With issn set but year=None the inner early return on ``not year`` fires.
    """
    agent = PolicyAgent(
        copyright_ledger=[
            CopyrightLedgerEntry(issn="12345678", article_year=2025, fulfilled_at=datetime.now(UTC))
            for _ in range(5)
        ]
    )
    # COPY request with issn but no year → _violates_contu returns False early
    req = _request(
        request_type=RequestType.COPY,
        item=ItemMetadata(title="T", issn="12345678", year=None, item_kind="article"),
    )
    decision = await agent.run(req)
    assert decision.passed
    assert not any(f.code == "contu_violation" for f in decision.flags)


@pytest.mark.asyncio
async def test_policy_contu_year_too_old_skips_check() -> None:
    """CONTU check skipped when article year predates recent window (line 132-133)."""
    agent = PolicyAgent(
        contu_recent_window_years=5,
        copyright_ledger=[
            CopyrightLedgerEntry(issn="12345678", article_year=2010, fulfilled_at=datetime.now(UTC))
            for _ in range(5)
        ],
    )
    current_year = datetime.now(UTC).year
    old_year = current_year - 10  # well outside 5-year window
    req = _request(
        request_type=RequestType.COPY,
        item=ItemMetadata(title="T", issn="12345678", year=old_year, item_kind="article"),
    )
    decision = await agent.run(req)
    assert decision.passed
    assert not any(f.code == "contu_violation" for f in decision.flags)
