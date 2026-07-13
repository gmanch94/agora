"""PolicyAgent — pre-flight legal & budget checks.

Implements three rule families in the prototype:

1. **CONTU rule of 5** — for copy requests of journal articles, no
   more than 5 articles from the same journal title within the
   most-recent 5 years of publication during the calendar year.
2. **Patron eligibility** — patron must not be suspended or expired.
3. **Budget cap** — per-library budget cap for paid lending fees.

Hard flags block forward steps even if staff click approve, unless
they explicitly override with a reason that's persisted in the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from agora.models.request import IllRequest, RequestType


@dataclass(slots=True)
class PolicyFlag:
    code: str  # 'contu_violation' | 'patron_suspended' | 'budget_exceeded' | ...
    message: str
    is_hard: bool = False


@dataclass(slots=True)
class PolicyDecision:
    request_id: str
    passed: bool
    flags: list[PolicyFlag] = field(default_factory=list)
    rationale: str = ""

    @property
    def hard_flags(self) -> list[PolicyFlag]:
        return [f for f in self.flags if f.is_hard]


@dataclass(slots=True)
class CopyrightLedgerEntry:
    """A previously fulfilled copy request, used for CONTU counting."""

    issn: str
    article_year: int
    fulfilled_at: datetime


class PolicyAgent:
    """Run rule checks against in-memory ledgers + patron registry.

    Real deployments would back these with Postgres tables; the
    prototype uses simple in-memory structures so policy rules can be
    tested in isolation.
    """

    def __init__(
        self,
        *,
        copyright_ledger: list[CopyrightLedgerEntry] | None = None,
        suspended_patrons: set[str] | None = None,
        budget_remaining: dict[str, float] | None = None,
        contu_recent_window_years: int = 5,
        contu_max_per_journal_per_year: int = 5,
    ):
        self._copyright_ledger = copyright_ledger or []
        self._suspended = suspended_patrons or set()
        self._budget = budget_remaining or {}
        self._window_years = contu_recent_window_years
        self._max_per_journal = contu_max_per_journal_per_year

    async def run(self, request: IllRequest, *, fee_estimate: float = 0.0) -> PolicyDecision:
        flags: list[PolicyFlag] = []

        if self._is_suspended(request):
            flags.append(
                PolicyFlag(
                    code="patron_suspended",
                    message="Patron account is suspended.",
                    is_hard=True,
                )
            )

        if request.request_type == RequestType.COPY and request.item.issn:
            if self._violates_contu(request):
                flags.append(
                    PolicyFlag(
                        code="contu_violation",
                        message=(
                            "CONTU rule of 5 violation: this would be the 6th "
                            f"copy from ISSN {request.item.issn} within the "
                            f"last {self._window_years} years."
                        ),
                        is_hard=True,
                    )
                )

        if fee_estimate > 0:
            remaining = self._budget.get(request.requesting_library.symbol)
            if remaining is not None and fee_estimate > remaining:
                flags.append(
                    PolicyFlag(
                        code="budget_exceeded",
                        message=(
                            f"Estimated fee ${fee_estimate:.2f} exceeds remaining "
                            f"library budget ${remaining:.2f}."
                        ),
                        is_hard=False,
                    )
                )

        passed = not any(f.is_hard for f in flags)
        rationale = self._make_rationale(passed, flags)
        return PolicyDecision(
            request_id=str(request.request_id),
            passed=passed,
            flags=flags,
            rationale=rationale,
        )

    def _is_suspended(self, request: IllRequest) -> bool:
        composite = f"{request.patron.library_symbol}:{request.patron.patron_id}"
        return composite in self._suspended

    def _violates_contu(self, request: IllRequest) -> bool:
        if not request.item.issn or not request.item.year:
            return False
        current_year = datetime.now(UTC).year
        # CONTU restricts materials published within five years of the
        # request date. At year granularity the window is the current
        # year plus the (window - 1) preceding years — 5 publication
        # years total. The former ``current_year - window`` cutoff
        # silently included a 6th year.
        recent_cutoff = current_year - self._window_years + 1
        if request.item.year < recent_cutoff:
            return False  # older than recent window — no CONTU constraint

        same_journal_this_year = [
            e
            for e in self._copyright_ledger
            if e.issn == request.item.issn
            and e.fulfilled_at.year == current_year
            and e.article_year >= recent_cutoff
        ]
        return len(same_journal_this_year) >= self._max_per_journal

    @staticmethod
    def _make_rationale(passed: bool, flags: list[PolicyFlag]) -> str:
        if passed and not flags:
            return "All policy checks passed."
        bits = [f.code for f in flags]
        verdict = "PASS (soft flags only)" if passed else "BLOCKED (hard flag)"
        return f"{verdict}: {', '.join(bits)}"
