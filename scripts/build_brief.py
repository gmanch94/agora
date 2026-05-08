"""Agora — Executive Brief generator (PDF, ReportLab/Platypus).

Produces ``artifacts/agora-executive-brief.pdf`` — a 3-ish page written-prose
companion to the slide deck (``scripts/build_deck.py``). Pure ReportLab so it
needs no Word installation; rendering is byte-identical across platforms.

Visual language matches the deck (NAVY / BLUE / TEAL palette, navy-on-white
section headings with a steel underline, status pills in green / amber /
muted, light-blue gate-row fill).

Run: ``python scripts/build_brief.py`` → writes
``artifacts/agora-executive-brief.pdf``.
"""

from __future__ import annotations

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUTPUT = "artifacts/agora-executive-brief.pdf"

# ---------------------------------------------------------------------------
# Palette (mirrors scripts/build_deck.py)
# ---------------------------------------------------------------------------
NAVY = HexColor("#13234A")
BLUE = HexColor("#1A6FFF")
TEAL = HexColor("#0B8278")
STEEL = HexColor("#5C6B80")
MUTED = HexColor("#9099A8")
LIGHT = HexColor("#F4F6FA")
GATE_BG = HexColor("#EAF1FA")
GREEN = HexColor("#0E7C45")
GLIGHT = HexColor("#DFF1E4")
AMBER = HexColor("#B85C00")
ALIGHT = HexColor("#FCEFD5")
WHITE = HexColor("#FFFFFF")
BLACK = HexColor("#222222")
GRID = HexColor("#B0BEC5")

# ---------------------------------------------------------------------------
# Paragraph styles
# ---------------------------------------------------------------------------
_base = getSampleStyleSheet()["Normal"]

TITLE_STYLE = ParagraphStyle(
    "AgTitle", parent=_base,
    fontName="Helvetica-Bold", fontSize=28, leading=32,
    textColor=NAVY, spaceAfter=2,
)
SUB_STYLE = ParagraphStyle(
    "AgSub", parent=_base,
    fontName="Helvetica", fontSize=13, leading=16,
    textColor=STEEL, spaceAfter=2,
)
META_STYLE = ParagraphStyle(
    "AgMeta", parent=_base,
    fontName="Helvetica", fontSize=9, leading=12,
    textColor=MUTED, spaceAfter=6,
)
SECTION_STYLE = ParagraphStyle(
    "AgSection", parent=_base,
    fontName="Helvetica-Bold", fontSize=13, leading=16,
    textColor=NAVY, spaceBefore=10, spaceAfter=2,
)
BODY_STYLE = ParagraphStyle(
    "AgBody", parent=_base,
    fontName="Helvetica", fontSize=9.5, leading=13,
    textColor=BLACK, spaceBefore=1, spaceAfter=3,
    alignment=TA_LEFT,
)
CALLOUT_STYLE = ParagraphStyle(
    "AgCallout", parent=_base,
    fontName="Helvetica-Oblique", fontSize=10.5, leading=14,
    textColor=NAVY, alignment=TA_CENTER,
)
TABLE_TEXT_STYLE = ParagraphStyle(
    "AgTableText", parent=_base,
    fontName="Helvetica", fontSize=9, leading=12,
    textColor=BLACK,
)
TABLE_HEAD_STYLE = ParagraphStyle(
    "AgTableHead", parent=TABLE_TEXT_STYLE,
    fontName="Helvetica-Bold", textColor=WHITE,
)
GATE_TEXT_STYLE = ParagraphStyle(
    "AgGate", parent=TABLE_TEXT_STYLE,
    fontName="Helvetica-Oblique", textColor=STEEL,
)
BULLET_LABEL_STYLE = ParagraphStyle(
    "AgBulletLabel", parent=BODY_STYLE,
    fontSize=10, leading=14, leftIndent=18, firstLineIndent=-18,
    spaceBefore=3, spaceAfter=3,
)
FOOTER_STYLE = ParagraphStyle(
    "AgFooter", parent=_base,
    fontName="Helvetica-Oblique", fontSize=8, leading=11,
    textColor=MUTED, spaceBefore=14,
)

# ---------------------------------------------------------------------------
# Flowable builders
# ---------------------------------------------------------------------------


def section(text: str) -> list:
    """Section heading + steel underline + small spacer."""
    return [
        Paragraph(text, SECTION_STYLE),
        HRFlowable(width="100%", thickness=0.6, color=STEEL,
                   spaceBefore=2, spaceAfter=8),
    ]


def callout(text: str) -> Table:
    """Light-blue tinted box with italic centered text + left blue rule."""
    inner = Paragraph(text, CALLOUT_STYLE)
    tbl = Table([[inner]], colWidths=[6.6 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GATE_BG),
        ("LINEBEFORE", (0, 0), (0, -1), 3, BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def make_table(
    rows: list[tuple[str, ...]],
    col_widths_in: list[float],
    *,
    status_col: int = -1,
    gate_match: tuple[int, str] | None = None,
) -> Table:
    """Build a styled data table.

    ``rows[0]`` is the header (navy fill, white bold).
    Body rows alternate white / light-gray striping.
    ``status_col`` (column index) tints cells green/amber based on text:
      - "implemented" / "green" / "written"  → green pill
      - any other non-empty text             → amber pill
    ``gate_match = (col_idx, substring)`` flags a single row as a gate
    step: light-blue fill, italic steel text.
    """
    head, *body = rows

    # Wrap each cell in a Paragraph for proper text wrapping.
    cells: list[list] = [[_para(h, TABLE_HEAD_STYLE) for h in head]]
    for r in body:
        cells.append([_para(c, TABLE_TEXT_STYLE) for c in r])

    style_cmds: list = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        # Borders
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY),
        ("INNERGRID", (0, 1), (-1, -1), 0.4, GRID),
        ("BOX", (0, 0), (-1, -1), 0.5, GRID),
    ]

    # Striping — first body row is gray, then alternating
    # (matches the prior .docx render).
    for r in range(1, len(rows)):
        if r % 2 == 1:
            style_cmds.append(("BACKGROUND", (0, r), (-1, r), LIGHT))

    # Gate row
    if gate_match is not None:
        gate_col, gate_val = gate_match
        for r in range(1, len(rows)):
            if gate_val and gate_val in rows[r][gate_col]:
                cells[r] = [_para(c, GATE_TEXT_STYLE) for c in rows[r]]
                style_cmds.append(("BACKGROUND", (0, r), (-1, r), GATE_BG))

    # Status column tints
    if status_col >= 0:
        for r in range(1, len(rows)):
            val = rows[r][status_col].strip().lower()
            if not val:
                continue
            if val in ("implemented", "green", "written"):
                bg, fg = GLIGHT, GREEN
            else:
                bg, fg = ALIGHT, AMBER
            style_cmds.append(("BACKGROUND", (status_col, r), (status_col, r), bg))
            cells[r][status_col] = _para(
                f'<font color="#{fg.hexval()[2:]}"><b>{rows[r][status_col]}</b></font>',
                TABLE_TEXT_STYLE,
            )

    table = Table(
        cells,
        colWidths=[w * inch for w in col_widths_in],
        repeatRows=1,
    )
    table.setStyle(TableStyle(style_cmds))
    return table


def labeled_bullet(label: str, text: str) -> Paragraph:
    """Bold navy label + body text on a hanging-indent line.

    NOTE: ``label`` and ``text`` may contain HTML entities (``&mdash;``,
    ``&rarr;``) and inline tags (``<i>...</i>``); they are passed through
    to ReportLab's mini-HTML parser unmodified. Don't pass user-supplied
    text — content is hand-crafted in this file."""
    inner = (
        f'<b><font color="#{NAVY.hexval()[2:]}">{label}</font></b> {text}'
    )
    return Paragraph(inner, BULLET_LABEL_STYLE)


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


def build() -> None:
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=letter,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title="Agora — Executive Brief",
        author="Agora ILL Project",
    )

    story: list = []

    # ── Cover block ────────────────────────────────────────────────────────
    story.append(Paragraph("Agora", TITLE_STYLE))
    story.append(Paragraph("Agentic Inter-Library Loan Orchestrator", SUB_STYLE))
    story.append(Paragraph(
        "May 2026 &nbsp;&nbsp;|&nbsp;&nbsp; Research Prototype "
        "&nbsp;&nbsp;|&nbsp;&nbsp; For internal review",
        META_STYLE,
    ))
    story.append(Spacer(1, 4))
    story.append(callout(
        "Agora automates decision-support for every ILL state transition "
        "while keeping a human staff click as the gate for every state change."
    ))

    # ── Problem ────────────────────────────────────────────────────────────
    story.extend(section("Problem"))
    story.append(Paragraph(
        "Inter-Library Loan (ILL) is one of the most manual workflows in academic "
        "libraries. A single request typically demands 5&ndash;15 staff decisions "
        "spanning discovery, supplier selection, copyright clearance, shipment "
        "tracking, and reconciliation. Existing NCIP automation reduces steps by "
        "~50% on the borrow side (NISO benchmarks) &mdash; but the remaining "
        "decisions are unstructured, untracked, and spread across disparate "
        "systems. Failures (item unavailable, supplier decline, lost in transit) "
        "are common, and legal compliance (CONTU, copyright) raises the bar for "
        "correctness.",
        BODY_STYLE,
    ))

    # ── What Agora Is ──────────────────────────────────────────────────────
    story.extend(section("What Agora Is"))
    story.append(Paragraph(
        "An agentic ILL orchestrator built on top of FOLIO/ReShare &mdash; the "
        "open-source library system stack already deployed across consortia. "
        "Agora adds a multi-agent advisory layer and a human-gated workflow "
        "engine over existing ISO 18626 infrastructure. It does not replace "
        "ReShare; it drives it.",
        BODY_STYLE,
    ))

    # ── How It Works ───────────────────────────────────────────────────────
    story.extend(section("How It Works"))
    story.append(make_table(
        rows=[
            ("Stage", "Component", "What it does"),
            ("1 &mdash; Discovery", "DiscoveryAgent",
             "Resolves citation / OpenURL to item holders via SRU catalog "
             "and CrossRef DOI lookup"),
            ("2 &mdash; Routing", "RoutingAgent",
             "Ranks suppliers by SLA tier, reciprocity, lender load, "
             "proximity; optional Gemini LLM tie-breaker"),
            ("3 &mdash; Policy", "PolicyAgent",
             "Checks CONTU copyright limits, patron eligibility, budget caps; "
             "flags hard violations"),
            ("&rarr; Staff gate", "Staff Console",
             "Staff reviews recommendation and rationale, then clicks "
             "Approve or Reject"),
            ("4 &mdash; Transaction", "TransactionAgent",
             "Drives ReShare over ISO 18626 (send request, ship, return) via "
             "async outbox worker"),
            ("5 &mdash; Tracking", "TrackingAgent",
             "Monitors overdue loans, proposes recalls, flags unconfirmed "
             "receipts &mdash; three advisory tiers"),
            ("6 &mdash; Compensators", "ReconciliationAgent",
             "Rolls back committed steps on failure: cancel, reroute, "
             "dispute resolution, override"),
        ],
        col_widths_in=[1.4, 1.55, 3.65],
        gate_match=(1, "Staff Console"),
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Human-in-the-loop is non-negotiable. Every lifecycle state transition "
        "&mdash; Submit, Route, Approve (via an Approving hold), Ship, Receive, "
        "Return &mdash; requires an explicit staff approval. Agents advise; "
        "staff commit. This preserves legal and policy accountability.",
        BODY_STYLE,
    ))

    # ── Key Design Decisions ───────────────────────────────────────────────
    # Force page 2: keeps the dense decision/build-status/gaps tables on
    # their own page so page 1 is the "elevator pitch" surface.
    story.append(PageBreak())
    story.extend(section("Key Design Decisions"))
    story.append(make_table(
        rows=[
            ("Decision", "Rationale"),
            ("Saga ledger (event-sourced)",
             "Every action is an immutable event. Full audit trail. "
             "Compensators undo exactly what was committed. Replay-safe by "
             "construction."),
            ("Outbox pattern (commit-then-enqueue)",
             "ISO 18626 messages to ReShare commit atomically with the ledger "
             "event &mdash; no silent drops, no double-delivery."),
            ("Advisory-only agents",
             "Legal and policy liability requires a human gate. Agents surface "
             "reasoning; staff own the decision. Enables FedRAMP alignment "
             "path."),
            ("Wraps ReShare, does not replace it",
             "ISO 18626 wire-level correctness stays in mod-rs. Agora drives "
             "it over REST. No XSD reimplementation."),
            ("LLM tie-breaking (optional)",
             "RoutingAgent uses deterministic rules by default. Opt-in Gemini "
             "Flash adapter resolves near-ties. 100% top-1 accuracy on 40 "
             "labeled scenarios (rules-only baseline: 92.5%)."),
        ],
        col_widths_in=[2.35, 4.25],
    ))

    # ── Build Status ───────────────────────────────────────────────────────
    story.extend(section("Build Status"))
    story.append(make_table(
        rows=[
            ("Component", "Status"),
            ("7-state lifecycle (Submitted, Routed, Approving, Approved, "
             "Shipped, Received, Returned)", "Implemented"),
            ("Saga compensators for every forward step", "Implemented"),
            ("All 6 advisory agents (Discovery, Routing, Policy, "
             "Transaction, Tracking, Reconciliation)", "Implemented"),
            ("HTMX + Jinja2 staff console (inbox, detail, approve / reject / "
             "compensate / discover / override)", "Implemented"),
            ("Async outbox worker with Postgres multi-worker safety "
             "(SKIP LOCKED)", "Implemented"),
            ("NCIP fan-out (check-out on receive, check-in on return)",
             "Implemented"),
            ("Overdue scanner &mdash; 3 tiers: overdue / recall-proposed / "
             "unconfirmed-receipt", "Implemented"),
            ("ISO 18626 XSD validation harness", "Implemented"),
            ("LLM routing tie-breaker (Gemini 2.5 Flash via Vertex AI)",
             "Implemented"),
            ("Alembic migrations on real Postgres + CI gate", "Implemented"),
            ("Read-only patron portal (/portal/*) &mdash; saga browse + "
             "status by patron_id", "Implemented"),
            ("RENEW saga step (extends due_at on RECEIVED) + JSON / HTMX "
             "endpoints", "Implemented"),
            ("503 automated tests (unit + property-based + end-to-end; 492 "
             "pass + 11 skipped env-gated)", "Green"),
            ("17 Architecture Decision Records", "Written"),
        ],
        col_widths_in=[4.85, 1.75],
        status_col=1,
    ))

    # ── Remaining Gaps ─────────────────────────────────────────────────────
    story.extend(section("Remaining Gaps"))
    story.append(make_table(
        rows=[
            ("Item", "Blocker"),
            ("ReShare two-tenant probe (Requester-side + recall path)",
             "Responder side verified 2026-05-06; Requester + ADR-0016 "
             "manualClose still pending"),
            ("Live NCIP probe against real ILS",
             "HttpNcipClient shipped (source-reviewed); needs FOLIO mod-ncip "
             "tenant"),
            ("WorldCat holdings lookup", "OCLC v2 paid subscription required"),
            ("FedRAMP authorization",
             "Explicitly deferred &mdash; research prototype scope (ADR-0007)"),
            ("Patron submission UI",
             "Read-only portal shipped (#117); submission form deferred"),
        ],
        col_widths_in=[3.05, 3.55],
        status_col=1,
    ))

    # ── Prototype Validation ───────────────────────────────────────────────
    story.extend(section("Prototype Validation"))
    story.append(labeled_bullet(
        "End-to-end demo:",
        "<i>make demo</i> runs Submit to Return against an in-memory mock in "
        "under 10 seconds, printing every ledger event.",
    ))
    story.append(labeled_bullet(
        "Property-based tests:",
        "Arbitrary saga sequences including partial failures and replay "
        "&mdash; all compensators invert their forward step correctly "
        "(Hypothesis framework).",
    ))
    story.append(labeled_bullet(
        "Routing eval harness:",
        "100% top-1 accuracy, 1.00 mean Spearman against 40 hand-labeled "
        "scenarios (Gemini 2.5 Flash); rules-only baseline 92.5% top-1, "
        "0.84 Spearman.",
    ))
    story.append(labeled_bullet(
        "Postgres CI:",
        "Alembic round-trip (upgrade, downgrade, upgrade) and multi-worker "
        "outbox safety verified against postgres:15-alpine on every PR.",
    ))

    # ── Technology Stack ───────────────────────────────────────────────────
    story.extend(section("Technology Stack"))
    story.append(make_table(
        rows=[
            ("Layer", "Technology"),
            ("Language / Runtime", "Python 3.11+  (built on 3.14.3)"),
            ("API Framework", "FastAPI + pydantic v2"),
            ("Database",
             "PostgreSQL 15  |  SQLAlchemy async  |  Alembic migrations"),
            ("UI",
             "HTMX 2.0.4 + Jinja2  (server-rendered, no build step)"),
            ("Agent LLM",
             "Google Gemini 2.5 Flash via Vertex AI  (Google ADK)"),
            ("ILL Standards",
             "ISO 18626  |  NCIP  |  SRU  |  OpenURL  (via FOLIO/ReShare "
             "mod-rs)"),
            ("Testing",
             "pytest + pytest-asyncio + Hypothesis  (property-based)"),
            ("Quality Gates",
             "ruff  |  mypy --strict  |  bandit  |  pip-audit  |  "
             "detect-secrets"),
        ],
        col_widths_in=[2.15, 4.45],
    ))

    # ── Next Steps ─────────────────────────────────────────────────────────
    story.extend(section("Next Steps to Production Path"))
    next_steps = [
        ("Close ReShare two-tenant verification",
         "Probe Requester-side flow + ADR-0016 manualClose recall path "
         "against a live two-tenant ReShare deployment."),
        ("Pilot with one consortium member",
         "Validate routing quality against a real supplier population; "
         "collect approval latency data."),
        ("Auth + RBAC",
         "HTTP Basic is in place; swap for institution SSO + role separation "
         "(viewer / approver / admin) before pilot."),
        ("Patron PII retention policy",
         "ALA / state-statute compliance: documented retention window + "
         "scrub job + DSAR flow before patron data goes live."),
        ("Patron submission form",
         "Read-only portal shipped (#117); add submission flow per PRD-05 "
         "to close the patron-facing surface."),
    ]
    for i, (label, text) in enumerate(next_steps, 1):
        story.append(labeled_bullet(f"{i}.  {label}:", text))

    # ── Footer ─────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=MUTED,
                             spaceBefore=14, spaceAfter=4))
    story.append(Paragraph(
        "Full technical documentation: docs/ &mdash; PRDs (7), ADRs (17), "
        "architecture diagrams, runbook, solution design, productionization. "
        "Source code: src/agora/. 503 automated tests. "
        "Repository: github.com/gmanch94/agora.",
        FOOTER_STYLE,
    ))

    doc.build(story)
    print(f"Written: {OUTPUT}")


if __name__ == "__main__":
    build()
