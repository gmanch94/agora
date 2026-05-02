"""Holder candidate model produced by DiscoveryAgent and consumed by RoutingAgent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HolderCandidate(BaseModel):
    """A library that may be able to supply the requested item."""

    symbol: str = Field(description="ISIL or consortium-local symbol")
    name: str | None = None
    status: str = Field(
        default="unknown",
        description="available|on_loan|reference_only|unknown",
    )
    distance_km: float | None = None
    is_consortium_member: bool = False
    preferred_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="0=worst, 1=best"
    )
    raw: dict[str, Any] = Field(default_factory=dict)
