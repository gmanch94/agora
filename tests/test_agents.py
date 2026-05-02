"""Agent unit tests (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agora.agents.discovery import DiscoveryAgent
from agora.agents.policy import CopyrightLedgerEntry, PolicyAgent
from agora.agents.routing import RoutingAgent
from agora.clients.openurl import parse_openurl
from agora.clients.sru import MockSruClient, SruRecord
from agora.models.candidate import HolderCandidate
from agora.models.request import (
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)


def _request(**overrides) -> IllRequest:
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
